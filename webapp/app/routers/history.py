"""/history — list owner's runs (decision #24, owner_email-filtered)."""
from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from .. import storage
from ..auth import require_user_or_redirect
from ..config import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/history")
async def history_page(request: Request):
    email, redirect = require_user_or_redirect(request)
    if redirect:
        return redirect
    runs = storage.list_runs(owner_email=email)
    for r in runs:
        r["display_status"] = r["status"]
    return templates.TemplateResponse(
        "history.html",
        {"request": request, "runs": runs, "current_user": email},
    )
