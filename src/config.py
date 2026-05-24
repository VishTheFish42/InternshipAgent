from __future__ import annotations

import base64
import json
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Required — agent cannot function without these
    anthropic_api_key: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    alert_phone_number: str

    # Optional API keys (scrapers are skipped when absent)
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    wellfound_api_key: str | None = None
    search_api_key: str | None = None  # SerpAPI — used for company discovery only

    # Profile: base64-encoded profile.cache.json for cloud; falls back to local file for dev
    profile_cache: str | None = None

    # Infrastructure
    database_url: str = "sqlite:///./internship_agent.db"

    # Scheduler
    run_interval_minutes: int = 30

    # Notifications
    burst_threshold: int = 5

    # Claude models
    claude_scoring_model: str = "claude-haiku-4-5"
    claude_extraction_model: str = "claude-sonnet-4-6"

    def load_profile(self) -> dict:
        """Return the merged profile dict from PROFILE_CACHE env var or local profile.cache.json."""
        if self.profile_cache:
            raw = base64.b64decode(self.profile_cache).decode("utf-8")
            return json.loads(raw)
        cache_path = Path("profile.cache.json")
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
        return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing = [err["loc"][0].upper() for err in exc.errors() if err["type"] == "missing"]
        if missing:
            raise SystemExit(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Copy .env.example to .env and fill in the missing values."
            ) from exc
        raise
