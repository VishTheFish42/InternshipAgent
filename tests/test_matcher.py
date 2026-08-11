"""Unit tests for matcher — all Claude calls are mocked."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.matcher import (
    ScoredPosting,
    _batches,
    _estimate_cost,
    _parse_batch_response,
    _posting_block,
    build_profile_summary,
    hash_profile,
    score_postings,
)
from src.scrapers.base import RawPosting

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _posting(external_id: str = "1", description: str = "desc") -> RawPosting:
    return RawPosting(
        source="greenhouse:stripe",
        external_id=external_id,
        title="Software Engineering Intern",
        company="Stripe",
        location="Remote",
        is_remote=True,
        url="https://boards.greenhouse.io/stripe/jobs/1",
        apply_url="https://boards.greenhouse.io/stripe/jobs/1",
        description=description,
        posted_at=None,
    )


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str, input_tokens: int = 100, output_tokens: int = 20) -> None:
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)


def _score_json(external_ids: list[str]) -> str:
    return json.dumps(
        [{"external_id": eid, "score": 80, "reasoning": "Good fit."} for eid in external_ids]
    )


def _settings() -> Settings:
    return Settings(anthropic_api_key="test-key", claude_scoring_model="claude-haiku-4-5-20251001")


# ── build_profile_summary ────────────────────────────────────────────────────


def test_build_profile_summary_includes_core_fields():
    summary = build_profile_summary(
        {
            "experience_level": "sophomore",
            "graduation_year": 2027,
            "languages": ["Python"],
            "frameworks": ["FastAPI"],
            "tools": ["Docker"],
        },
        {},
    )
    assert "sophomore" in summary
    assert "2027" in summary
    assert "Python" in summary
    assert "FastAPI" in summary
    assert "Docker" in summary


def test_build_profile_summary_includes_projects_and_experience():
    summary = build_profile_summary(
        {
            "projects": [{"name": "Image Classifier", "description": "CNN on CIFAR-10."}],
            "work_experience": [
                {"title": "SWE Intern", "company": "Acme", "description": "Built APIs."}
            ],
        },
        {},
    )
    assert "Image Classifier" in summary
    assert "SWE Intern" in summary
    assert "Acme" in summary


def test_build_profile_summary_includes_preferences():
    summary = build_profile_summary(
        {}, {"locations": ["United States"], "keywords_excluded": ["senior"]}
    )
    assert "United States" in summary
    assert "senior" in summary


def test_build_profile_summary_handles_empty_profile():
    summary = build_profile_summary({}, {})
    assert "unknown" in summary
    assert "none listed" in summary


# ── hash_profile ──────────────────────────────────────────────────────────────


def test_hash_profile_deterministic():
    profile = {"languages": ["Python"], "graduation_year": 2027}
    assert hash_profile(profile) == hash_profile(profile)


def test_hash_profile_order_independent():
    a = {"languages": ["Python"], "graduation_year": 2027}
    b = {"graduation_year": 2027, "languages": ["Python"]}
    assert hash_profile(a) == hash_profile(b)


def test_hash_profile_changes_on_content_change():
    a = {"languages": ["Python"]}
    b = {"languages": ["Python", "Go"]}
    assert hash_profile(a) != hash_profile(b)


# ── _batches ──────────────────────────────────────────────────────────────────


def test_batches_splits_into_groups_of_ten():
    postings = [_posting(str(i)) for i in range(25)]
    batches = _batches(postings)
    assert [len(b) for b in batches] == [10, 10, 5]


def test_batches_empty_list():
    assert _batches([]) == []


def test_batches_single_partial_batch():
    postings = [_posting(str(i)) for i in range(3)]
    assert _batches(postings) == [postings]


# ── _posting_block ────────────────────────────────────────────────────────────


def test_posting_block_includes_all_fields():
    block = _posting_block(_posting("42", "A great backend role."))
    assert "external_id: 42" in block
    assert "title: Software Engineering Intern" in block
    assert "company: Stripe" in block
    assert "A great backend role." in block


def test_posting_block_truncates_long_description():
    block = _posting_block(_posting("1", "z" * 2000))
    assert block.count("z") == 1000


# ── _parse_batch_response ────────────────────────────────────────────────────


def test_parse_batch_response_parses_scores():
    raw = json.dumps([{"external_id": "1", "score": 88, "reasoning": "Strong fit."}])
    scored = _parse_batch_response(raw)
    assert scored == [ScoredPosting(external_id="1", score=88, reasoning="Strong fit.")]


def test_parse_batch_response_clamps_score_above_100():
    raw = json.dumps([{"external_id": "1", "score": 150, "reasoning": "x"}])
    assert _parse_batch_response(raw)[0].score == 100


def test_parse_batch_response_clamps_score_below_0():
    raw = json.dumps([{"external_id": "1", "score": -20, "reasoning": "x"}])
    assert _parse_batch_response(raw)[0].score == 0


def test_parse_batch_response_strips_markdown_fences():
    raw = "```json\n" + json.dumps([{"external_id": "1", "score": 50, "reasoning": "x"}]) + "\n```"
    assert _parse_batch_response(raw)[0].external_id == "1"


def test_parse_batch_response_defaults_missing_reasoning():
    raw = json.dumps([{"external_id": "1", "score": 50}])
    assert _parse_batch_response(raw)[0].reasoning == ""


def test_parse_batch_response_parses_missing_qualifications():
    raw = json.dumps(
        [
            {
                "external_id": "1",
                "score": 60,
                "reasoning": "Close but missing a few things.",
                "missing_qualifications": ["Kubernetes", "GraphQL"],
            }
        ]
    )
    assert _parse_batch_response(raw)[0].missing_qualifications == ["Kubernetes", "GraphQL"]


def test_parse_batch_response_defaults_missing_qualifications_to_empty_list():
    raw = json.dumps([{"external_id": "1", "score": 90, "reasoning": "Great fit."}])
    assert _parse_batch_response(raw)[0].missing_qualifications == []


def test_parse_batch_response_handles_null_missing_qualifications():
    raw = json.dumps(
        [{"external_id": "1", "score": 90, "reasoning": "x", "missing_qualifications": None}]
    )
    assert _parse_batch_response(raw)[0].missing_qualifications == []


# ── _estimate_cost ────────────────────────────────────────────────────────────


def test_estimate_cost_known_model():
    cost = _estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.80 + 4.00)


def test_estimate_cost_unknown_model_falls_back_to_default_pricing():
    cost = _estimate_cost("some-future-model", 1_000_000, 1_000_000)
    assert cost == pytest.approx(3.00 + 15.00)


def test_estimate_cost_zero_tokens():
    assert _estimate_cost("claude-haiku-4-5-20251001", 0, 0) == 0.0


# ── score_postings ────────────────────────────────────────────────────────────


def test_score_postings_empty_list_does_not_call_claude():
    with patch("src.matcher.Anthropic") as mock_anthropic:
        result = score_postings([], {}, {}, settings=_settings())
    mock_anthropic.assert_not_called()
    assert result.scored == []
    assert result.estimated_cost_usd == 0.0


def test_score_postings_single_batch():
    postings = [_posting("1"), _posting("2")]
    fake_message = _FakeMessage(_score_json(["1", "2"]), input_tokens=500, output_tokens=40)

    mock_client = MagicMock()
    mock_client.messages.create.return_value = fake_message

    with patch("src.matcher.Anthropic", return_value=mock_client):
        result = score_postings(postings, {}, {}, settings=_settings())

    assert {s.external_id for s in result.scored} == {"1", "2"}
    assert all(s.score == 80 for s in result.scored)
    assert result.input_tokens == 500
    assert result.output_tokens == 40
    mock_client.messages.create.assert_called_once()


def test_score_postings_propagates_missing_qualifications():
    postings = [_posting("1")]
    raw = json.dumps(
        [
            {
                "external_id": "1",
                "score": 60,
                "reasoning": "Close.",
                "missing_qualifications": ["Kubernetes", "GraphQL"],
            }
        ]
    )
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _FakeMessage(raw)

    with patch("src.matcher.Anthropic", return_value=mock_client):
        result = score_postings(postings, {}, {}, settings=_settings())

    assert result.scored[0].missing_qualifications == ["Kubernetes", "GraphQL"]


def test_score_postings_splits_across_multiple_batches():
    postings = [_posting(str(i)) for i in range(12)]
    batch1_ids = [str(i) for i in range(10)]
    batch2_ids = [str(i) for i in range(10, 12)]

    responses = [
        _FakeMessage(_score_json(batch1_ids), input_tokens=300, output_tokens=30),
        _FakeMessage(_score_json(batch2_ids), input_tokens=100, output_tokens=10),
    ]

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = responses

    with patch("src.matcher.Anthropic", return_value=mock_client):
        result = score_postings(postings, {}, {}, settings=_settings())

    assert {s.external_id for s in result.scored} == set(batch1_ids) | set(batch2_ids)
    assert mock_client.messages.create.call_count == 2
    assert result.input_tokens == 400
    assert result.output_tokens == 40


def test_score_postings_skips_failing_batch_and_continues():
    postings = [_posting(str(i)) for i in range(11)]  # 2 batches: 10 + 1

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        RuntimeError("API error"),
        _FakeMessage(_score_json(["10"]), input_tokens=50, output_tokens=5),
    ]

    with patch("src.matcher.Anthropic", return_value=mock_client):
        result = score_postings(postings, {}, {}, settings=_settings())

    assert {s.external_id for s in result.scored} == {"10"}
    assert result.input_tokens == 50
    assert result.output_tokens == 5


def test_score_postings_skips_batch_with_unparseable_response():
    postings = [_posting("1")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _FakeMessage("not valid json")

    with patch("src.matcher.Anthropic", return_value=mock_client):
        result = score_postings(postings, {}, {}, settings=_settings())

    assert result.scored == []


def test_score_postings_computes_cost_estimate():
    postings = [_posting("1")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _FakeMessage(
        _score_json(["1"]), input_tokens=1_000_000, output_tokens=1_000_000
    )

    with patch("src.matcher.Anthropic", return_value=mock_client):
        result = score_postings(postings, {}, {}, settings=_settings())

    assert result.estimated_cost_usd == pytest.approx(0.80 + 4.00)


def test_score_postings_uses_cache_control_on_system_prompt():
    postings = [_posting("1")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _FakeMessage(_score_json(["1"]))

    with patch("src.matcher.Anthropic", return_value=mock_client):
        score_postings(postings, {}, {}, settings=_settings())

    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
