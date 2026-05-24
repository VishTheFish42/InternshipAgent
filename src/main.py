"""InternshipAgent entry point.  Full orchestration added in T-701."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.resume_extractor import rebuild_profile

_RESUMES_DIR = Path("resumes")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="InternshipAgent — monitors job boards and sends SMS alerts.",
    )
    parser.add_argument(
        "--rebuild-profile",
        action="store_true",
        help=(
            "Extract structured profile from all PDFs in /resumes/, write "
            "profile.cache.json, and print the railway variables set command."
        ),
    )
    # --run-once, --dry-run, --rescore added in T-701
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.rebuild_profile:
        rebuild_profile(_RESUMES_DIR)
        sys.exit(0)


if __name__ == "__main__":
    main()
