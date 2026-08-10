"""AI-driven job posting scorer: batched Claude calls with a cached system prompt."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from src.config import Settings, get_settings
from src.scrapers.base import RawPosting

_log = logging.getLogger(__name__)

_BATCH_SIZE = 10
_MAX_DESCRIPTION_CHARS = 1000

# USD per million tokens — used only for the estimated_cost_usd figure logged per run.
_MODEL_PRICING_PER_MTOK = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}
_DEFAULT_PRICING = {"input": 3.00, "output": 15.00}

_SYSTEM_PROMPT_TEMPLATE = """\
You are scoring internship postings for fit against this candidate profile:

{profile_summary}

Evaluate each posting on:
- Skills alignment (40%): does the posting require skills the candidate has?
- Domain relevance (30%): is this a SWE / DS / ML / AI role?
- Seniority fit (20%): is this appropriate for the candidate's experience level?
- Role accessibility (10%): are there gatekeeping requirements (PhD, security clearance, \
2+ years experience) the candidate doesn't meet?

Score each posting 0-100. Return ONLY a valid JSON array, no extra text, no markdown fences:
[{{"external_id": "...", "score": 0-100, "reasoning": "1-2 sentence explanation"}}]
"""


@dataclass
class ScoredPosting:
    external_id: str
    score: int
    reasoning: str


@dataclass
class ScoringResult:
    scored: list[ScoredPosting]
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


# ── Profile rendering ─────────────────────────────────────────────────────────


def build_profile_summary(profile: dict[str, Any], preferences: dict[str, Any]) -> str:
    """Render the merged profile + preferences into a compact, cacheable text block."""
    lines = [
        f"Experience level: {profile.get('experience_level') or 'unknown'}",
        f"Graduation year: {profile.get('graduation_year') or 'unknown'}",
        f"Languages: {', '.join(profile.get('languages') or []) or 'none listed'}",
        f"Frameworks: {', '.join(profile.get('frameworks') or []) or 'none listed'}",
        f"Tools: {', '.join(profile.get('tools') or []) or 'none listed'}",
    ]

    projects = profile.get("projects") or []
    if projects:
        lines.append("Projects:")
        lines.extend(f"  - {p['name']}: {p.get('description', '')}" for p in projects)

    experience = profile.get("work_experience") or []
    if experience:
        lines.append("Work experience:")
        lines.extend(
            f"  - {w['title']} at {w.get('company', '')}: {w.get('description', '')}"
            for w in experience
        )

    locations = preferences.get("locations") or []
    if locations:
        lines.append(f"Preferred locations: {', '.join(locations)}")

    excluded = preferences.get("keywords_excluded") or []
    if excluded:
        lines.append(f"Automatically excluded keywords: {', '.join(excluded)}")

    return "\n".join(lines)


def hash_profile(profile: dict[str, Any]) -> str:
    """Stable hash of the profile dict, used to detect changes between runs."""
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


# ── Batching & prompt formatting ─────────────────────────────────────────────


def _batches(postings: list[RawPosting], size: int = _BATCH_SIZE) -> list[list[RawPosting]]:
    return [postings[i : i + size] for i in range(0, len(postings), size)]


def _posting_block(posting: RawPosting) -> str:
    description = (posting.description or "")[:_MAX_DESCRIPTION_CHARS]
    return (
        f"external_id: {posting.external_id}\n"
        f"title: {posting.title}\n"
        f"company: {posting.company}\n"
        f"description: {description}"
    )


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences if the model wraps the JSON anyway."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        stripped = "\n".join(inner).strip()
    return stripped


def _parse_batch_response(raw: str) -> list[ScoredPosting]:
    data = json.loads(_strip_fences(raw))
    return [
        ScoredPosting(
            external_id=str(item["external_id"]),
            score=max(0, min(100, int(item["score"]))),
            reasoning=item.get("reasoning", ""),
        )
        for item in data
    ]


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = _MODEL_PRICING_PER_MTOK.get(model, _DEFAULT_PRICING)
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def _call_claude_batch(
    client: Anthropic, system_prompt: str, batch: list[RawPosting], model: str
) -> tuple[list[ScoredPosting], int, int]:
    user_content = "\n\n---\n\n".join(_posting_block(p) for p in batch)
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )
    block = message.content[0]
    text = getattr(block, "text", None)
    if text is None:
        raise RuntimeError(f"Unexpected response block type from Claude: {type(block)}")
    scored = _parse_batch_response(text)
    return scored, message.usage.input_tokens, message.usage.output_tokens


# ── Public API ────────────────────────────────────────────────────────────────


def score_postings(
    postings: list[RawPosting],
    profile: dict[str, Any],
    preferences: dict[str, Any],
    settings: Settings | None = None,
) -> ScoringResult:
    """
    Score postings against the candidate profile using Claude, in batches of 10
    with a cached system prompt. A batch that fails to call or parse is logged
    and skipped rather than failing the whole run.
    """
    if not postings:
        return ScoringResult(scored=[], input_tokens=0, output_tokens=0, estimated_cost_usd=0.0)

    settings = settings or get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    model = settings.claude_scoring_model
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        profile_summary=build_profile_summary(profile, preferences)
    )

    all_scored: list[ScoredPosting] = []
    total_input = 0
    total_output = 0

    for batch in _batches(postings):
        try:
            scored, input_tokens, output_tokens = _call_claude_batch(
                client, system_prompt, batch, model
            )
        except Exception as exc:
            _log.warning("Skipping batch of %d postings — scoring failed: %s", len(batch), exc)
            continue
        all_scored.extend(scored)
        total_input += input_tokens
        total_output += output_tokens

    return ScoringResult(
        scored=all_scored,
        input_tokens=total_input,
        output_tokens=total_output,
        estimated_cost_usd=_estimate_cost(model, total_input, total_output),
    )
