"""Settings for the multi-tenant web app (T-1101) — deliberately separate from
src/config.py's Settings, which drives the existing single-user worker. Keeping
them apart means running the worker never requires SUPABASE_URL/etc. to be set,
and vice versa; the two processes are independently deployable (design.md §14).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Supabase project URL and anon (public) API key — Settings → API in the
    # Supabase dashboard. The anon key is safe to use from a server process
    # (it's the same key Supabase's own client-side JS SDK ships to browsers);
    # it does not grant elevated access, auth endpoints still enforce it per-user.
    supabase_url: str
    supabase_anon_key: str

    # Signs the session cookie (Starlette SessionMiddleware) — generate with
    # `python -c "import secrets; print(secrets.token_hex(32))"`. Rotating this
    # invalidates every existing session.
    session_secret: str


@lru_cache(maxsize=1)
def get_web_settings() -> WebSettings:
    try:
        return WebSettings()
    except ValidationError as exc:
        missing = [str(err["loc"][0]).upper() for err in exc.errors() if err["type"] == "missing"]
        if missing:
            raise SystemExit(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Copy .env.example to .env and fill in the missing values "
                "(SUPABASE_URL, SUPABASE_ANON_KEY, SESSION_SECRET)."
            ) from exc
        raise
