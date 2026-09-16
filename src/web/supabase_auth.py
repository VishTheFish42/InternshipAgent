"""Thin wrapper around Supabase's GoTrue (Auth) REST API — plain httpx calls,
not the supabase-py SDK, mirroring notifier.py's existing "plain HTTP POST, no
SDK needed for a single-endpoint integration this simple" choice for Telegram.
Reference: https://supabase.com/docs/reference/auth

Only the handful of endpoints the sign-up/sign-in/session web app (T-1101)
actually needs are wrapped here: sign up, sign in with password, fetch the
current user, refresh an expired access token, and sign out.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

import httpx


class SupabaseAuthError(Exception):
    """Raised for any non-2xx response from a Supabase Auth call. Carries the
    provider's own message where available so routes can show it to the user
    (e.g. "Invalid login credentials", "User already registered")."""


@dataclass
class AuthUser:
    id: str
    email: str


@dataclass
class AuthSession:
    access_token: str
    refresh_token: str
    user: AuthUser


def _extract_error_message(response: httpx.Response) -> str:
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        return f"Auth request failed ({response.status_code})"
    return str(
        body.get("msg")
        or body.get("error_description")
        or body.get("error")
        or body.get("message")
        or f"Auth request failed ({response.status_code})"
    )


def _session_from_token_response(body: dict[str, Any]) -> AuthSession:
    user = body["user"]
    return AuthSession(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        user=AuthUser(id=user["id"], email=user["email"]),
    )


class SupabaseAuthClient:
    def __init__(self, base_url: str, anon_key: str) -> None:
        self._auth_url = base_url.rstrip("/") + "/auth/v1"
        self._anon_key = anon_key

    def _headers(self, *, bearer: str | None = None) -> dict[str, str]:
        headers = {"apikey": self._anon_key, "Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {bearer or self._anon_key}"
        return headers

    def sign_up(self, email: str, password: str) -> AuthSession | None:
        """Returns an AuthSession if the project auto-confirms new users, or
        None if Supabase requires email confirmation before a session is
        issued (the caller should show a "check your email" message in that
        case) — both are successful outcomes, distinguished only by whether
        access_token is present in the response body."""
        response = httpx.post(
            f"{self._auth_url}/signup",
            headers=self._headers(),
            json={"email": email, "password": password},
            timeout=10.0,
        )
        if response.status_code >= 400:
            raise SupabaseAuthError(_extract_error_message(response))
        body = response.json()
        if not body.get("access_token"):
            return None
        return _session_from_token_response(body)

    def sign_in(self, email: str, password: str) -> AuthSession:
        response = httpx.post(
            f"{self._auth_url}/token",
            params={"grant_type": "password"},
            headers=self._headers(),
            json={"email": email, "password": password},
            timeout=10.0,
        )
        if response.status_code >= 400:
            raise SupabaseAuthError(_extract_error_message(response))
        return _session_from_token_response(response.json())

    def refresh(self, refresh_token: str) -> AuthSession:
        response = httpx.post(
            f"{self._auth_url}/token",
            params={"grant_type": "refresh_token"},
            headers=self._headers(),
            json={"refresh_token": refresh_token},
            timeout=10.0,
        )
        if response.status_code >= 400:
            raise SupabaseAuthError(_extract_error_message(response))
        return _session_from_token_response(response.json())

    def get_user(self, access_token: str) -> AuthUser:
        """Verifies the access token against Supabase (not just decoded
        locally) so a revoked or expired token is rejected even though the
        session cookie itself is tamper-proof (signed, not just opaque)."""
        response = httpx.get(
            f"{self._auth_url}/user",
            headers=self._headers(bearer=access_token),
            timeout=10.0,
        )
        if response.status_code >= 400:
            raise SupabaseAuthError(_extract_error_message(response))
        body = response.json()
        return AuthUser(id=body["id"], email=body["email"])

    def sign_out(self, access_token: str) -> None:
        """Best-effort — revokes the refresh token server-side. Failures are
        swallowed by the caller (same pattern as every other outbound
        best-effort call in this codebase, e.g. notifier.py's Telegram edits):
        the session cookie is cleared client-side regardless, so a failed
        revoke just means the old token remains valid until it naturally
        expires, not that sign-out silently fails from the user's view."""
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(
                f"{self._auth_url}/logout",
                headers=self._headers(bearer=access_token),
                timeout=10.0,
            )
