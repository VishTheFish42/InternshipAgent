"""Unit tests for notifier — all Telegram HTTP calls are mocked."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from src.notifier import (
    MatchInfo,
    _friendly_source,
    _redact_chat_id,
    answer_callback_query,
    applied_button,
    bot_token_is_rejected,
    edit_message_mark_applied,
    format_burst_messages,
    format_individual_message,
    get_updates,
    notify_matches,
    send_message,
    send_message_with_retry,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _match(
    company="Stripe",
    title="Software Engineering Intern",
    location="Remote",
    score=88,
    missing=None,
    posting_id=1,
    source="",
):
    return MatchInfo(
        company=company,
        title=title,
        location=location,
        score=score,
        reasoning="Strong Python/backend fit, welcoming undergrads.",
        missing_qualifications=missing if missing is not None else [],
        apply_url="https://boards.greenhouse.io/stripe/jobs/123456",
        posting_id=posting_id,
        source=source,
    )


def _ok_response(message_id: int = 111) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"ok": True, "result": {"message_id": message_id}}
    return resp


def _error_response(description: str = "Bad Request: chat not found") -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "description": description}
    return resp


# ── _redact_chat_id ───────────────────────────────────────────────────────────


def test_redact_chat_id_keeps_last_four_chars():
    assert _redact_chat_id("123456789") == "****6789"


def test_redact_chat_id_handles_short_string():
    assert _redact_chat_id("123") == "***"


# ── format_individual_message ─────────────────────────────────────────────────


def test_format_individual_message_includes_all_fields():
    body = format_individual_message(
        "Stripe",
        "Software Engineering Intern",
        "Remote",
        88,
        "Great fit.",
        ["Kubernetes"],
        "https://apply.example/1",
    )
    assert "Stripe" in body
    assert "Software Engineering Intern" in body
    assert "Remote" in body
    assert "88/100" in body
    assert "Great fit." in body
    assert "Kubernetes" in body
    assert 'href="https://apply.example/1"' in body


def test_format_individual_message_omits_location_when_none():
    body = format_individual_message(
        "Stripe", "SWE Intern", None, 88, "Great fit.", [], "https://apply.example/1"
    )
    assert "📍" not in body
    assert "<b>Stripe</b> — SWE Intern" in body


def test_format_individual_message_handles_empty_missing_list():
    body = format_individual_message(
        "Stripe", "SWE Intern", "Remote", 88, "Great fit.", [], "https://apply.example/1"
    )
    assert "<b>Missing:</b> none listed" in body


def test_format_individual_message_stays_within_message_budget():
    body = format_individual_message(
        "Stripe",
        "Software Engineering Intern",
        "Remote",
        88,
        "x" * 5000,
        [],
        "https://apply.example/1",
    )
    assert len(body) <= 4096


def test_format_individual_message_never_truncates_apply_url():
    long_url = "https://apply.example/" + "a" * 300
    body = format_individual_message("Stripe", "SWE Intern", "Remote", 88, "x" * 5000, [], long_url)
    assert long_url in body


def test_format_individual_message_truncates_reasoning_with_ellipsis():
    body = format_individual_message(
        "Stripe", "SWE Intern", "Remote", 88, "x" * 5000, [], "https://apply.example/1"
    )
    assert "…" in body


def test_format_individual_message_no_truncation_for_short_reasoning():
    body = format_individual_message(
        "Stripe", "SWE Intern", "Remote", 88, "Great fit.", [], "https://apply.example/1"
    )
    assert "…" not in body
    assert "Great fit." in body


def test_format_individual_message_includes_source_line_when_given():
    body = format_individual_message(
        "Stripe",
        "SWE Intern",
        "Remote",
        88,
        "Great fit.",
        [],
        "https://apply.example/1",
        "jsearch:LinkedIn",
    )
    assert "🔗 LinkedIn" in body


def test_format_individual_message_omits_source_line_when_empty():
    body = format_individual_message(
        "Stripe", "SWE Intern", "Remote", 88, "Great fit.", [], "https://apply.example/1", ""
    )
    assert "🔗" not in body


def test_format_individual_message_escapes_html_special_characters():
    """Company/title/reasoning/missing come from AI output or a scraped
    posting — never safe to trust verbatim inside an HTML-parsed message."""
    body = format_individual_message(
        "R&D <Labs>",
        "Engineer",
        "Remote",
        88,
        "Great <fit> & ready",
        ["C++ & Rust"],
        "https://apply.example/1?a=1&b=2",
    )
    assert "<Labs>" not in body
    assert "R&amp;D" in body
    assert "&lt;Labs&gt;" in body
    assert "&lt;fit&gt;" in body
    # the URL itself must be escaped too, for safe use inside href="..."
    assert "a=1&amp;b=2" in body


def test_format_individual_message_score_emoji_tiers():
    high = format_individual_message("A", "B", None, 85, "x", [], "https://x")
    mid = format_individual_message("A", "B", None, 50, "x", [], "https://x")
    low = format_individual_message("A", "B", None, 20, "x", [], "https://x")
    assert "🟢" in high
    assert "🟡" in mid
    assert "🔴" in low


# ── _friendly_source ──────────────────────────────────────────────────────────


def test_friendly_source_maps_known_prefixes():
    assert _friendly_source("greenhouse:stripe") == "Greenhouse"
    assert _friendly_source("lever:acme") == "Lever"
    assert _friendly_source("indeed") == "Indeed"
    assert _friendly_source("hn") == "HackerNews"
    assert _friendly_source("remoteok") == "RemoteOK"
    assert _friendly_source("adzuna") == "Adzuna"


def test_friendly_source_shows_jsearch_publisher_verbatim():
    assert _friendly_source("jsearch:LinkedIn") == "LinkedIn"
    assert _friendly_source("jsearch:ZipRecruiter") == "ZipRecruiter"


def test_friendly_source_falls_back_for_unknown_prefix():
    assert _friendly_source("mystery") == "Mystery"


def test_friendly_source_handles_empty_string():
    assert _friendly_source("") == "Unknown"


# ── format_burst_message ──────────────────────────────────────────────────────


def test_format_burst_message_includes_count_and_ranks_by_score():
    matches = [_match(company="Zeta", score=50), _match(company="Omega", score=90)]
    body = format_burst_messages(matches)[0].body
    assert "2 new postings" in body
    assert body.index("Omega") < body.index("Zeta")


def test_format_burst_message_singular_for_one_posting():
    body = format_burst_messages([_match(company="Stripe")])[0].body
    assert "1 new posting this cycle" in body


def test_format_burst_message_includes_missing_qualifications_per_item():
    matches = [_match(company="Acme", missing=["Kubernetes", "GraphQL"])]
    body = format_burst_messages(matches)[0].body
    assert "Missing: Kubernetes, GraphQL" in body


def test_format_burst_message_handles_empty_missing_list():
    matches = [_match(company="Acme", missing=[])]
    body = format_burst_messages(matches)[0].body
    assert "Missing: none listed" in body


def test_format_burst_message_includes_apply_link_per_item():
    matches = [_match(company="Stripe")]
    body = format_burst_messages(matches)[0].body
    assert 'href="https://boards.greenhouse.io/stripe/jobs/123456"' in body


def test_format_burst_message_includes_source_when_given():
    matches = [_match(company="Stripe", source="jsearch:LinkedIn")]
    body = format_burst_messages(matches)[0].body
    assert "🔗LinkedIn" in body


def test_format_burst_message_omits_source_when_empty():
    matches = [_match(company="Stripe", source="")]
    body = format_burst_messages(matches)[0].body
    assert "🔗" not in body


def test_format_burst_messages_lists_every_posting_across_parts():
    matches = [_match(company=f"Company{i}", score=100 - i) for i in range(25)]
    parts = format_burst_messages(matches)
    assert len(parts) == 3
    assert [len(p.matches) for p in parts] == [10, 10, 5]
    assert "25 new postings this cycle</b> (part 2 of 3)" in parts[1].body
    combined = "\n".join(p.body for p in parts)
    for i in range(1, 26):
        assert f"<b>{i}. Company{i - 1}</b>" in combined


def test_format_burst_messages_splits_on_message_length():
    long_url = "https://example.com/" + "x" * 1000
    matches = [_match(company=f"C{i}") for i in range(10)]
    for m in matches:
        m.apply_url = long_url
    parts = format_burst_messages(matches)
    assert len(parts) > 1
    assert all(len(p.body) <= 4096 for p in parts)
    assert sum(len(p.matches) for p in parts) == 10


def test_format_burst_messages_single_part_has_no_part_label():
    parts = format_burst_messages([_match(company=f"C{i}") for i in range(10)])
    assert len(parts) == 1
    assert "part" not in parts[0].body


def test_format_burst_message_no_overflow_note_when_under_cap():
    matches = [_match(company="A"), _match(company="B")]
    body = format_burst_messages(matches)[0].body
    assert "more" not in body


# ── send_message ──────────────────────────────────────────────────────────────


def test_send_message_success_returns_message_id():
    with patch("src.notifier.httpx.post", return_value=_ok_response(111)) as mock_post:
        result = send_message("bot-token", "12345", "hello")

    assert result.success is True
    assert result.message_id == 111
    assert result.error is None
    _, kwargs = mock_post.call_args
    assert kwargs["json"] == {"chat_id": "12345", "text": "hello"}


def test_send_message_omits_parse_mode_by_default():
    with patch("src.notifier.httpx.post", return_value=_ok_response()) as mock_post:
        send_message("bot-token", "12345", "hello")

    assert "parse_mode" not in mock_post.call_args.kwargs["json"]


def test_send_message_includes_parse_mode_when_given():
    with patch("src.notifier.httpx.post", return_value=_ok_response()) as mock_post:
        send_message("bot-token", "12345", "<b>hello</b>", parse_mode="HTML")

    assert mock_post.call_args.kwargs["json"]["parse_mode"] == "HTML"


def test_send_message_api_error_returns_error_not_raises():
    with patch("src.notifier.httpx.post", return_value=_error_response("chat not found")):
        result = send_message("bot-token", "12345", "hello")

    assert result.success is False
    assert result.message_id is None
    assert "chat not found" in (result.error or "")


def test_send_message_network_exception_returns_error_not_raises():
    with patch("src.notifier.httpx.post", side_effect=RuntimeError("connection refused")):
        result = send_message("bot-token", "12345", "hello")

    assert result.success is False
    assert "connection refused" in (result.error or "")


def test_send_message_does_not_leak_chat_id_in_logs(caplog):
    with (
        patch("src.notifier.httpx.post", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.WARNING),
    ):
        send_message("bot-token", "999999999", "hello")

    assert "999999999" not in caplog.text
    assert "****9999" in caplog.text


# ── send_message_with_retry ───────────────────────────────────────────────────


def test_send_message_with_retry_success_on_first_try_no_sleep():
    with (
        patch("src.notifier.httpx.post", return_value=_ok_response()),
        patch("src.notifier.time.sleep") as mock_sleep,
    ):
        result = send_message_with_retry("bot-token", "12345", "hello")

    assert result.success is True
    mock_sleep.assert_not_called()


def test_send_message_with_retry_retries_once_after_failure():
    with (
        patch(
            "src.notifier.httpx.post",
            side_effect=[RuntimeError("boom"), _ok_response(222)],
        ),
        patch("src.notifier.time.sleep") as mock_sleep,
    ):
        result = send_message_with_retry("bot-token", "12345", "hello")

    assert result.success is True
    assert result.message_id == 222
    mock_sleep.assert_called_once_with(300)


def test_send_message_with_retry_permanent_failure_after_both_attempts_fail(caplog):
    with (
        patch("src.notifier.httpx.post", side_effect=RuntimeError("boom")),
        patch("src.notifier.time.sleep"),
        caplog.at_level(logging.ERROR),
    ):
        result = send_message_with_retry("bot-token", "12345", "hello")

    assert result.success is False
    assert "permanently failed" in caplog.text


# ── notify_matches ──────────────────────────────────────────────────────────────


def test_notify_matches_empty_list_sends_nothing():
    with patch("src.notifier.httpx.post") as mock_post:
        results = notify_matches("bot-token", [], chat_id="12345", burst_threshold=5)
    assert results == []
    mock_post.assert_not_called()


def test_notify_matches_individual_mode_below_threshold():
    matches = [_match(company="A", posting_id=101), _match(company="B", posting_id=102)]

    with (
        patch("src.notifier.httpx.post", return_value=_ok_response()) as mock_post,
        patch("src.notifier.time.sleep"),
    ):
        results = notify_matches("bot-token", matches, chat_id="12345", burst_threshold=5)

    assert len(results) == 2
    assert mock_post.call_count == 2

    first_markup = mock_post.call_args_list[0].kwargs["json"]["reply_markup"]
    assert first_markup["inline_keyboard"][0][0]["callback_data"] == "applied:101"
    second_markup = mock_post.call_args_list[1].kwargs["json"]["reply_markup"]
    assert second_markup["inline_keyboard"][0][0]["callback_data"] == "applied:102"
    assert mock_post.call_args_list[0].kwargs["json"]["parse_mode"] == "HTML"


def test_notify_matches_burst_mode_at_or_above_threshold():
    matches = [_match(company=f"C{i}") for i in range(5)]

    with (
        patch("src.notifier.httpx.post", return_value=_ok_response()) as mock_post,
        patch("src.notifier.time.sleep"),
    ):
        results = notify_matches("bot-token", matches, chat_id="12345", burst_threshold=5)

    assert len(results) == 1
    assert mock_post.call_count == 1
    body = mock_post.call_args.kwargs["json"]["text"]
    assert "5 new postings" in body
    assert "reply_markup" not in mock_post.call_args.kwargs["json"]
    assert mock_post.call_args.kwargs["json"]["parse_mode"] == "HTML"


def test_notify_matches_individual_mode_paces_sends():
    matches = [_match(company="A"), _match(company="B"), _match(company="C")]

    with (
        patch("src.notifier.httpx.post", return_value=_ok_response()),
        patch("src.notifier.time.sleep") as mock_sleep,
    ):
        notify_matches("bot-token", matches, chat_id="12345", burst_threshold=5)

    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(1.0)


def test_notify_matches_burst_mode_does_not_pace_sends():
    matches = [_match(company=f"C{i}") for i in range(5)]

    with (
        patch("src.notifier.httpx.post", return_value=_ok_response()),
        patch("src.notifier.time.sleep") as mock_sleep,
    ):
        notify_matches("bot-token", matches, chat_id="12345", burst_threshold=5)

    mock_sleep.assert_not_called()


# ── applied_button ────────────────────────────────────────────────────────────


def test_applied_button_encodes_posting_id():
    markup = applied_button(42)
    button = markup["inline_keyboard"][0][0]
    assert button["callback_data"] == "applied:42"
    assert "Mark Applied" in button["text"]


# ── get_updates ───────────────────────────────────────────────────────────────


def test_get_updates_returns_result_list():
    resp = MagicMock()
    resp.json.return_value = {"ok": True, "result": [{"update_id": 1}, {"update_id": 2}]}
    with patch("src.notifier.httpx.get", return_value=resp) as mock_get:
        updates = get_updates("bot-token", offset=5)

    assert updates == [{"update_id": 1}, {"update_id": 2}]
    assert mock_get.call_args.kwargs["params"]["offset"] == 5


def test_get_updates_returns_empty_list_on_api_error():
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "description": "Unauthorized"}
    with patch("src.notifier.httpx.get", return_value=resp):
        updates = get_updates("bot-token")
    assert updates == []


def test_get_updates_returns_empty_list_on_network_error():
    with patch("src.notifier.httpx.get", side_effect=RuntimeError("boom")):
        updates = get_updates("bot-token")
    assert updates == []


def test_get_updates_omits_offset_when_none():
    resp = MagicMock()
    resp.json.return_value = {"ok": True, "result": []}
    with patch("src.notifier.httpx.get", return_value=resp) as mock_get:
        get_updates("bot-token")
    assert "offset" not in mock_get.call_args.kwargs["params"]


# ── answer_callback_query ─────────────────────────────────────────────────────


def test_answer_callback_query_sends_expected_payload():
    resp = MagicMock()
    resp.json.return_value = {"ok": True}
    with patch("src.notifier.httpx.post", return_value=resp) as mock_post:
        answer_callback_query("bot-token", "cbq-1", text="Marked applied")

    payload = mock_post.call_args.kwargs["json"]
    assert payload == {"callback_query_id": "cbq-1", "text": "Marked applied"}


def test_answer_callback_query_never_raises_on_network_error():
    with patch("src.notifier.httpx.post", side_effect=RuntimeError("boom")):
        answer_callback_query("bot-token", "cbq-1")  # should not raise


# ── edit_message_mark_applied ────────────────────────────────────────────────


def test_edit_message_mark_applied_sends_expected_payload():
    resp = MagicMock()
    resp.json.return_value = {"ok": True}
    with patch("src.notifier.httpx.post", return_value=resp) as mock_post:
        edit_message_mark_applied("bot-token", "12345", "999", "<b>Stripe</b> — SWE Intern")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["chat_id"] == "12345"
    assert payload["message_id"] == 999
    assert payload["text"] == "<b>Stripe</b> — SWE Intern\n\n✅ Applied"
    assert payload["reply_markup"] == {"inline_keyboard": []}
    assert payload["parse_mode"] == "HTML"


def test_edit_message_mark_applied_never_raises_on_network_error():
    with patch("src.notifier.httpx.post", side_effect=RuntimeError("boom")):
        edit_message_mark_applied("bot-token", "12345", "999", "original text")  # should not raise


def test_edit_message_mark_applied_logs_but_does_not_raise_on_api_error(caplog):
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "description": "message is not modified"}
    with (
        patch("src.notifier.httpx.post", return_value=resp),
        caplog.at_level(logging.WARNING),
    ):
        edit_message_mark_applied("bot-token", "12345", "999", "original text")

    assert "message is not modified" in caplog.text


def test_notify_matches_burst_sends_every_part_paced():
    matches = [_match(company=f"C{i}") for i in range(25)]

    with (
        patch("src.notifier.httpx.post", return_value=_ok_response()) as mock_post,
        patch("src.notifier.time.sleep") as mock_sleep,
    ):
        results = notify_matches("bot-token", matches, chat_id="12345", burst_threshold=5)

    assert len(results) == 3
    assert mock_post.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(1.0)


def test_notify_matches_burst_waits_out_retry_delay_only_once():
    matches = [_match(company=f"C{i}") for i in range(25)]
    rejected = MagicMock()
    rejected.json.return_value = {"ok": False, "description": "Unauthorized"}

    with (
        patch("src.notifier.httpx.post", return_value=rejected) as mock_post,
        patch("src.notifier.time.sleep") as mock_sleep,
    ):
        results = notify_matches("bot-token", matches, chat_id="12345", burst_threshold=5)

    assert [r.success for r in results] == [False, False, False]
    assert mock_post.call_count == 4  # first part twice, then one try each
    delays = [c.args[0] for c in mock_sleep.call_args_list]
    assert delays.count(300) == 1


def test_notify_matches_burst_failed_part_does_not_block_later_parts():
    matches = [_match(company=f"C{i}") for i in range(25)]
    rejected = MagicMock()
    rejected.json.return_value = {"ok": False, "description": "Bad Request"}

    with (
        patch(
            "src.notifier.httpx.post",
            side_effect=[rejected, rejected, _ok_response(), _ok_response()],
        ),
        patch("src.notifier.time.sleep"),
    ):
        results = notify_matches("bot-token", matches, chat_id="12345", burst_threshold=5)

    assert [r.success for r in results] == [False, True, True]


def test_bot_token_is_rejected_when_telegram_says_so():
    rejected = MagicMock()
    rejected.json.return_value = {"ok": False, "description": "Unauthorized"}
    with patch("src.notifier.httpx.get", return_value=rejected):
        assert bot_token_is_rejected("bad-token") is True


def test_bot_token_is_not_rejected_when_valid():
    accepted = MagicMock()
    accepted.json.return_value = {"ok": True, "result": {"id": 1}}
    with patch("src.notifier.httpx.get", return_value=accepted):
        assert bot_token_is_rejected("good-token") is False


def test_bot_token_is_not_rejected_on_network_error():
    with patch("src.notifier.httpx.get", side_effect=RuntimeError("no route")):
        assert bot_token_is_rejected("any-token") is False
