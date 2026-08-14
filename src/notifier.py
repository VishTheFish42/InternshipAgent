"""Telegram delivery: individual and burst match alerts.

Switched from Twilio SMS after a toll-free verification rejection — Twilio's
A2P/toll-free review process is built to vet registered businesses, which a
personal single-recipient tool doesn't fit. Telegram's Bot API delivers the
same real-time push-notification experience with no business verification of
any kind: create a bot via @BotFather, get a token, done.

Messages are sent with parse_mode="HTML" for scannability (bold headers, a
score-tier emoji, a clickable Apply link) — every value substituted into a
template is run through html.escape() first, since it's either AI-generated
text or comes from a scraped posting, neither of which is safe to trust
verbatim inside HTML.
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

_log = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 4096  # Telegram's hard per-message limit
_BURST_MAX_LINES = 10
_RETRY_DELAY_SECONDS = 300
_INDIVIDUAL_SEND_DELAY_SECONDS = 1.0  # Telegram's documented per-chat rate limit is ~1 msg/sec
_API_BASE = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class MatchInfo:
    company: str
    title: str
    location: str | None
    score: int
    reasoning: str
    missing_qualifications: list[str]
    apply_url: str
    posting_id: int
    source: str = ""


# Maps a JobPosting.source prefix to a human-readable label. greenhouse/lever
# encode the company slug after the colon (e.g. "greenhouse:stripe") — not
# useful to display since the company name is already shown separately.
# jsearch encodes the aggregator's original publisher instead (e.g.
# "jsearch:LinkedIn") — that IS useful to show, so it's shown as-is.
_SOURCE_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "indeed": "Indeed",
    "hn": "HackerNews",
    "remoteok": "RemoteOK",
    "adzuna": "Adzuna",
}


def _friendly_source(source: str) -> str:
    """Human-readable label for a JobPosting.source value, e.g. 'Greenhouse'
    or 'LinkedIn' (via JSearch). Falls back to a title-cased prefix for any
    unrecognized source rather than showing the raw internal string."""
    prefix, _, rest = source.partition(":")
    if prefix == "jsearch":
        return rest or "JSearch"
    return _SOURCE_LABELS.get(prefix, prefix.title() if prefix else "Unknown")


def _score_emoji(score: int) -> str:
    """Purely a visual at-a-glance triage cue, not a filtering mechanism —
    the actual floor is min_relevance_score (profile.yaml), applied upstream
    before a posting ever reaches formatting."""
    if score >= 70:
        return "🟢"
    if score >= 40:
        return "🟡"
    return "🔴"


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
    missing_qualifications: list[str],
    apply_url: str,
    source: str = "",
) -> str:
    """Format a single-posting message as Telegram HTML for scannability —
    bold company/title header, a score-tier emoji, and a clickable Apply
    link rather than a bare pasted URL. Every scored posting gets one of
    these, regardless of score — the apply link, source, and
    missing-qualifications lines are always included verbatim; reasoning is
    truncated (after escaping) to fit _MAX_MESSAGE_CHARS.

    Every value is html.escape()'d before going into the template — company/
    title/location/reasoning/missing_qualifications are all either
    AI-generated or scraped from a third-party posting, never safe to trust
    verbatim inside HTML."""
    header = f"<b>{html.escape(company)}</b> — {html.escape(title)}"
    meta_bits = []
    if location:
        meta_bits.append(f"📍 {html.escape(location)}")
    if source:
        meta_bits.append(f"🔗 {html.escape(_friendly_source(source))}")
    meta_line = f"\n{'  ·  '.join(meta_bits)}" if meta_bits else ""

    missing = ", ".join(missing_qualifications) if missing_qualifications else "none listed"
    apply_link = f'<a href="{html.escape(apply_url, quote=True)}">Apply</a>'

    prefix = f"{header}{meta_line}\n\n{_score_emoji(score)} <b>{score}/100</b>\n"
    suffix = f"\n\n<b>Missing:</b> {html.escape(missing)}\n👉 {apply_link}"

    reasoning_escaped = html.escape(reasoning)
    budget = max(_MAX_MESSAGE_CHARS - len(prefix) - len(suffix), 0)
    if len(reasoning_escaped) > budget:
        reasoning_escaped = (
            reasoning_escaped[: max(budget - 1, 0)].rstrip() + "…" if budget > 0 else ""
        )

    return f"{prefix}{reasoning_escaped}{suffix}"


def format_burst_message(matches: list[MatchInfo]) -> str:
    """Format a batched summary message (Telegram HTML) for a cycle with many
    scored postings at once — every posting is listed, ranked highest score
    first, using the same visual language as the individual format."""
    ranked = sorted(matches, key=lambda m: m.score, reverse=True)
    n = len(ranked)
    lines = [f"<b>{n} new posting{'s' if n != 1 else ''} this cycle</b>"]

    shown = ranked[:_BURST_MAX_LINES]
    for i, m in enumerate(shown, start=1):
        loc = f" · 📍{html.escape(m.location)}" if m.location else ""
        src = f" · 🔗{html.escape(_friendly_source(m.source))}" if m.source else ""
        missing = ", ".join(m.missing_qualifications) if m.missing_qualifications else "none listed"
        apply_link = f'<a href="{html.escape(m.apply_url, quote=True)}">Apply</a>'
        lines.append(
            f"{_score_emoji(m.score)} <b>{i}. {html.escape(m.company)}</b> — "
            f"{html.escape(m.title)}{loc}{src} ({m.score})"
        )
        lines.append(f"    Missing: {html.escape(missing)} · {apply_link}")

    remaining = n - len(shown)
    if remaining > 0:
        lines.append(f"\n+ {remaining} more — run <code>db stats</code> to see all")

    return "\n".join(lines)


def applied_button(posting_id: int) -> dict[str, Any]:
    """Inline keyboard attached to individual-mode alerts so a tap marks the
    posting applied via a callback_query — no typing, no ambiguity. Only
    attached in individual mode; burst-mode summaries skip it (one button per
    line in a 20-item batch isn't worth the complexity)."""
    return {
        "inline_keyboard": [[{"text": "✅ Mark Applied", "callback_data": f"applied:{posting_id}"}]]
    }


# ── Sending ───────────────────────────────────────────────────────────────────


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = None,
) -> SendResult:
    """Attempt a single Telegram send. Never raises; failures are returned, not
    thrown. parse_mode="HTML" is opt-in per call, not default — only the
    formatted individual/burst alerts use it (their content is pre-escaped
    with html.escape()); plain confirmation texts (e.g. /pause, /resume) have
    no need for it."""
    try:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        resp = httpx.post(
            _API_BASE.format(token=bot_token, method="sendMessage"),
            json=payload,
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
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = None,
) -> SendResult:
    """Send a message; on failure, wait once and retry. Logs a permanent failure if the retry also fails."""
    result = send_message(
        bot_token, chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode
    )
    if result.success:
        return result

    _log.warning(
        "Retrying Telegram send to %s in %ds after initial failure",
        _redact_chat_id(chat_id),
        retry_delay_seconds,
    )
    time.sleep(retry_delay_seconds)
    retry_result = send_message(
        bot_token, chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode
    )
    if not retry_result.success:
        _log.error(
            "Telegram send to %s permanently failed after retry: %s",
            _redact_chat_id(chat_id),
            retry_result.error,
        )
    return retry_result


# ── Inbound updates ───────────────────────────────────────────────────────────


def get_updates(
    bot_token: str, *, offset: int | None = None, timeout: int = 0
) -> list[dict[str, Any]]:
    """Short-poll Telegram's getUpdates for new messages/callback_queries since
    `offset`. Never raises; returns [] on any failure. `offset` should be the
    last-seen update_id + 1 — Telegram then stops redelivering older updates."""
    try:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = httpx.get(
            _API_BASE.format(token=bot_token, method="getUpdates"),
            params=params,
            timeout=timeout + 10.0,
        )
        data = resp.json()
        if not data.get("ok"):
            _log.warning("Telegram getUpdates failed: %s", data.get("description"))
            return []
        result: list[dict[str, Any]] = data.get("result", [])
        return result
    except Exception as exc:
        _log.warning("Telegram getUpdates failed: %s", exc)
        return []


def answer_callback_query(
    bot_token: str, callback_query_id: str, *, text: str | None = None
) -> None:
    """Acknowledge a callback_query (removes the button's loading spinner and
    optionally shows a brief toast). Never raises."""
    try:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        httpx.post(
            _API_BASE.format(token=bot_token, method="answerCallbackQuery"),
            json=payload,
            timeout=10.0,
        )
    except Exception as exc:
        _log.warning("Telegram answerCallbackQuery failed: %s", exc)


def edit_message_mark_applied(
    bot_token: str, chat_id: str, message_id: str, original_text: str
) -> None:
    """Edit a previously-sent individual alert to visually confirm "Mark
    Applied" was recorded — the callback-query toast alone (see
    answer_callback_query) disappears in a couple seconds and leaves no trace
    once you scroll past it. Appends a confirmation line to the message text
    and removes the now-redundant button (mark_applied is idempotent, so
    leaving it live risks nothing functionally, but there's no reason to keep
    inviting a tap on a posting already marked applied).

    original_text is the exact stored message body, which is HTML (bold
    header, score emoji, Apply link) — parse_mode="HTML" must be passed here
    too, or Telegram renders the tags as literal text instead of formatting.

    Never raises. Telegram returns ok:false rather than an HTTP error for
    "message is not modified" (e.g. two near-simultaneous taps racing this
    edit) — that's treated as a harmless no-op like any other failure here."""
    try:
        resp = httpx.post(
            _API_BASE.format(token=bot_token, method="editMessageText"),
            json={
                "chat_id": chat_id,
                "message_id": int(message_id),
                "text": f"{original_text}\n\n✅ Applied",
                "reply_markup": {"inline_keyboard": []},
                "parse_mode": "HTML",
            },
            timeout=10.0,
        )
        data = resp.json()
        if not data.get("ok"):
            _log.warning("Telegram editMessageText failed: %s", data.get("description"))
    except Exception as exc:
        _log.warning("Telegram editMessageText failed: %s", exc)


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
        return [send_message_with_retry(bot_token, chat_id, body, parse_mode="HTML")]

    results = []
    for i, m in enumerate(matches):
        if i > 0:
            time.sleep(_INDIVIDUAL_SEND_DELAY_SECONDS)
        results.append(
            send_message_with_retry(
                bot_token,
                chat_id,
                format_individual_message(
                    m.company,
                    m.title,
                    m.location,
                    m.score,
                    m.reasoning,
                    m.missing_qualifications,
                    m.apply_url,
                    m.source,
                ),
                reply_markup=applied_button(m.posting_id),
                parse_mode="HTML",
            )
        )
    return results
