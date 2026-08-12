"""Unit tests for notifier — all Telegram HTTP calls are mocked."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from src.notifier import (
    MatchInfo,
    _redact_chat_id,
    answer_callback_query,
    applied_button,
    format_burst_message,
    format_digest,
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
    assert "88" in body
    assert "Great fit." in body
    assert "Kubernetes" in body
    assert "https://apply.example/1" in body


def test_format_individual_message_omits_location_when_none():
    body = format_individual_message(
        "Stripe", "SWE Intern", None, 88, "Great fit.", [], "https://apply.example/1"
    )
    assert " · Remote" not in body
    assert "Stripe · SWE Intern" in body


def test_format_individual_message_handles_empty_missing_list():
    body = format_individual_message(
        "Stripe", "SWE Intern", "Remote", 88, "Great fit.", [], "https://apply.example/1"
    )
    assert "Missing: none listed" in body


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


# ── format_burst_message ──────────────────────────────────────────────────────


def test_format_burst_message_includes_count_and_ranks_by_score():
    matches = [_match(company="Zeta", score=50), _match(company="Omega", score=90)]
    body = format_burst_message(matches)
    assert "2 new postings" in body
    assert body.index("Omega") < body.index("Zeta")


def test_format_burst_message_singular_for_one_posting():
    body = format_burst_message([_match(company="Stripe")])
    assert "1 new posting this cycle" in body


def test_format_burst_message_includes_missing_qualifications_per_item():
    matches = [_match(company="Acme", missing=["Kubernetes", "GraphQL"])]
    body = format_burst_message(matches)
    assert "missing: Kubernetes, GraphQL" in body


def test_format_burst_message_handles_empty_missing_list():
    matches = [_match(company="Acme", missing=[])]
    body = format_burst_message(matches)
    assert "missing: none listed" in body


def test_format_burst_message_includes_apply_link_per_item():
    matches = [_match(company="Stripe")]
    body = format_burst_message(matches)
    assert "Apply: https://boards.greenhouse.io/stripe/jobs/123456" in body


def test_format_burst_message_caps_at_ten_lines_with_overflow_note():
    matches = [_match(company=f"Company{i}", score=i) for i in range(15)]
    body = format_burst_message(matches)
    for i in range(1, 11):
        assert f" {i}. " in body
    assert "+ 5 more" in body
    assert "db stats" in body


def test_format_burst_message_no_overflow_note_when_under_cap():
    matches = [_match(company="A"), _match(company="B")]
    body = format_burst_message(matches)
    assert "more" not in body


# ── format_digest ──────────────────────────────────────────────────────────────


def test_format_digest_includes_stats():
    body = format_digest("Sun May 10 3:00PM", 47, 6, _match(score=92), [])
    assert "47 new postings" in body
    assert "6 alerts sent" in body
    assert "92/100" in body


def test_format_digest_includes_unresolved_companies():
    body = format_digest("Sun May 10 3:00PM", 47, 6, None, ["Acme Corp", "Widgets Inc"])
    assert "Acme Corp" in body
    assert "Widgets Inc" in body
    assert "⚠" in body


def test_format_digest_omits_unresolved_section_when_empty():
    body = format_digest("Sun May 10 3:00PM", 47, 6, None, [])
    assert "⚠" not in body


def test_format_digest_omits_top_match_when_none():
    body = format_digest("Sun May 10 3:00PM", 47, 6, None, [])
    assert "Top match" not in body


# ── send_message ──────────────────────────────────────────────────────────────


def test_send_message_success_returns_message_id():
    with patch("src.notifier.httpx.post", return_value=_ok_response(111)) as mock_post:
        result = send_message("bot-token", "12345", "hello")

    assert result.success is True
    assert result.message_id == 111
    assert result.error is None
    _, kwargs = mock_post.call_args
    assert kwargs["json"] == {"chat_id": "12345", "text": "hello"}


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
