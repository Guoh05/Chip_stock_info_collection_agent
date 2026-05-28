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
from ..services.progress import read_progress

# Decisions #6 + UX: estimate ~3 min per MPN (scraper-dominated, --sequential
# over 9 sources). Used to show "预计 N 分钟" hint on the run page.
SECONDS_PER_MPN_ESTIMATE = 180

log = logging.getLogger("webapp.runs")
router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["render_cell"] = render_cell


def _env_root() -> Path:
    return PROJECT_ROOT / ("test" if PIPELINE_ENV == "test" else "production")


def _phases_from_state_file(path: Path) -> dict:
    """Read a pipeline state json (live or snapshot) → 3-phase dict."""
    if not path.exists():
        return {"api": "pending", "scraper_main": "pending", "merge": "pending"}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        phases = state.get("phases", {})
        return {
            "api": (phases.get("api") or {}).get("status", "pending"),
            "scraper_main": (phases.get("scraper_main") or {}).get("status", "pending"),
            "merge": (phases.get("merge") or {}).get("status", "pending"),
        }
    except Exception:
        return {"api": "pending", "scraper_main": "pending", "merge": "pending"}


def _phase_status_from_pipeline() -> dict:
    """Read pipeline's live .pipeline_state.json for the currently running run."""
    return _phases_from_state_file(_env_root() / ".pipeline_state.json")


def _phases_for_terminal_run(run_id: str, overall: str) -> dict:
    """Snapshot path (frozen at pipeline exit) → real per-phase status. Falls
    back to coarse heuristic when no snapshot exists (e.g. pre-state-file failure)."""
    snap = RUNS_DIR / run_id / "state_snapshot.json"
    if snap.exists():
        return _phases_from_state_file(snap)
    if overall in ("done", "done_empty"):
        return {"api": "ok", "scraper_main": "ok", "merge": "ok"}
    if overall == "failed":
        return {"api": "pending", "scraper_main": "failed", "merge": "skipped"}
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

    phases = (
        _phase_status_from_pipeline() if overall in ("queued", "running")
        else _phases_for_terminal_run(run_id, overall)
    )

    qpos = storage.queue_position(run_id) if overall == "queued" else 0

    n_mpns = len(run.get("mpns") or [])
    estimated_minutes = max(1, (n_mpns * SECONDS_PER_MPN_ESTIMATE + 59) // 60)

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
            "estimated_minutes": estimated_minutes,
            "n_mpns": n_mpns,
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
    phases = (
        _phase_status_from_pipeline() if overall in ("queued", "running")
        else _phases_for_terminal_run(run_id, overall)
    )
    qpos = storage.queue_position(run_id) if overall == "queued" else 0
    progress = read_progress(RUNS_DIR / run_id / "pipeline.log") if overall == "running" else None
    return JSONResponse({
        "status": overall,
        "phases": phases,
        "queue_position": qpos,
        "progress": progress,
    })


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
