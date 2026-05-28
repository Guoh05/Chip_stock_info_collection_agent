"""/r/<run_id> — waiting + result + download (M1).

Reads real data from SQLite + .pipeline_state.json + parsed.json.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from .. import storage
from ..auth import require_user_or_redirect
from ..config import PROJECT_ROOT, RUNS_DIR, TEMPLATES_DIR, PIPELINE_ENV
from ..schemas import HIGHLIGHT_COLUMNS, WEBAPP_SCHEMA_v1, render_cell

log = logging.getLogger("webapp.runs")
router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["render_cell"] = render_cell


def _env_root() -> Path:
    return PROJECT_ROOT / ("test" if PIPELINE_ENV == "test" else "production")


def _phase_status_from_pipeline() -> dict:
    """Read pipeline's .pipeline_state.json to surface live phase progress."""
    state_path = _env_root() / ".pipeline_state.json"
    if not state_path.exists():
        return {"api": "pending", "scraper_main": "pending", "merge": "pending"}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        phases = state.get("phases", {})
        return {
            "api": (phases.get("api") or {}).get("status", "pending"),
            "scraper_main": (phases.get("scraper_main") or {}).get("status", "pending"),
            "merge": (phases.get("merge") or {}).get("status", "pending"),
        }
    except Exception:
        return {"api": "pending", "scraper_main": "pending", "merge": "pending"}


@router.get("/r/{run_id}")
async def run_page(request: Request, run_id: str, cache_hit: int = 0):
    email, redirect = require_user_or_redirect(request)
    if redirect:
        return redirect
    run = storage.get_run(run_id)
    if not run:
        return PlainTextResponse(f"Run {run_id} not found", status_code=404)
    if run["owner_email"] != email:
        return PlainTextResponse("Not your run", status_code=403)

    overall = run["status"]
    results: list[dict] = []
    if overall in ("done", "done_empty"):
        parsed_path = RUNS_DIR / run_id / "parsed.json"
        if parsed_path.exists():
            try:
                results = json.loads(parsed_path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("parsed.json read failed for %s", run_id)

    # If running, expose live phase status; otherwise leave defaults.
    phases = _phase_status_from_pipeline() if overall in ("queued", "running") else {
        "api": "ok", "scraper_main": "ok", "merge": "ok"
    } if overall in ("done", "done_empty") else {
        "api": "ok", "scraper_main": "failed", "merge": "skipped"
    } if overall == "failed" else {"api": "pending", "scraper_main": "pending", "merge": "pending"}

    # Queue position: only meaningful for status=queued (others are 0/irrelevant)
    qpos = storage.queue_position(run_id) if overall == "queued" else 0

    return templates.TemplateResponse(
        "run.html",
        {
            "request": request,
            "run": run,
            "overall_status": overall,
            "phases": phases,
            "queue_position": qpos,
            "results": results,
            "show_empty": overall == "done_empty",
            "schema": WEBAPP_SCHEMA_v1,
            "highlight": HIGHLIGHT_COLUMNS,
            "error_text": run.get("error_text"),
            "cache_hit": bool(cache_hit),
        },
    )


@router.get("/r/{run_id}/status")
async def run_status(request: Request, run_id: str):
    """Polled by run.html — decision #23."""
    email, redirect = require_user_or_redirect(request)
    if redirect:
        return JSONResponse({"error": "auth required"}, status_code=401)
    run = storage.get_run(run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run["owner_email"] != email:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    overall = run["status"]
    phases = _phase_status_from_pipeline() if overall in ("queued", "running") else {
        "api": "ok", "scraper_main": "ok", "merge": "ok"
    } if overall in ("done", "done_empty") else {
        "api": "ok", "scraper_main": "failed", "merge": "skipped"
    } if overall == "failed" else {"api": "pending", "scraper_main": "pending", "merge": "pending"}
    qpos = storage.queue_position(run_id) if overall == "queued" else 0
    return JSONResponse({"status": overall, "phases": phases, "queue_position": qpos})


@router.get("/r/{run_id}/download")
async def download(request: Request, run_id: str):
    email, redirect = require_user_or_redirect(request)
    if redirect:
        return redirect
    run = storage.get_run(run_id)
    if not run:
        return PlainTextResponse(f"Run {run_id} not found", status_code=404)
    if run["owner_email"] != email:
        return PlainTextResponse("Not your run", status_code=403)
    slim_path = RUNS_DIR / run_id / f"Versuni_chip_stock_{run_id}.xlsx"
    if not slim_path.exists():
        return PlainTextResponse(
            f"xlsx not ready yet (run status: {run['status']})", status_code=404,
        )
    return FileResponse(
        path=str(slim_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=slim_path.name,
    )
