"""Unit tests for notifier — all Twilio calls are mocked."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from src.notifier import (
    MatchInfo,
    _redact_phone,
    check_delivery_status,
    format_burst_message,
    format_individual_message,
    format_weekly_digest,
    notify_matches,
    send_sms,
    send_sms_with_retry,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _match(company="Stripe", title="Software Engineering Intern", location="Remote", score=88):
    return MatchInfo(
        company=company,
        title=title,
        location=location,
        score=score,
        reasoning="Strong Python/backend fit, welcoming undergrads.",
        apply_url="https://boards.greenhouse.io/stripe/jobs/123456",
    )


# ── _redact_phone ─────────────────────────────────────────────────────────────


def test_redact_phone_keeps_last_four_digits():
    assert _redact_phone("+15551234567") == "****4567"


def test_redact_phone_handles_short_string():
    assert _redact_phone("123") == "***"


# ── format_individual_message ─────────────────────────────────────────────────


def test_format_individual_message_includes_all_fields():
    body = format_individual_message(
        "Stripe",
        "Software Engineering Intern",
        "Remote",
        88,
        "Great fit.",
        "https://apply.example/1",
    )
    assert "Stripe" in body
    assert "Software Engineering Intern" in body
    assert "Remote" in body
    assert "88" in body
    assert "Great fit." in body
    assert "https://apply.example/1" in body


def test_format_individual_message_omits_location_when_none():
    body = format_individual_message(
        "Stripe", "SWE Intern", None, 88, "Great fit.", "https://apply.example/1"
    )
    assert " · Remote" not in body
    assert "Stripe · SWE Intern" in body


def test_format_individual_message_stays_within_sms_budget():
    body = format_individual_message(
        "Stripe",
        "Software Engineering Intern",
        "Remote",
        88,
        "x" * 1000,
        "https://apply.example/1",
    )
    assert len(body) <= 480


def test_format_individual_message_never_truncates_apply_url():
    long_url = "https://apply.example/" + "a" * 300
    body = format_individual_message("Stripe", "SWE Intern", "Remote", 88, "x" * 1000, long_url)
    assert long_url in body


def test_format_individual_message_truncates_reasoning_with_ellipsis():
    body = format_individual_message(
        "Stripe", "SWE Intern", "Remote", 88, "x" * 1000, "https://apply.example/1"
    )
    assert "…" in body


def test_format_individual_message_no_truncation_for_short_reasoning():
    body = format_individual_message(
        "Stripe", "SWE Intern", "Remote", 88, "Great fit.", "https://apply.example/1"
    )
    assert "…" not in body
    assert "Great fit." in body


# ── format_burst_message ──────────────────────────────────────────────────────


def test_format_burst_message_includes_count_and_ranks_by_score():
    matches = [_match(company="Zeta", score=50), _match(company="Omega", score=90)]
    body = format_burst_message(matches)
    assert "2 new matches" in body
    assert body.index("Omega") < body.index("Zeta")


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


# ── format_weekly_digest ──────────────────────────────────────────────────────


def test_format_weekly_digest_includes_stats():
    body = format_weekly_digest("Sun May 10", 47, 6, _match(score=92), [])
    assert "47 new postings" in body
    assert "6 alerts sent" in body
    assert "92/100" in body


def test_format_weekly_digest_includes_unresolved_companies():
    body = format_weekly_digest("Sun May 10", 47, 6, None, ["Acme Corp", "Widgets Inc"])
    assert "Acme Corp" in body
    assert "Widgets Inc" in body
    assert "⚠" in body


def test_format_weekly_digest_omits_unresolved_section_when_empty():
    body = format_weekly_digest("Sun May 10", 47, 6, None, [])
    assert "⚠" not in body


def test_format_weekly_digest_omits_top_match_when_none():
    body = format_weekly_digest("Sun May 10", 47, 6, None, [])
    assert "Top match" not in body


# ── send_sms ──────────────────────────────────────────────────────────────────


def test_send_sms_success_returns_sid():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(sid="SM123")

    result = send_sms(mock_client, "+15551234567", "+15557654321", "hello")

    assert result.success is True
    assert result.sid == "SM123"
    assert result.error is None


def test_send_sms_failure_returns_error_not_raises():
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("Twilio down")

    result = send_sms(mock_client, "+15551234567", "+15557654321", "hello")

    assert result.success is False
    assert result.sid is None
    assert "Twilio down" in (result.error or "")


def test_send_sms_does_not_leak_phone_number_in_logs(caplog):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        send_sms(mock_client, "+15551234567", "+15557654321", "hello")

    assert "5551234567" not in caplog.text
    assert "****4567" in caplog.text


# ── send_sms_with_retry ────────────────────────────────────────────────────────


def test_send_sms_with_retry_success_on_first_try_no_sleep():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(sid="SM123")

    with patch("src.notifier.time.sleep") as mock_sleep:
        result = send_sms_with_retry(mock_client, "+15551234567", "+15557654321", "hello")

    assert result.success is True
    mock_sleep.assert_not_called()


def test_send_sms_with_retry_retries_once_after_failure():
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [RuntimeError("boom"), MagicMock(sid="SM456")]

    with patch("src.notifier.time.sleep") as mock_sleep:
        result = send_sms_with_retry(mock_client, "+15551234567", "+15557654321", "hello")

    assert result.success is True
    assert result.sid == "SM456"
    mock_sleep.assert_called_once_with(300)


def test_send_sms_with_retry_permanent_failure_after_both_attempts_fail(caplog):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("boom")

    with patch("src.notifier.time.sleep"), caplog.at_level(logging.ERROR):
        result = send_sms_with_retry(mock_client, "+15551234567", "+15557654321", "hello")

    assert result.success is False
    assert "permanently failed" in caplog.text


# ── check_delivery_status ──────────────────────────────────────────────────────


def test_check_delivery_status_returns_status():
    mock_client = MagicMock()
    mock_client.messages.return_value.fetch.return_value = MagicMock(status="delivered")

    status = check_delivery_status(mock_client, "SM123")

    assert status == "delivered"
    mock_client.messages.assert_called_once_with("SM123")


# ── notify_matches ──────────────────────────────────────────────────────────────


def test_notify_matches_empty_list_sends_nothing():
    mock_client = MagicMock()
    results = notify_matches(mock_client, [], to="+1", from_="+2", burst_threshold=5)
    assert results == []
    mock_client.messages.create.assert_not_called()


def test_notify_matches_individual_mode_below_threshold():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(sid="SM1")
    matches = [_match(company="A"), _match(company="B")]

    with patch("src.notifier.time.sleep"):
        results = notify_matches(mock_client, matches, to="+1", from_="+2", burst_threshold=5)

    assert len(results) == 2
    assert mock_client.messages.create.call_count == 2


def test_notify_matches_burst_mode_at_or_above_threshold():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(sid="SM1")
    matches = [_match(company=f"C{i}") for i in range(5)]

    with patch("src.notifier.time.sleep"):
        results = notify_matches(mock_client, matches, to="+1", from_="+2", burst_threshold=5)

    assert len(results) == 1
    assert mock_client.messages.create.call_count == 1
    body = mock_client.messages.create.call_args.kwargs["body"]
    assert "5 new matches" in body
