"""Integration test for rebuild_profile: requires ANTHROPIC_API_KEY."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.resume_extractor import rebuild_profile

# ── PDF fixture helpers ───────────────────────────────────────────────────────

_RESUME_LINES = [
    "Jane Doe - Software Engineering Student",
    "Graduation: May 2027  (Junior year)",
    "",
    "Languages: Python  JavaScript  TypeScript  SQL",
    "Frameworks: React  FastAPI  PyTorch  scikit-learn",
    "Tools: Docker  Git  PostgreSQL  AWS S3",
    "",
    "Projects",
    "ML Image Classifier: CNN trained on CIFAR-10 achieving 94% accuracy using PyTorch and AWS.",
    "Task Dashboard: Full-stack web app built with React and FastAPI for project management.",
    "",
    "Work Experience",
    "Software Engineering Intern  Acme Corp  Summer 2024",
    "Built REST APIs with FastAPI and reduced endpoint latency by 40%.",
]


def _make_resume_pdf(path: Path) -> None:
    """Write a minimal valid single-page PDF that pdfplumber can read."""
    # Build the content stream using Tm (absolute text matrix) for each line
    stream_parts: list[bytes] = [b"BT\n/F1 10 Tf\n"]
    y = 750
    for line in _RESUME_LINES:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_parts.append(f"1 0 0 1 50 {y} Tm ({escaped}) Tj\n".encode("latin-1"))
        y -= 14
    stream_parts.append(b"ET\n")
    stream = b"".join(stream_parts)

    obj1 = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    obj2 = b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    obj3 = (
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]\n"
        b"   /Contents 4 0 R\n"
        b"   /Resources << /Font << /F1 << /Type /Font /Subtype /Type1"
        b" /BaseFont /Helvetica >> >> >> >>\nendobj\n"
    )
    obj4 = (
        b"4 0 obj\n<< /Length " + str(len(stream)).encode() + b" >>\n"
        b"stream\n" + stream + b"endstream\nendobj\n"
    )

    header = b"%PDF-1.4\n"
    offsets: list[int] = []
    body = b""
    for obj in (obj1, obj2, obj3, obj4):
        offsets.append(len(header) + len(body))
        body += obj

    xref_offset = len(header) + len(body)
    xref = b"xref\n0 5\n0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()

    trailer = (
        b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )

    path.write_bytes(header + body + xref + trailer)


# ── Test ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_rebuild_profile_produces_cache_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resumes_dir = tmp_path / "resumes"
    resumes_dir.mkdir()
    _make_resume_pdf(resumes_dir / "sample_resume.pdf")

    # rebuild_profile writes profile.cache.json to cwd
    monkeypatch.chdir(tmp_path)

    merged = rebuild_profile(resumes_dir)

    # Cache file must exist
    cache_path = tmp_path / "profile.cache.json"
    assert cache_path.exists(), "profile.cache.json was not written"

    data = json.loads(cache_path.read_text(encoding="utf-8"))

    # All expected top-level keys present
    required_keys = {
        "languages",
        "frameworks",
        "tools",
        "experience_level",
        "graduation_year",
        "projects",
        "work_experience",
        "source_hash",
    }
    assert required_keys <= data.keys()

    # source_hash is a SHA-256 hex digest
    assert len(data["source_hash"]) == 64
    assert all(c in "0123456789abcdef" for c in data["source_hash"])

    # Claude should have picked up Python from the resume text
    assert any("python" in lang.lower() for lang in data["languages"]), (
        f"Expected Python in languages, got: {data['languages']}"
    )

    # Return value must match what was written to disk
    assert merged.source_hash == data["source_hash"]

    # Railway command must have been printed to stdout
    out = capsys.readouterr().out
    assert "railway variables set PROFILE_CACHE=" in out
