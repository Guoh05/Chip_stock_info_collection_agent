"""Auth helpers: session lookup + require-user dependency (decision #10).

When `AUTH_REQUIRED=False` (local dev, no ALLOWLIST_EMAILS), `current_user()`
falls back to DEV_OWNER_EMAIL — keeps laptop iteration friction-free.
"""
from __future__ import annotations
from fastapi import Cookie, Request
from fastapi.responses import RedirectResponse

from . import storage
from .config import AUTH_REQUIRED, DEV_OWNER_EMAIL

SESSION_COOKIE = "chipwebapp_session"


def current_user_email(session: str | None) -> str | None:
    """Return current user's email or None if no/invalid session."""
    if not AUTH_REQUIRED:
        return DEV_OWNER_EMAIL
    if not session:
        return None
    return storage.lookup_session(session)


def require_user_or_redirect(request: Request):
    """Convenience helper for route handlers: returns either (email, None)
    when authed, or (None, RedirectResponse) when not — caller checks.

    Usage in route:
        email, redirect = require_user_or_redirect(request)
        if redirect:
            return redirect
        # ... use email ...
    """
    session = request.cookies.get(SESSION_COOKIE)
    email = current_user_email(session)
    if email is None:
        return None, RedirectResponse(url="/login", status_code=303)
    return email, None
