"""Resume PDF extraction: pdfplumber for text, Claude Sonnet for structured parsing."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
from anthropic import Anthropic
from anthropic.types import TextBlock

from src.config import get_settings

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class Project:
    name: str
    description: str
    technologies: list[str] = field(default_factory=list)


@dataclass
class WorkExperience:
    title: str
    company: str
    description: str


@dataclass
class ExtractedProfile:
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    experience_level: str = ""
    graduation_year: int | None = None
    projects: list[Project] = field(default_factory=list)
    work_experience: list[WorkExperience] = field(default_factory=list)
    source_file: str = field(default="", compare=False)


@dataclass
class MergedProfile:
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    experience_level: str = ""
    graduation_year: int | None = None
    projects: list[Project] = field(default_factory=list)
    work_experience: list[WorkExperience] = field(default_factory=list)
    source_hash: str = field(default="", compare=False)  # set by rebuild_profile (T-204)


_log = logging.getLogger(__name__)


# ── Extraction prompt (T-202) ─────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a resume parser. Extract structured information from the resume text provided.
Return ONLY valid JSON matching this exact schema — no extra text, no markdown fences:

{
  "languages": ["Python", "JavaScript", "Java"],
  "frameworks": ["React", "FastAPI", "PyTorch", "scikit-learn"],
  "tools": ["Docker", "Git", "AWS S3", "PostgreSQL"],
  "experience_level": "sophomore",
  "graduation_year": 2027,
  "projects": [
    {
      "name": "Image Classifier",
      "description": "CNN trained on CIFAR-10 achieving 94% top-1 accuracy.",
      "technologies": ["Python", "PyTorch", "AWS S3"]
    }
  ],
  "work_experience": [
    {
      "title": "Software Engineering Intern",
      "company": "Acme Corp",
      "description": "Built REST APIs using FastAPI; reduced latency by 40%."
    }
  ]
}

Field rules:
- languages: programming languages only (Python, Java, C++, JavaScript, TypeScript, Go, Rust, SQL, etc.)
- frameworks: libraries and frameworks (React, FastAPI, PyTorch, TensorFlow, Django, Spring, etc.)
- tools: infrastructure, DevOps, databases, cloud services, and other non-language/non-framework tools
- experience_level: one of "freshman", "sophomore", "junior", "senior", "graduate"; infer from \
year in school or graduation date; use "junior" if unclear
- graduation_year: 4-digit integer; null if not found
- projects: all personal, academic, and open-source projects listed on the resume
- work_experience: internships, part-time roles, and research positions; omit co-curriculars
- Never list the same item in multiple lists (e.g. Docker belongs in tools, not frameworks)
- Return empty lists [] for fields with no matching content; return null for graduation_year if absent
"""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _extract_text(path: Path) -> str:
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences if the model wraps the JSON anyway."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Drop opening fence (```json or ```) and closing fence (```)
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        stripped = "\n".join(inner).strip()
    return stripped


def _parse_response(raw: str, source_file: str) -> ExtractedProfile:
    data = json.loads(_strip_fences(raw))
    return ExtractedProfile(
        languages=data.get("languages") or [],
        frameworks=data.get("frameworks") or [],
        tools=data.get("tools") or [],
        experience_level=data.get("experience_level") or "junior",
        graduation_year=data.get("graduation_year"),
        projects=[
            Project(
                name=p["name"],
                description=p.get("description") or "",
                technologies=p.get("technologies") or [],
            )
            for p in (data.get("projects") or [])
        ],
        work_experience=[
            WorkExperience(
                title=w["title"],
                company=w.get("company") or "",
                description=w.get("description") or "",
            )
            for w in (data.get("work_experience") or [])
        ],
        source_file=source_file,
    )


# ── Merge helpers (T-203) ────────────────────────────────────────────────────


def _norm(s: str) -> str:
    """Lowercase and strip punctuation — used as a dedup key only, not for display."""
    return re.sub(r"[^\w\s]", "", s).lower().strip()


def _dedup_strings(lists: list[list[str]]) -> list[str]:
    """Union of string lists with case-insensitive dedup; first-seen casing is kept."""
    seen: dict[str, str] = {}  # normalized key → original value
    for lst in lists:
        for item in lst:
            key = item.lower().strip()
            if key not in seen:
                seen[key] = item
    return list(seen.values())


def _merge_projects(lists: list[list[Project]]) -> list[Project]:
    """Dedup by normalized project name; longest description wins."""
    merged: dict[str, Project] = {}
    for projects in lists:
        for p in projects:
            key = _norm(p.name)
            if key not in merged or len(p.description) > len(merged[key].description):
                merged[key] = p
    return list(merged.values())


def _merge_work_experience(lists: list[list[WorkExperience]]) -> list[WorkExperience]:
    """Dedup by (normalized title, normalized company); longest description wins."""
    merged: dict[tuple[str, str], WorkExperience] = {}
    for experiences in lists:
        for w in experiences:
            key = (_norm(w.title), _norm(w.company))
            if key not in merged or len(w.description) > len(merged[key].description):
                merged[key] = w
    return list(merged.values())


def merge_profiles(profiles: list[ExtractedProfile]) -> MergedProfile:
    """Merge per-PDF profiles into a single MergedProfile.

    Profiles should be ordered oldest-first; the last entry is treated as most
    recent and wins any scalar conflicts (experience_level, graduation_year).
    """
    if not profiles:
        return MergedProfile()

    merged = MergedProfile(
        languages=_dedup_strings([p.languages for p in profiles]),
        frameworks=_dedup_strings([p.frameworks for p in profiles]),
        tools=_dedup_strings([p.tools for p in profiles]),
        projects=_merge_projects([p.projects for p in profiles]),
        work_experience=_merge_work_experience([p.work_experience for p in profiles]),
        experience_level=profiles[-1].experience_level,
        graduation_year=profiles[-1].graduation_year,
    )

    distinct_levels = {p.experience_level for p in profiles if p.experience_level}
    if len(distinct_levels) > 1:
        _log.warning(
            "Conflicting experience_level across resumes %s — using most recent: %r",
            distinct_levels,
            merged.experience_level,
        )

    distinct_years = {p.graduation_year for p in profiles if p.graduation_year is not None}
    if len(distinct_years) > 1:
        _log.warning(
            "Conflicting graduation_year across resumes %s — using most recent: %s",
            distinct_years,
            merged.graduation_year,
        )

    return merged


# ── Shared helpers ───────────────────────────────────────────────────────────


def _hash_pdfs(pdf_files: list[Path]) -> str:
    """SHA-256 of all PDF contents concatenated in sorted-name order."""
    hasher = hashlib.sha256()
    for p in sorted(pdf_files):
        hasher.update(p.read_bytes())
    return hasher.hexdigest()


# ── Public API ────────────────────────────────────────────────────────────────


def rebuild_profile(resumes_dir: Path) -> MergedProfile:
    """Extract from every PDF in resumes_dir, merge, and write profile.cache.json.

    PDFs are hashed (sorted by name) to produce source_hash. Extraction order is
    oldest-mtime-first so the most recent file wins scalar conflicts in merge_profiles.

    Prints the Railway CLI command to stdout so the caller can update PROFILE_CACHE.
    Raises ValueError if no PDFs are found or none can be extracted.
    """
    pdf_files = sorted(resumes_dir.glob("*.pdf"))
    if not pdf_files:
        raise ValueError(f"No PDF files found in {resumes_dir}")

    source_hash = _hash_pdfs(pdf_files)

    # Extract profiles ordered oldest-first by modification time
    by_mtime = sorted(pdf_files, key=lambda p: p.stat().st_mtime)
    profiles: list[ExtractedProfile] = []
    for p in by_mtime:
        try:
            profiles.append(extract_from_pdf(p))
        except Exception as exc:
            _log.warning("Skipping %s — extraction failed: %s", p.name, exc)

    if not profiles:
        raise ValueError(f"No profiles could be extracted from PDFs in {resumes_dir}")

    merged = merge_profiles(profiles)
    merged.source_hash = source_hash

    cache_json = json.dumps(dataclasses.asdict(merged), indent=2)
    Path("profile.cache.json").write_text(cache_json, encoding="utf-8")
    _log.info("Wrote profile.cache.json (%d bytes)", len(cache_json))

    encoded = base64.b64encode(cache_json.encode()).decode()
    print(f'railway variables set PROFILE_CACHE="{encoded}"')

    return merged


def detect_resume_changes(resumes_dir: Path, cache: MergedProfile) -> bool:
    """Return True if the PDFs in resumes_dir differ from what was last hashed into cache.

    Returns False if cache.source_hash is empty (no prior rebuild recorded).
    """
    if not cache.source_hash:
        return False
    current_hash = _hash_pdfs(list(resumes_dir.glob("*.pdf")))
    return current_hash != cache.source_hash


def extract_from_pdf(path: Path) -> ExtractedProfile:
    """Extract a structured profile from a single resume PDF.

    Raises ValueError if no text can be extracted from the PDF.
    """
    resume_text = _extract_text(path)
    if not resume_text.strip():
        raise ValueError(f"No text could be extracted from {path}")

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=settings.claude_extraction_model,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Extract structured information from this resume:\n\n{resume_text}",
            }
        ],
    )

    block = message.content[0]
    if not isinstance(block, TextBlock):
        raise RuntimeError(f"Unexpected response block type from Claude: {type(block)}")
    return _parse_response(block.text, source_file=str(path))
