"""Unit tests for the web app's sign-up/sign-in/session flow (T-1101). No real
network calls — SupabaseAuthClient is swapped for a fake via FastAPI's
dependency_overrides, same idea as mocking send_message() for notifier tests."""

from __future__ import annotations

import os

# WebSettings is validated at import time (src/web/app.py instantiates it at
# module scope) — must be set before importing the app below.
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")

from fastapi.testclient import TestClient  # noqa: E402

from src.web.app import app, get_auth_client  # noqa: E402
from src.web.supabase_auth import AuthSession, AuthUser, SupabaseAuthError  # noqa: E402

# ── Fake auth client ─────────────────────────────────────────────────────────


class FakeAuthClient:
    def __init__(self) -> None:
        self.users: dict[str, tuple[str, str]] = {}  # email -> (password, user_id)
        self.confirmation_required = False
        self._next_user_id = 1
        self.signed_out_tokens: list[str] = []

    def sign_up(self, email: str, password: str) -> AuthSession | None:
        if email in self.users:
            raise SupabaseAuthError("User already registered")
        user_id = str(self._next_user_id)
        self._next_user_id += 1
        self.users[email] = (password, user_id)
        if self.confirmation_required:
            return None
        return AuthSession(f"access-{user_id}", f"refresh-{user_id}", AuthUser(user_id, email))

    def sign_in(self, email: str, password: str) -> AuthSession:
        record = self.users.get(email)
        if record is None or record[0] != password:
            raise SupabaseAuthError("Invalid login credentials")
        user_id = record[1]
        return AuthSession(f"access-{user_id}", f"refresh-{user_id}", AuthUser(user_id, email))

    def get_user(self, access_token: str) -> AuthUser:
        for email, (_password, user_id) in self.users.items():
            if access_token == f"access-{user_id}":
                return AuthUser(user_id, email)
        raise SupabaseAuthError("invalid or expired token")

    def refresh(self, refresh_token: str) -> AuthSession:
        for email, (_password, user_id) in self.users.items():
            if refresh_token == f"refresh-{user_id}":
                return AuthSession(
                    f"access-{user_id}", f"refresh-{user_id}", AuthUser(user_id, email)
                )
        raise SupabaseAuthError("invalid refresh token")

    def sign_out(self, access_token: str) -> None:
        self.signed_out_tokens.append(access_token)


def _client(fake: FakeAuthClient) -> TestClient:
    app.dependency_overrides[get_auth_client] = lambda: fake
    return TestClient(app)


# ── Sign-up ──────────────────────────────────────────────────────────────────


def test_signup_creates_session_and_redirects_to_dashboard():
    client = _client(FakeAuthClient())
    response = client.post(
        "/signup",
        data={"email": "a@example.com", "password": "hunter22", "confirm_password": "hunter22"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_signup_rejects_mismatched_passwords_without_calling_provider():
    fake = FakeAuthClient()
    client = _client(fake)
    response = client.post(
        "/signup",
        data={"email": "a@example.com", "password": "hunter22", "confirm_password": "different"},
    )
    assert response.status_code == 200
    assert "match" in response.text.lower()
    assert fake.users == {}


def test_signup_with_confirmation_required_shows_check_email_message():
    fake = FakeAuthClient()
    fake.confirmation_required = True
    client = _client(fake)
    response = client.post(
        "/signup",
        data={"email": "a@example.com", "password": "hunter22", "confirm_password": "hunter22"},
    )
    assert response.status_code == 200
    assert "check your email" in response.text.lower()


def test_signup_surfaces_provider_error_for_duplicate_email():
    fake = FakeAuthClient()
    fake.users["a@example.com"] = ("hunter22", "1")
    client = _client(fake)
    response = client.post(
        "/signup",
        data={"email": "a@example.com", "password": "hunter22", "confirm_password": "hunter22"},
    )
    assert response.status_code == 200
    assert "already registered" in response.text.lower()


# ── Sign-in / dashboard / sign-out ──────────────────────────────────────────


def test_signin_wrong_password_shows_error_and_preserves_email():
    fake = FakeAuthClient()
    fake.users["a@example.com"] = ("correct-password", "1")
    client = _client(fake)
    response = client.post("/signin", data={"email": "a@example.com", "password": "wrong"})
    assert response.status_code == 200
    assert "invalid login credentials" in response.text.lower()
    assert 'value="a@example.com"' in response.text


def test_dashboard_requires_session_redirects_to_signin():
    client = _client(FakeAuthClient())
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"


def test_full_signin_then_dashboard_then_signout_flow():
    fake = FakeAuthClient()
    fake.users["a@example.com"] = ("hunter22", "1")
    client = _client(fake)

    signin = client.post(
        "/signin", data={"email": "a@example.com", "password": "hunter22"}, follow_redirects=False
    )
    assert signin.status_code == 303

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "a@example.com" in dashboard.text

    signout = client.get("/signout", follow_redirects=False)
    assert signout.status_code == 303
    assert fake.signed_out_tokens == ["access-1"]

    after_signout = client.get("/dashboard", follow_redirects=False)
    assert after_signout.status_code == 303
    assert after_signout.headers["location"] == "/signin"


def test_dashboard_transparently_refreshes_expired_access_token():
    """get_current_user should fall back to the refresh token rather than
    booting the user, since Supabase access tokens are short-lived."""
    fake = FakeAuthClient()
    fake.users["a@example.com"] = ("hunter22", "1")
    client = _client(fake)
    client.post("/signin", data={"email": "a@example.com", "password": "hunter22"})

    # Simulate an expired access token by clobbering the real get_user check:
    # swap in a client whose access token is now "stale" but refresh works.
    class ExpiredThenRefreshable(FakeAuthClient):
        def get_user(self, access_token: str) -> AuthUser:
            raise SupabaseAuthError("token expired")

    expiring = ExpiredThenRefreshable()
    expiring.users = fake.users
    app.dependency_overrides[get_auth_client] = lambda: expiring

    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "a@example.com" in response.text


def test_dashboard_clears_session_when_refresh_also_fails():
    fake = FakeAuthClient()
    fake.users["a@example.com"] = ("hunter22", "1")
    client = _client(fake)
    client.post("/signin", data={"email": "a@example.com", "password": "hunter22"})

    class AlwaysInvalid(FakeAuthClient):
        def get_user(self, access_token: str) -> AuthUser:
            raise SupabaseAuthError("token expired")

        def refresh(self, refresh_token: str) -> AuthSession:
            raise SupabaseAuthError("refresh token revoked")

    app.dependency_overrides[get_auth_client] = lambda: AlwaysInvalid()

    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"
