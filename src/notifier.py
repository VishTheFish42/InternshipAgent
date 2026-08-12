"""Telegram delivery: individual/burst match alerts and the periodic digest.

Switched from Twilio SMS after a toll-free verification rejection — Twilio's
A2P/toll-free review process is built to vet registered businesses, which a
personal single-recipient tool doesn't fit. Telegram's Bot API delivers the
same real-time push-notification experience with no business verification of
any kind: create a bot via @BotFather, get a token, done.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

_log = logging.getLogger(__name__)

_APP_NAME = "InternAgent"
_MAX_MESSAGE_CHARS = 4096  # Telegram's hard per-message limit
_BURST_MAX_LINES = 10
_RETRY_DELAY_SECONDS = 300
_INDIVIDUAL_SEND_DELAY_SECONDS = 1.0  # Telegram's documented per-chat rate limit is ~1 msg/sec
_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


@dataclass
class MatchInfo:
    company: str
    title: str
    location: str | None
    score: int
    reasoning: str
    apply_url: str


@dataclass
class PartialMatchInfo:
    company: str
    title: str
    score: int
    missing_qualifications: list[str]
    apply_url: str


@dataclass
class SendResult:
    success: bool
    message_id: int | None
    error: str | None


# ── Chat ID redaction ─────────────────────────────────────────────────────────


def _redact_chat_id(chat_id: str) -> str:
    """Return the chat ID with only the last 4 characters visible, for logs."""
    if len(chat_id) <= 4:
        return "*" * len(chat_id)
    return f"****{chat_id[-4:]}"


# ── Message formatting ────────────────────────────────────────────────────────


def format_individual_message(
    company: str,
    title: str,
    location: str | None,
    score: int,
    reasoning: str,
    apply_url: str,
) -> str:
    """Format a single-match message. The apply_url is always included verbatim;
    reasoning is truncated to fit within _MAX_MESSAGE_CHARS."""
    header = f"[{_APP_NAME}] {company} · {title}"
    if location:
        header += f" · {location}"
    prefix = f"{header}\nMatch: {score} — "
    suffix = f"\nApply: {apply_url}"

    budget = max(_MAX_MESSAGE_CHARS - len(prefix) - len(suffix), 0)
    if len(reasoning) > budget:
        reasoning = reasoning[: max(budget - 1, 0)].rstrip() + "…" if budget > 0 else ""

    return f"{prefix}{reasoning}{suffix}"


def format_burst_message(matches: list[MatchInfo]) -> str:
    """Format a batched summary message for a cycle with many matches at once."""
    ranked = sorted(matches, key=lambda m: m.score, reverse=True)
    lines = [f"[{_APP_NAME}] {len(ranked)} new matches this cycle"]

    shown = ranked[:_BURST_MAX_LINES]
    for i, m in enumerate(shown, start=1):
        loc = f" · {m.location}" if m.location else ""
        lines.append(f" {i}. {m.company} · {m.title}{loc} ({m.score})")
        lines.append(f"    Apply: {m.apply_url}")

    remaining = len(ranked) - len(shown)
    if remaining > 0:
        lines.append(f" + {remaining} more — run `db stats` to see all")

    return "\n".join(lines)


def format_partial_match_individual_message(
    company: str,
    title: str,
    score: int,
    missing_qualifications: list[str],
    apply_url: str,
) -> str:
    """Format a single partial-match message — used in individual mode
    (fewer than burst_threshold partial matches in the cycle)."""
    missing = ", ".join(missing_qualifications) if missing_qualifications else "none listed"
    return (
        f"[{_APP_NAME}] Partial match: {company} · {title}\n"
        f"Score: {score} — missing: {missing}\n"
        f"Apply: {apply_url}"
    )


def format_partial_match_message(matches: list[PartialMatchInfo]) -> str:
    """
    Format a single batched message listing this cycle's partial matches —
    postings that scored below the full match threshold but close, with few
    enough missing qualifications to be a plausible resume-tweak target.
    Used in burst mode (burst_threshold or more partial matches in the cycle),
    same capped/truncated shape as format_burst_message.
    """
    ranked = sorted(matches, key=lambda m: m.score, reverse=True)
    lines = [
        f"[{_APP_NAME}] {len(ranked)} partial match{'es' if len(ranked) != 1 else ''} this cycle"
    ]

    shown = ranked[:_BURST_MAX_LINES]
    for i, m in enumerate(shown, start=1):
        missing = ", ".join(m.missing_qualifications) if m.missing_qualifications else "none listed"
        lines.append(f" {i}. {m.company} · {m.title} ({m.score}) — missing: {missing}")
        lines.append(f"    Apply: {m.apply_url}")

    remaining = len(ranked) - len(shown)
    if remaining > 0:
        lines.append(f" + {remaining} more — run `db stats` to see all")

    lines.append("Consider updating your resume/profile if you notice a pattern.")
    return "\n".join(lines)


def format_digest(
    period_label: str,
    postings_found: int,
    alerts_sent: int,
    top_match: MatchInfo | None,
    unresolved_companies: list[str],
) -> str:
    """Format the periodic digest message: stats, top match, and unresolved companies."""
    lines = [
        f"[{_APP_NAME}] Digest — {period_label}",
        f"In the last hour: {postings_found} new postings, {alerts_sent} alerts sent",
    ]
    if top_match:
        lines.append(f"Top match: {top_match.company} {top_match.title} ({top_match.score}/100)")
    if unresolved_companies:
        lines.append(f"⚠ Not found: {', '.join(unresolved_companies)}")
    return "\n".join(lines)


# ── Sending ───────────────────────────────────────────────────────────────────


def send_message(bot_token: str, chat_id: str, text: str) -> SendResult:
    """Attempt a single Telegram send. Never raises; failures are returned, not thrown."""
    try:
        resp = httpx.post(
            _API_URL.format(token=bot_token),
            json={"chat_id": chat_id, "text": text},
            timeout=10.0,
        )
        data = resp.json()
        if not data.get("ok"):
            error = data.get("description", "Unknown Telegram API error")
            _log.warning("Telegram send failed for %s: %s", _redact_chat_id(chat_id), error)
            return SendResult(success=False, message_id=None, error=error)
        return SendResult(success=True, message_id=data["result"]["message_id"], error=None)
    except Exception as exc:
        _log.warning("Telegram send failed for %s: %s", _redact_chat_id(chat_id), exc)
        return SendResult(success=False, message_id=None, error=str(exc))


def send_message_with_retry(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    retry_delay_seconds: int = _RETRY_DELAY_SECONDS,
) -> SendResult:
    """Send a message; on failure, wait once and retry. Logs a permanent failure if the retry also fails."""
    result = send_message(bot_token, chat_id, text)
    if result.success:
        return result

    _log.warning(
        "Retrying Telegram send to %s in %ds after initial failure",
        _redact_chat_id(chat_id),
        retry_delay_seconds,
    )
    time.sleep(retry_delay_seconds)
    retry_result = send_message(bot_token, chat_id, text)
    if not retry_result.success:
        _log.error(
            "Telegram send to %s permanently failed after retry: %s",
            _redact_chat_id(chat_id),
            retry_result.error,
        )
    return retry_result


# ── Public entrypoint ─────────────────────────────────────────────────────────


def notify_matches(
    bot_token: str,
    matches: list[MatchInfo],
    *,
    chat_id: str,
    burst_threshold: int,
) -> list[SendResult]:
    """
    Route matches to individual or burst mode based on burst_threshold, and send.
    Returns one SendResult per message actually sent (one for burst mode).
    Individual sends are paced to respect Telegram's per-chat rate limit.
    """
    if not matches:
        return []

    if len(matches) >= burst_threshold:
        body = format_burst_message(matches)
        return [send_message_with_retry(bot_token, chat_id, body)]

    results = []
    for i, m in enumerate(matches):
        if i > 0:
            time.sleep(_INDIVIDUAL_SEND_DELAY_SECONDS)
        results.append(
            send_message_with_retry(
                bot_token,
                chat_id,
                format_individual_message(
                    m.company, m.title, m.location, m.score, m.reasoning, m.apply_url
                ),
            )
        )
    return results


def notify_partial_matches(
    bot_token: str,
    matches: list[PartialMatchInfo],
    *,
    chat_id: str,
    burst_threshold: int,
) -> list[SendResult]:
    """
    Route partial matches to individual or burst mode based on burst_threshold,
    and send. Mirrors notify_matches — same threshold, same pacing.
    """
    if not matches:
        return []

    if len(matches) >= burst_threshold:
        body = format_partial_match_message(matches)
        return [send_message_with_retry(bot_token, chat_id, body)]

    results = []
    for i, m in enumerate(matches):
        if i > 0:
            time.sleep(_INDIVIDUAL_SEND_DELAY_SECONDS)
        results.append(
            send_message_with_retry(
                bot_token,
                chat_id,
                format_partial_match_individual_message(
                    m.company, m.title, m.score, m.missing_qualifications, m.apply_url
                ),
            )
        )
    return results
