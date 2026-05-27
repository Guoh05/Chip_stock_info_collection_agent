"""/history — list owner's runs (M1: reads from SQLite).

Decision #24: filter by owner_email (only own runs).
"""
from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from .. import storage
from ..config import DEV_OWNER_EMAIL, TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/history")
async def history_page(request: Request):
    runs = storage.list_runs(owner_email=DEV_OWNER_EMAIL)
    # Template expects display_status; map status → display_status 1:1 for now
    for r in runs:
        r["display_status"] = r["status"]
    return templates.TemplateResponse(
        "history.html", {"request": request, "runs": runs},
    )
