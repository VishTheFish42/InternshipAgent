"""Minimal server-rendered web app (T-1101): Supabase-backed sign-up, sign-in,
and session handling. Proves the auth flow end-to-end; resume upload, company
watchlist, and every other per-user feature in docs/design.md §14 build on top
of the session established here, starting at T-1102 (multi-tenant schema).

Run locally: python -m src.web
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.web.config import get_web_settings
from src.web.supabase_auth import AuthUser, SupabaseAuthClient, SupabaseAuthError

app = FastAPI(title="InternshipAgent")

_settings = get_web_settings()
app.add_middleware(SessionMiddleware, secret_key=_settings.session_secret)

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_auth_client() -> SupabaseAuthClient:
    return SupabaseAuthClient(_settings.supabase_url, _settings.supabase_anon_key)


def get_current_user(
    request: Request,
    auth: Annotated[SupabaseAuthClient, Depends(get_auth_client)],
) -> AuthUser | None:
    """Verifies the session's access token against Supabase on every request
    (not just decoding the signed cookie) so a revoked session is rejected —
    see SupabaseAuthClient.get_user's docstring. Transparently refreshes an
    expired access token using the stored refresh token before giving up;
    clears the session if neither the token nor the refresh is valid."""
    access_token = request.session.get("access_token")
    if not access_token:
        return None
    try:
        return auth.get_user(access_token)
    except SupabaseAuthError:
        pass

    refresh_token = request.session.get("refresh_token")
    if not refresh_token:
        request.session.clear()
        return None
    try:
        refreshed = auth.refresh(refresh_token)
    except SupabaseAuthError:
        request.session.clear()
        return None

    request.session["access_token"] = refreshed.access_token
    request.session["refresh_token"] = refreshed.refresh_token
    return refreshed.user


@app.get("/")
def index(user: Annotated[AuthUser | None, Depends(get_current_user)]) -> RedirectResponse:
    return RedirectResponse("/dashboard" if user else "/signin")


@app.get("/signup")
def signup_form(request: Request) -> Any:
    return _templates.TemplateResponse(request, "signup.html", {})


@app.post("/signup")
def signup_submit(
    request: Request,
    auth: Annotated[SupabaseAuthClient, Depends(get_auth_client)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> Any:
    if password != confirm_password:
        return _templates.TemplateResponse(
            request, "signup.html", {"error": "Passwords don't match.", "email": email}
        )
    try:
        session = auth.sign_up(email, password)
    except SupabaseAuthError as exc:
        return _templates.TemplateResponse(
            request, "signup.html", {"error": str(exc), "email": email}
        )

    if session is None:
        # Supabase project has "Confirm email" enabled — no session is issued
        # until the user clicks the link it just emailed them.
        return _templates.TemplateResponse(
            request,
            "signup.html",
            {"success": "Account created — check your email to confirm before signing in."},
        )

    request.session["access_token"] = session.access_token
    request.session["refresh_token"] = session.refresh_token
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/signin")
def signin_form(request: Request) -> Any:
    return _templates.TemplateResponse(request, "signin.html", {})


@app.post("/signin")
def signin_submit(
    request: Request,
    auth: Annotated[SupabaseAuthClient, Depends(get_auth_client)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Any:
    try:
        session = auth.sign_in(email, password)
    except SupabaseAuthError as exc:
        return _templates.TemplateResponse(
            request, "signin.html", {"error": str(exc), "email": email}
        )

    request.session["access_token"] = session.access_token
    request.session["refresh_token"] = session.refresh_token
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/signout")
def signout(
    request: Request,
    auth: Annotated[SupabaseAuthClient, Depends(get_auth_client)],
) -> RedirectResponse:
    access_token = request.session.get("access_token")
    if access_token:
        auth.sign_out(access_token)
    request.session.clear()
    return RedirectResponse("/signin", status_code=303)


@app.get("/dashboard")
def dashboard(
    request: Request, user: Annotated[AuthUser | None, Depends(get_current_user)]
) -> Any:
    if user is None:
        return RedirectResponse("/signin", status_code=303)
    return _templates.TemplateResponse(request, "dashboard.html", {"email": user.email})
