"""/query routes (M2).

Mode A (paste, MPN-only) + Mode B (Excel upload with metadata) + cleaning review.

Flow:
  GET /query                  → query page (tabs)
  POST /query (mode_a)        → parse text → clean → review.html if changes, else /r/<id>
  POST /query (mode_b)        → parse xlsx → clean → always review.html (to confirm metadata)
  GET /query/template         → download .xlsx template
  POST /query/confirm         → read pending review → create run + enqueue → /r/<id>
"""
from __future__ import annotations
import logging
import re
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import storage
from ..auth import require_user_or_redirect
from ..config import RUNS_DIR, TEMPLATES_DIR
from ..services import pipeline_runner
from ..services.excel_input import (
    ExcelParseError, make_template_xlsx, parse_upload, write_input_csv,
)
from ..services.mpn_cleaner import clean_batch

log = logging.getLogger("webapp.query")
router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_SPLIT_RE = re.compile(r"[\n\r]+")
_SUSPICIOUS_LEN = 50

# In-memory pending review stash (TTL ~1h). Single-process uvicorn → safe.
_PENDING: dict[str, dict] = {}
_PENDING_TTL = 3600


def _stash(mpns: list[str], metadata: list[dict] | None, mode: str) -> str:
    _gc_pending()
    token = uuid.uuid4().hex[:12]
    _PENDING[token] = {
        "mpns": mpns, "metadata": metadata, "mode": mode, "ts": time.time(),
    }
    return token


def _pop(token: str) -> dict | None:
    _gc_pending()
    return _PENDING.pop(token, None)


def _gc_pending() -> None:
    cutoff = time.time() - _PENDING_TTL
    stale = [k for k, v in _PENDING.items() if v["ts"] < cutoff]
    for k in stale:
        _PENDING.pop(k, None)


def _parse_paste(text: str) -> tuple[list[str] | None, str | None, int]:
    """Return (deduped_mpns, error_msg, raw_row_count). On error, mpns is None."""
    raw = [m.strip() for m in _SPLIT_RE.split(text)]
    raw_nonempty = [m for m in raw if m]
    mpns: list[str] = []
    for m in raw_nonempty:
        if m not in mpns:
            mpns.append(m)
    if not mpns:
        return None, "请先粘贴 MPN 列表（每行一个）", 0
    suspicious = [m for m in mpns if len(m) > _SUSPICIOUS_LEN]
    if suspicious:
        return None, (
            f"检测到 1 个超长 MPN（{len(suspicious[0])} 字符）。MPN 通常 ≤30 字符。"
            "如果你粘贴的是逗号/分号分隔的列表，请改成一行一个 MPN 再提交。"
        ), len(raw_nonempty)
    return mpns, None, len(raw_nonempty)


@router.get("/query")
async def query_page(request: Request):
    email, redirect = require_user_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        "query.html", {"request": request, "current_user": email},
    )


@router.get("/query/template")
async def download_template(request: Request):
    email, redirect = require_user_or_redirect(request)
    if redirect:
        return redirect
    xlsx_bytes = make_template_xlsx()
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="chip_query_template.xlsx"'},
    )


@router.post("/query")
async def submit_query(
    request: Request,
    mode: str = Form("paste"),
    mpns_text: str = Form(""),
    upload: UploadFile | None = File(None),
):
    email, redirect = require_user_or_redirect(request)
    if redirect:
        return redirect
    metadata: list[dict] | None = None
    raw_row_count = 0  # tracks how many MPN-bearing rows the user submitted, pre-dedup

    if mode == "paste":
        mpns, err, raw_row_count = _parse_paste(mpns_text)
        if err:
            return templates.TemplateResponse(
                "query.html",
                {"request": request, "error": err, "prefill": mpns_text},
                status_code=400,
            )
    elif mode == "upload":
        if upload is None or not upload.filename:
            return templates.TemplateResponse(
                "query.html",
                {"request": request, "error": "请先选择 Excel 文件"},
                status_code=400,
            )
        if not upload.filename.lower().endswith(".xlsx"):
            return templates.TemplateResponse(
                "query.html",
                {"request": request, "error": "只支持 .xlsx 格式"},
                status_code=400,
            )
        content = await upload.read()
        try:
            mpns, metadata, raw_row_count = parse_upload(content)
        except ExcelParseError as e:
            return templates.TemplateResponse(
                "query.html",
                {"request": request, "error": str(e)},
                status_code=400,
            )
    else:
        return templates.TemplateResponse(
            "query.html", {"request": request, "error": f"未知模式 {mode!r}"},
            status_code=400,
        )

    dedup_dropped = max(0, raw_row_count - len(mpns))

    # Run mechanical cleaner (decision #22)
    clean_results, has_changes = clean_batch(mpns)

    if has_changes or mode == "upload":
        token = _stash(
            [r.cleaned for r in clean_results],
            metadata,
            mode,
        )
        return templates.TemplateResponse(
            "review.html",
            {
                "request": request,
                "results": clean_results,
                "metadata": metadata,
                "token": token,
                "mode": mode,
                "raw_row_count": raw_row_count,
                "dedup_dropped": dedup_dropped,
            },
        )

    # No changes for Mode A → straight through
    return _create_and_enqueue([r.cleaned for r in clean_results], None, email=email)


@router.post("/query/confirm")
async def confirm_review(
    request: Request,
    token: str = Form(...),
    final_mpns: str = Form(""),
    force_rerun: str = Form(""),
):
    email, redirect = require_user_or_redirect(request)
    if redirect:
        return redirect
    pending = _pop(token)
    if not pending:
        return templates.TemplateResponse(
            "query.html",
            {"request": request, "error": "review session 过期或无效，请重新提交"},
            status_code=400,
        )
    # final_mpns may differ from pending if user edited on review page
    edited = _SPLIT_RE.split(final_mpns)
    final = []
    for m in edited:
        m = m.strip()
        if m and m not in final:
            final.append(m)
    if not final:
        return templates.TemplateResponse(
            "query.html",
            {"request": request, "error": "MPN 列表为空"},
            status_code=400,
        )

    # Re-align metadata to final MPNs:
    # pending["mpns"] is the cleaned-MPN list shown on the review page;
    # pending["metadata"] is the metadata aligned to those cleaned MPNs.
    # If user edited final_mpns (delete/add/reorder), we look up metadata
    # by cleaned MPN match. MPNs the user added that weren't in original →
    # no metadata for them.
    pending_cleaned: list[str] = pending["mpns"]
    pending_metadata = pending.get("metadata")
    matched_metadata: list[dict] | None = None
    if pending_metadata:
        mpn_to_meta: dict[str, dict] = {}
        for clean_mpn, meta in zip(pending_cleaned, pending_metadata):
            if clean_mpn not in mpn_to_meta:
                new_meta = dict(meta)
                new_meta["Manufacture Part Number"] = clean_mpn
                mpn_to_meta[clean_mpn] = new_meta
        matched_metadata = [mpn_to_meta[m] for m in final if m in mpn_to_meta]
        if not matched_metadata:
            matched_metadata = None  # all final MPNs were user-added; no metadata to write

    return _create_and_enqueue(final, matched_metadata, email=email, force_rerun=bool(force_rerun))


def _create_and_enqueue(
    mpns: list[str], metadata: list[dict] | None, *, email: str, force_rerun: bool = False,
) -> RedirectResponse:
    if not force_rerun:
        mpns_hash = storage.hash_mpns(mpns)
        cached = storage.find_cached_run(mpns_hash, hours=24)
        if cached and cached["owner_email"] == email:
            log.info("cache hit: redirecting to existing run %s", cached["run_id"])
            return RedirectResponse(
                url=f"/r/{cached['run_id']}?cache_hit=1", status_code=303,
            )

    run_id = f"r_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
    storage.new_run(run_id, mpns, owner_email=email)
    if metadata:
        try:
            write_input_csv(metadata, RUNS_DIR / run_id / "input.csv")
        except Exception:
            log.exception("write input.csv failed for %s", run_id)
    log.info("queued run %s with %d MPNs (metadata=%s, force=%s)",
             run_id, len(mpns), bool(metadata), force_rerun)
    pipeline_runner.enqueue(run_id)
    return RedirectResponse(url=f"/r/{run_id}", status_code=303)
