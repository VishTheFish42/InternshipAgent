"""Twilio SMS delivery: individual/burst match alerts and the weekly digest."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from twilio.rest import Client

_log = logging.getLogger(__name__)

_APP_NAME = "InternAgent"
_MAX_SMS_CHARS = 480
_BURST_MAX_LINES = 10
_RETRY_DELAY_SECONDS = 300


@dataclass
class MatchInfo:
    company: str
    title: str
    location: str | None
    score: int
    reasoning: str
    apply_url: str


@dataclass
class SendResult:
    success: bool
    sid: str | None
    error: str | None


# ── Phone redaction ───────────────────────────────────────────────────────────


def _redact_phone(phone: str) -> str:
    """Return the phone number with only the last 4 digits visible, for logs."""
    if len(phone) <= 4:
        return "*" * len(phone)
    return f"****{phone[-4:]}"


# ── Message formatting ────────────────────────────────────────────────────────


def format_individual_message(
    company: str,
    title: str,
    location: str | None,
    score: int,
    reasoning: str,
    apply_url: str,
) -> str:
    """Format a single-match SMS. The apply_url is always included verbatim;
    reasoning is truncated to fit within _MAX_SMS_CHARS."""
    header = f"[{_APP_NAME}] {company} · {title}"
    if location:
        header += f" · {location}"
    prefix = f"{header}\nMatch: {score} — "
    suffix = f"\nApply: {apply_url}"

    budget = max(_MAX_SMS_CHARS - len(prefix) - len(suffix), 0)
    if len(reasoning) > budget:
        reasoning = reasoning[: max(budget - 1, 0)].rstrip() + "…" if budget > 0 else ""

    return f"{prefix}{reasoning}{suffix}"


def format_burst_message(matches: list[MatchInfo]) -> str:
    """Format a batched summary SMS for a cycle with many matches at once."""
    ranked = sorted(matches, key=lambda m: m.score, reverse=True)
    lines = [f"[{_APP_NAME}] {len(ranked)} new matches this cycle"]

    shown = ranked[:_BURST_MAX_LINES]
    for i, m in enumerate(shown, start=1):
        loc = f" · {m.location}" if m.location else ""
        lines.append(f" {i}. {m.company} · {m.title}{loc} ({m.score})")

    remaining = len(ranked) - len(shown)
    if remaining > 0:
        lines.append(f" + {remaining} more — run `db stats` to see all")

    return "\n".join(lines)


def format_weekly_digest(
    week_label: str,
    postings_found: int,
    alerts_sent: int,
    top_match: MatchInfo | None,
    unresolved_companies: list[str],
) -> str:
    """Format the weekly digest SMS: stats, top match, and unresolved companies."""
    lines = [
        f"[{_APP_NAME}] Weekly Summary — {week_label}",
        f"This week: {postings_found} new postings, {alerts_sent} alerts sent",
    ]
    if top_match:
        lines.append(f"Top match: {top_match.company} {top_match.title} ({top_match.score}/100)")
    if unresolved_companies:
        lines.append(f"⚠ Not found: {', '.join(unresolved_companies)}")
    return "\n".join(lines)


# ── Sending ───────────────────────────────────────────────────────────────────


def send_sms(client: Client, to: str, from_: str, body: str) -> SendResult:
    """Attempt a single SMS send. Never raises; failures are returned, not thrown."""
    try:
        message = client.messages.create(to=to, from_=from_, body=body)
        return SendResult(success=True, sid=message.sid, error=None)
    except Exception as exc:
        _log.warning("SMS send failed for %s: %s", _redact_phone(to), exc)
        return SendResult(success=False, sid=None, error=str(exc))


def send_sms_with_retry(
    client: Client,
    to: str,
    from_: str,
    body: str,
    *,
    retry_delay_seconds: int = _RETRY_DELAY_SECONDS,
) -> SendResult:
    """Send an SMS; on failure, wait once and retry. Logs a permanent failure if the retry also fails."""
    result = send_sms(client, to, from_, body)
    if result.success:
        return result

    _log.warning(
        "Retrying SMS to %s in %ds after initial failure", _redact_phone(to), retry_delay_seconds
    )
    time.sleep(retry_delay_seconds)
    retry_result = send_sms(client, to, from_, body)
    if not retry_result.success:
        _log.error(
            "SMS to %s permanently failed after retry: %s", _redact_phone(to), retry_result.error
        )
    return retry_result


def check_delivery_status(client: Client, message_sid: str) -> str:
    """Fetch the current Twilio delivery status for a previously sent message."""
    message = client.messages(message_sid).fetch()
    status: str = message.status
    return status


# ── Public entrypoint ─────────────────────────────────────────────────────────


def notify_matches(
    client: Client,
    matches: list[MatchInfo],
    *,
    to: str,
    from_: str,
    burst_threshold: int,
) -> list[SendResult]:
    """
    Route matches to individual or burst mode based on burst_threshold, and send.
    Returns one SendResult per SMS actually sent (one for burst mode).
    """
    if not matches:
        return []

    if len(matches) >= burst_threshold:
        body = format_burst_message(matches)
        return [send_sms_with_retry(client, to, from_, body)]

    return [
        send_sms_with_retry(
            client,
            to,
            from_,
            format_individual_message(
                m.company, m.title, m.location, m.score, m.reasoning, m.apply_url
            ),
        )
        for m in matches
    ]
