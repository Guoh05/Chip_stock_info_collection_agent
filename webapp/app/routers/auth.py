"""/login + /auth/<token> + /logout (decision #10 Magic Link)."""
from __future__ import annotations
import logging
import secrets
import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import storage
from ..auth import SESSION_COOKIE
from ..config import (
    ALLOWLIST_EMAILS, AUTH_REQUIRED, MAGIC_LINK_TTL_MINUTES, SESSION_TTL_DAYS,
    TEMPLATES_DIR, WEBAPP_BASE_URL,
)
from ..services.emailer import send_magic_link

log = logging.getLogger("webapp.auth")
router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "auth_disabled": not AUTH_REQUIRED},
    )


@router.post("/login")
async def login_submit(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    if "@" not in email:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "请输入有效邮箱地址"},
            status_code=400,
        )

    # Allowlist check (decision #10)
    if AUTH_REQUIRED and email not in ALLOWLIST_EMAILS:
        log.info("login attempt denied: %s not in allowlist", email)
        # Don't reveal whether the email is allowlisted (avoid enumeration);
        # still show "we sent an email" — but actually send nothing.
        return templates.TemplateResponse(
            "auth_sent.html",
            {"request": request, "email": email},
        )

    # Generate Magic Link
    token = secrets.token_urlsafe(32)
    storage.new_magic_link(token, email, MAGIC_LINK_TTL_MINUTES)
    link = f"{WEBAPP_BASE_URL.rstrip('/')}/auth/{token}"
    log.info("magic link issued for %s (token=%s...)", email, token[:8])
    send_magic_link(email, link)

    return templates.TemplateResponse(
        "auth_sent.html",
        {"request": request, "email": email},
    )


@router.get("/auth/{token}")
async def auth_verify(request: Request, token: str):
    email = storage.consume_magic_link(token)
    if email is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "登录链接已过期、已被使用或无效。请重新申请。",
            },
            status_code=400,
        )
    if AUTH_REQUIRED and email not in ALLOWLIST_EMAILS:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "此邮箱已不在 allowlist。"},
            status_code=403,
        )

    session_id = uuid.uuid4().hex + secrets.token_hex(16)
    storage.new_session(session_id, email, SESSION_TTL_DAYS)
    log.info("session created for %s", email)

    response = RedirectResponse(url="/query", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, session_id,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        # secure=True  # enable when HTTPS — decision #11 MVP裸奔 HTTP, off for now
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    session = request.cookies.get(SESSION_COOKIE)
    if session:
        storage.revoke_session(session)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
