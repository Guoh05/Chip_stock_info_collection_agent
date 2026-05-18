"""Batch-run every chip in ref/Chip_DataSource_Master.xlsx through the four
working scrapers (LCSC, Digikey, HQEW, Future) and consolidate results.

Each (input_mpn, channel) is executed as a SUBPROCESS so a hung browser or
crashed scraper can be killed cleanly with a hard wallclock timeout — that is
the only reliable way to keep a multi-hour batch moving.

Per-channel timeouts (seconds, observed-budget × 1.4 safety):
    LCSC v3     240
    Digikey     180
    HQEW         90
    Future      300

Outputs under `test/scraper_test/BatchTest_<YYYYMMDD>_<HH_MM_SS>/`:
    batch_summary.md                — TL;DR + per-channel stats + highlights
    batch_index.csv / .xlsx         — long form (one row per MPN × channel)
    batch_compare.csv / .xlsx       — wide form (~43 cols, all 4 channels)
    batch_index.json                — machine-readable long form
    batch_input.csv                 — verbatim (MPN, expected_mfr) from xlsx
    failures.md                     — non-ok rows grouped by channel
    Test_<sanitized_mpn>_<CHANNEL>/ — per-MPN-per-channel run folder
                                       (populated by the scraper subprocess)

Usage:
    .venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py
    .venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --limit 3
    .venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --only LCSC,HQEW
    .venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --resume
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scraper" / "scripts"
SCRAPER_TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
DEFAULT_XLSX = PROJECT_ROOT / "ref" / "Chip_DataSource_Master.xlsx"

CHANNELS: dict[str, dict[str, Any]] = {
    "LCSC":    {"script": "scrape_lcsc_v3.py", "timeout": 240},
    "DIGIKEY": {"script": "scrape_digikey.py", "timeout": 180},
    "HQEW":    {"script": "scrape_hqew.py",    "timeout": 90},
    "FUTURE":  {"script": "scrape_future.py",  "timeout": 300},
}

THROTTLE_SECONDS = 1.0


# --- helpers ---------------------------------------------------------------


def _safe_folder(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", mpn) or "UNKNOWN"


def _looks_like_real_mpn(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    if len(s) < 3:
        return False
    if "缺失" in s or "TBD" in s.upper() or "TODO" in s.upper():
        return False
    if not re.search(r"[A-Za-z0-9]", s):
        return False
    return True


def _norm_mfr(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _mfr_match(expected: str | None, returned: str | None) -> bool:
    e = _norm_mfr(expected)
    r = _norm_mfr(returned)
    if not e or not r:
        return False
    return e in r or r in e


def _parse_price_str(price_str) -> float | None:
    if price_str is None or price_str == "":
        return None
    if isinstance(price_str, (int, float)):
        return float(price_str)
    s = re.sub(r"[^\d.,\-]", "", str(price_str).strip())
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


# --- input loading ---------------------------------------------------------


def load_chip_list(xlsx_path: Path) -> tuple[list[dict], list[dict]]:
    """Return (chips, skipped). Sheet 1, header at row 4, data from row 5."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.worksheets[0]
    chips: list[dict] = []
    skipped: list[dict] = []
    for r in range(5, ws.max_row + 1):
        mpn = ws.cell(row=r, column=1).value
        mfr = ws.cell(row=r, column=2).value
        mpn_s = str(mpn).strip() if mpn is not None else ""
        mfr_s = str(mfr).strip() if mfr is not None else ""
        if not _looks_like_real_mpn(mpn_s):
            skipped.append({"row": r, "raw_mpn": mpn_s, "raw_mfr": mfr_s,
                            "reason": "missing or non-MPN placeholder"})
            continue
        chips.append({"row": r, "input_mpn": mpn_s, "expected_mfr": mfr_s})
    return chips, skipped


# --- subprocess execution --------------------------------------------------


def run_subprocess(channel: str, mpn: str, out_dir: Path) -> dict:
    """Run a single scraper subprocess with hard wallclock timeout.

    Returns {"status": ok|exit_N|timeout|exception, "elapsed_sec", "stdout_tail",
    "stderr_tail", "error"}.
    """
    cfg = CHANNELS[channel]
    script = SCRIPTS_DIR / cfg["script"]
    timeout = cfg["timeout"]
    cmd = [sys.executable, str(script), mpn, str(out_dir)]
    # Force UTF-8 in the subprocess so prints of '¥' / Chinese characters don't
    # crash under Windows GBK when stdout is being captured by us. The scrapers'
    # actual data is written to disk independently — only their console prints
    # are affected — but a Unicode crash in a final print still aborts the
    # process with exit code 1, which we'd misclassify as a failure.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        elapsed = time.time() - t0
        if proc.returncode == 0:
            status = "ok"
            error = None
        else:
            status = f"exit_{proc.returncode}"
            error = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else None
        return {
            "status": status,
            "elapsed_sec": round(elapsed, 1),
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
            "error": error,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "elapsed_sec": round(time.time() - t0, 1),
            "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
            "error": f"timeout after {timeout}s",
        }
    except Exception as exc:
        return {
            "status": "exception",
            "elapsed_sec": round(time.time() - t0, 1),
            "stdout_tail": "",
            "stderr_tail": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


# --- post-subprocess: load + pick best variant ----------------------------


def load_record(out_dir: Path, mpn: str) -> dict | None:
    """Find the parent JSON the scraper wrote. Scrapers name it `<safe>.json`."""
    safe = _safe_folder(mpn)
    candidate = out_dir / f"{safe}.json"
    if candidate.exists():
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    # Fallback: any top-level .json
    for p in sorted(out_dir.glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
    return None


def pick_best_extracted(record: dict, channel: str, out_dir: Path, input_mpn: str) -> tuple[dict | None, int]:
    """Choose the variant whose data best represents this MPN.

    Returns (extracted_dict, num_variants). For single-result channels
    (Digikey, HQEW) num_variants is 1 when ok else 0.
    """
    if not record:
        return None, 0

    # HQEW + Digikey carry a flat `extracted` directly
    if channel in ("DIGIKEY", "HQEW"):
        ex = record.get("extracted")
        if ex:
            # HQEW's extracted may carry an inner variants list — count is informative
            n = len(ex.get("variants") or []) if channel == "HQEW" else 1
            return ex, n if n else 1
        return None, 0

    # LCSC v3 / Future — record["variants"] list
    variants = record.get("variants") or []
    if not variants:
        return None, 0

    target = (input_mpn or "").strip().lower()

    def _stock_of(d: dict) -> int:
        try:
            return int(d.get("stock_now_qty") or 0)
        except (TypeError, ValueError):
            return 0

    if channel == "LCSC":
        # variants is list of variant_rec dicts with inline `extracted`
        ok_variants = [v for v in variants if v.get("status") == "ok" and v.get("extracted")]
        if not ok_variants:
            return None, len(variants)
        exact = [v for v in ok_variants
                 if str(v["extracted"].get("manufacturer_part_number", "")).strip().lower() == target]
        pool = exact or ok_variants
        best = max(pool, key=lambda v: _stock_of(v["extracted"]))
        return best["extracted"], len(variants)

    if channel == "FUTURE":
        # variants is list of lifted summary dicts; full extracted lives in
        # the per-variant subfolder's <MPN>.json
        candidates: list[dict] = []
        for v in variants:
            mpn = v.get("manufacturer_part_number") or ""
            sub = v.get("subfolder")
            if not sub:
                continue
            sub_json = out_dir / sub / f"{_safe_folder(mpn)}.json"
            if not sub_json.exists():
                continue
            try:
                vr = json.loads(sub_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            ex = vr.get("extracted") if vr.get("status") == "ok" else None
            if ex:
                candidates.append(ex)
        if not candidates:
            return None, len(variants)
        exact = [ex for ex in candidates
                 if str(ex.get("manufacturer_part_number", "")).strip().lower() == target]
        pool = exact or candidates
        best = max(pool, key=_stock_of)
        return best, len(variants)

    return None, 0


# --- index row + compare row -----------------------------------------------


def derive_price_summary(prices: list[dict]) -> dict:
    if not prices:
        return {"price_at_qty_1": None, "min_break_qty": None,
                "lowest_unit_price": None, "num_price_tiers": 0}
    tiers = [t for t in prices if isinstance(t, dict)]

    def _num(t):
        for k in ("unit_price_float", "unit_price_cny", "unit_price"):
            v = t.get(k)
            if v is not None and v != "":
                if isinstance(v, (int, float)):
                    return float(v)
                parsed = _parse_price_str(v)
                if parsed is not None:
                    return parsed
        return None

    by_qty = sorted([t for t in tiers if t.get("min_qty") is not None],
                    key=lambda t: t.get("min_qty"))
    min_break = by_qty[0] if by_qty else None
    largest = by_qty[-1] if by_qty else None
    one_break = next((t for t in by_qty if t.get("min_qty") == 1), None)
    return {
        "price_at_qty_1": (_num(one_break) if one_break else (_num(min_break) if min_break else None)),
        "min_break_qty": (min_break.get("min_qty") if min_break else None),
        "lowest_unit_price": (_num(largest) if largest else None),
        "num_price_tiers": len(tiers),
    }


def make_index_row(
    input_mpn: str,
    expected_mfr: str,
    channel: str,
    sub_result: dict,
    record: dict | None,
    extracted: dict | None,
    num_variants: int,
    run_subdir: Path,
) -> dict:
    ex = extracted or {}

    # Compose final status: subprocess outcome ∧ record content.
    # The scrapers write their JSON BEFORE the final print(), so a successful
    # scrape that crashes during console output still leaves a valid record on
    # disk. Trust the record's own status when it exists.
    sub_status = sub_result.get("status")
    rec_status = (record or {}).get("status") if record else None

    if rec_status == "ok" and extracted:
        status = "ok"
    elif rec_status in ("no_matches", "no_results", "no_listings"):
        status = "no_results"
    elif rec_status == "blocked":
        status = "blocked"
    elif rec_status == "exception":
        status = "exception"
    elif sub_status == "timeout":
        status = "timeout"
    elif sub_status != "ok":
        # Subprocess crashed AND no record on disk → real failure
        status = sub_status
    else:
        status = rec_status or "no_results"

    prices_summary = derive_price_summary(ex.get("prices") or [])
    returned_mfr = ex.get("manufacturer") or ""

    # Currency: LCSC uses CNY implicitly when stock_now_qty present; Digikey has currency in price tiers; Future has it
    currency = ex.get("currency")
    if not currency and channel == "LCSC" and ex.get("unit_price_cny") is not None:
        currency = "CNY"

    error = sub_result.get("error") or ""
    if not error and rec_status == "exception":
        error = (record or {}).get("error", "")
    if not error and rec_status == "blocked":
        error = (record or {}).get("blocker", "blocked")

    return {
        "input_mpn": input_mpn,
        "expected_mfr": expected_mfr,
        "channel": channel,
        "status": status,
        "elapsed_sec": sub_result.get("elapsed_sec"),
        "num_variants": num_variants,
        "returned_mpn": ex.get("manufacturer_part_number") or "",
        "returned_mfr": returned_mfr,
        "mfr_match": _mfr_match(expected_mfr, returned_mfr),
        "stock_now_qty": ex.get("stock_now_qty"),
        "stock_future_qty": ex.get("stock_future_qty"),
        "stock_future_ship_text": ex.get("stock_future_ship_text") or "",
        "price_at_qty_1": prices_summary["price_at_qty_1"],
        "min_break_qty": prices_summary["min_break_qty"],
        "lowest_unit_price": prices_summary["lowest_unit_price"],
        "num_price_tiers": prices_summary["num_price_tiers"],
        "currency": currency or "",
        "datasheet_url": ex.get("datasheet_url") or "",
        "run_subdir": str(run_subdir.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "error": (error or "")[:300],
    }


INDEX_COLUMNS = [
    "input_mpn", "expected_mfr", "channel", "status", "elapsed_sec", "num_variants",
    "returned_mpn", "returned_mfr", "mfr_match",
    "stock_now_qty", "stock_future_qty", "stock_future_ship_text",
    "price_at_qty_1", "min_break_qty", "lowest_unit_price", "num_price_tiers",
    "currency", "datasheet_url", "run_subdir", "error",
]


# Per-channel fields lifted into the wide compare row
COMPARE_FIELDS = [
    "status", "num_variants", "returned_mpn", "returned_mfr", "mfr_match",
    "stock_now_qty", "stock_future_qty", "stock_future_ship_text",
    "price_at_qty_1", "datasheet_url",
]

CHANNEL_ORDER = ["LCSC", "DIGIKEY", "HQEW", "FUTURE"]


def make_compare_row(per_chip_rows: dict[str, dict]) -> dict:
    out = {
        "input_mpn": next(iter(per_chip_rows.values()))["input_mpn"],
        "expected_mfr": next(iter(per_chip_rows.values()))["expected_mfr"],
    }
    for ch in CHANNEL_ORDER:
        row = per_chip_rows.get(ch)
        for fld in COMPARE_FIELDS:
            key = f"{ch.lower()}_{fld}"
            out[key] = row.get(fld) if row else None
    # Cross-channel stock_now disagreement marker: list channels that have stock>0
    have = []
    for ch in CHANNEL_ORDER:
        row = per_chip_rows.get(ch)
        if row and (row.get("stock_now_qty") or 0) > 0:
            have.append(ch.lower())
    if not have:
        out["stock_now_disagreement"] = "none_have"
    elif len(have) == len(CHANNEL_ORDER):
        out["stock_now_disagreement"] = "all_have"
    else:
        out["stock_now_disagreement"] = "only_" + "+".join(have)
    return out


COMPARE_COLUMNS = ["input_mpn", "expected_mfr"]
for _ch in CHANNEL_ORDER:
    for _fld in COMPARE_FIELDS:
        COMPARE_COLUMNS.append(f"{_ch.lower()}_{_fld}")
COMPARE_COLUMNS.append("stock_now_disagreement")


# --- writers ---------------------------------------------------------------


def _cell_value_for_excel(v):
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def write_csv(rows: list[dict], columns: list[str], path: Path) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})


def write_xlsx(rows: list[dict], columns: list[str], path: Path, sheet_name: str) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name[:31] or "Sheet1"
    header_font = Font(bold=True)
    header_fill = PatternFill(start_color="FFE2E2E2", end_color="FFE2E2E2", fill_type="solid")
    for col_idx, col in enumerate(columns, 1):
        c = ws.cell(row=1, column=col_idx, value=col)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(vertical="center")
    for row_idx, r in enumerate(rows, 2):
        for col_idx, col in enumerate(columns, 1):
            ws.cell(row=row_idx, column=col_idx, value=_cell_value_for_excel(r.get(col)))
    ws.freeze_panes = "A2"
    for col_idx, col in enumerate(columns, 1):
        sample = [str(r.get(col)) for r in rows if r.get(col) is not None][:300]
        max_len = max([len(col)] + [len(s) for s in sample] + [0])
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 2, 10), 60)
    wb.save(path)


def write_batch_input_csv(chips: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["xlsx_row", "input_mpn", "expected_mfr"])
        for c in chips:
            w.writerow([c["row"], c["input_mpn"], c["expected_mfr"]])


def write_failures(rows: list[dict], path: Path) -> None:
    failures = [r for r in rows if r["status"] != "ok"]
    lines = ["# Batch scraper failures", ""]
    if not failures:
        lines.append("_No failures._")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    lines.append(f"{len(failures)} non-ok rows (out of {len(rows)} total).")
    lines.append("")
    by_ch: dict[str, list[dict]] = {}
    for r in failures:
        by_ch.setdefault(r["channel"], []).append(r)
    for ch in CHANNEL_ORDER:
        ch_rows = by_ch.get(ch, [])
        if not ch_rows:
            continue
        lines.append(f"## {ch} — {len(ch_rows)} failure(s)")
        lines.append("")
        lines.append("| input_mpn | expected_mfr | status | elapsed_sec | error | run_subdir |")
        lines.append("|---|---|---|---|---|---|")
        for r in ch_rows:
            err = (r.get("error") or "").replace("|", "\\|").replace("\n", " ")[:200]
            lines.append(
                f"| `{r['input_mpn']}` | {r['expected_mfr']} | {r['status']} | "
                f"{r.get('elapsed_sec','')} | {err} | `{r['run_subdir']}` |"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_summary_md(
    chips: list[dict],
    rows: list[dict],
    compare_rows: list[dict],
    skipped: list[dict],
    channels_used: list[str],
    batch_dir: Path,
    started: datetime,
    finished: datetime,
) -> None:
    lines: list[str] = []
    elapsed = (finished - started).total_seconds()
    lines.append(f"# Batch scraper sweep — {len(chips)} chips × {len(channels_used)} channels")
    lines.append("")
    lines.append(f"- **Started:** {started.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **Finished:** {finished.strftime('%Y-%m-%d %H:%M:%S')}  (elapsed {elapsed:.1f} s ≈ {elapsed/60:.1f} min)")
    lines.append(f"- **Chips processed:** {len(chips)}  ({len(skipped)} row(s) skipped from xlsx)")
    lines.append(f"- **Total scrape calls:** {len(rows)}  (channels: {', '.join(channels_used)})")
    lines.append("")

    # Per-channel pass rate
    per_channel: dict[str, dict] = {}
    for r in rows:
        d = per_channel.setdefault(r["channel"], {"ok": 0, "no_results": 0,
                                                  "blocked": 0, "timeout": 0,
                                                  "failed_other": 0, "total": 0,
                                                  "elapsed_total": 0.0})
        d["total"] += 1
        d["elapsed_total"] += float(r.get("elapsed_sec") or 0)
        s = r["status"]
        if s == "ok":
            d["ok"] += 1
        elif s == "no_results":
            d["no_results"] += 1
        elif s == "blocked":
            d["blocked"] += 1
        elif s == "timeout":
            d["timeout"] += 1
        else:
            d["failed_other"] += 1

    lines.append("## Per-channel results")
    lines.append("")
    lines.append("| Channel | OK | No results | Blocked | Timeout | Other fail | Total | OK % | Mean s/MPN |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for ch in channels_used:
        d = per_channel.get(ch, {"ok": 0, "no_results": 0, "blocked": 0,
                                 "timeout": 0, "failed_other": 0, "total": 0,
                                 "elapsed_total": 0.0})
        pct = (100.0 * d["ok"] / d["total"]) if d["total"] else 0.0
        mean = (d["elapsed_total"] / d["total"]) if d["total"] else 0.0
        lines.append(
            f"| {ch} | {d['ok']} | {d['no_results']} | {d['blocked']} | "
            f"{d['timeout']} | {d['failed_other']} | {d['total']} | "
            f"{pct:.1f}% | {mean:.1f} |"
        )
    lines.append("")

    # Cross-channel coverage: for each MPN, count how many channels returned ok
    coverage_hist: dict[int, int] = {}
    for c in compare_rows:
        n_ok = sum(1 for ch in channels_used if c.get(f"{ch.lower()}_status") == "ok")
        coverage_hist[n_ok] = coverage_hist.get(n_ok, 0) + 1
    lines.append("## Cross-channel coverage per chip")
    lines.append("")
    lines.append("| # channels returning ok | # chips |")
    lines.append("|---|---|")
    for n in sorted(coverage_hist.keys(), reverse=True):
        lines.append(f"| {n} | {coverage_hist[n]} |")
    lines.append("")

    # Highlights — top 5 stock per channel
    lines.append("## Highlights — top 5 by in-stock quantity")
    lines.append("")
    for ch in channels_used:
        lines.append(f"### {ch}")
        lines.append("")
        ok_rows = [r for r in rows if r["channel"] == ch and r["status"] == "ok"
                   and r.get("stock_now_qty") not in (None, "", 0)]
        if not ok_rows:
            lines.append("_no in-stock results_")
            lines.append("")
            continue
        top = sorted(ok_rows, key=lambda r: r.get("stock_now_qty") or 0, reverse=True)[:5]
        lines.append("| input_mpn | returned_mfr | stock_now_qty | price_at_qty_1 | currency |")
        lines.append("|---|---|---|---|---|")
        for r in top:
            qty = r.get("stock_now_qty")
            qty_s = f"{qty:,}" if isinstance(qty, int) else str(qty)
            price = r.get("price_at_qty_1")
            price_s = "" if price is None else str(price)
            lines.append(
                f"| `{r['input_mpn']}` | {r['returned_mfr']} | {qty_s} | {price_s} | {r['currency']} |"
            )
        lines.append("")

    # Manufacturer mismatches per channel
    lines.append("## Manufacturer mismatches (returned_mfr ≠ expected_mfr after normalization)")
    lines.append("")
    for ch in channels_used:
        mismatches = [r for r in rows
                      if r["channel"] == ch and r["status"] == "ok"
                      and r["returned_mfr"] and r["mfr_match"] is False]
        if not mismatches:
            lines.append(f"- **{ch}:** none")
            continue
        lines.append(f"- **{ch}: {len(mismatches)} chip(s)**")
        lines.append("")
        lines.append("  | input_mpn | expected_mfr | returned_mfr |")
        lines.append("  |---|---|---|")
        for r in mismatches[:20]:
            lines.append(f"  | `{r['input_mpn']}` | {r['expected_mfr']} | {r['returned_mfr']} |")
        if len(mismatches) > 20:
            lines.append(f"  | …and {len(mismatches)-20} more (see batch_index.csv) |  |  |")
        lines.append("")

    if skipped:
        lines.append("## Skipped xlsx rows")
        lines.append("")
        lines.append("| row | raw_mpn | raw_mfr | reason |")
        lines.append("|---|---|---|---|")
        for s in skipped:
            lines.append(f"| {s['row']} | `{s['raw_mpn']}` | {s['raw_mfr']} | {s['reason']} |")
        lines.append("")

    lines.append("## Files in this batch folder")
    lines.append("")
    for name, what in [
        ("batch_summary.md", "this file"),
        ("batch_index.csv / .xlsx", "long form — one row per (MPN × channel)"),
        ("batch_compare.csv / .xlsx", "wide form — one row per MPN, ~43 cols across all 4 channels"),
        ("batch_index.json", "machine-readable long form (records + subprocess output tails)"),
        ("batch_input.csv", "verbatim (MPN, expected_mfr) input rows"),
        ("failures.md", "non-ok rows grouped by channel"),
        ("Test_<sanitized_mpn>_<CHANNEL>/", "per-MPN-per-channel run folder"),
    ]:
        lines.append(f"- `{name}` — {what}")
    lines.append("")

    (batch_dir / "batch_summary.md").write_text("\n".join(lines), encoding="utf-8")


# --- main ------------------------------------------------------------------


def _parse_only(s: str | None) -> list[str]:
    if not s:
        return CHANNEL_ORDER.copy()
    requested = [tok.strip().upper() for tok in s.split(",") if tok.strip()]
    unknown = [c for c in requested if c not in CHANNELS]
    if unknown:
        raise SystemExit(f"Unknown channel(s): {unknown}. Choose from {CHANNEL_ORDER}.")
    return [c for c in CHANNEL_ORDER if c in requested]


def process_one_channel(
    channel: str,
    mpn: str,
    mfr: str,
    batch_dir: Path,
    resume_mode: bool,
) -> tuple[str, dict, dict]:
    """Run (or resume) one (MPN × channel) call. Returns (channel, index_row, record_log).

    Pure per-call work — no shared state, no printing. Safe to dispatch from a
    thread pool because the underlying network I/O happens inside a subprocess
    (truly parallel, not GIL-blocked).
    """
    safe = _safe_folder(mpn)
    run_dir = batch_dir / f"Test_{safe}_{channel}"
    run_dir.mkdir(parents=True, exist_ok=True)

    if resume_mode and (run_dir / f"{safe}.json").exists():
        record = load_record(run_dir, mpn)
        extracted, n_var = pick_best_extracted(record or {}, channel, run_dir, mpn)
        sub_result = {"status": "ok", "elapsed_sec": None, "error": ""}
        row = make_index_row(mpn, mfr, channel, sub_result, record, extracted, n_var, run_dir)
        record_log = {
            "channel": channel, "input_mpn": mpn, "expected_mfr": mfr,
            "elapsed_sec": None, "subprocess_status": "resume_skipped",
            "record": record, "extracted_best": extracted,
        }
        return channel, row, record_log

    sub_result = run_subprocess(channel, mpn, run_dir)
    record = load_record(run_dir, mpn)
    extracted, n_var = pick_best_extracted(record or {}, channel, run_dir, mpn)
    row = make_index_row(mpn, mfr, channel, sub_result, record, extracted, n_var, run_dir)
    record_log = {
        "channel": channel, "input_mpn": mpn, "expected_mfr": mfr,
        "elapsed_sec": sub_result.get("elapsed_sec"),
        "subprocess_status": sub_result.get("status"),
        "stdout_tail": sub_result.get("stdout_tail"),
        "stderr_tail": sub_result.get("stderr_tail"),
        "record": record,
        "extracted_best": extracted,
        "error": sub_result.get("error"),
    }
    return channel, row, record_log


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX,
                        help=f"Path to the chip-list xlsx (default: {DEFAULT_XLSX}).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N valid MPNs (dry-run aid).")
    parser.add_argument("--only", type=str, default=None,
                        help="Comma-separated subset of channels (e.g. LCSC,HQEW).")
    parser.add_argument("--throttle", type=float, default=THROTTLE_SECONDS,
                        help=f"Sleep between chips (s). Default {THROTTLE_SECONDS}. "
                             "With parallel channels, this is a politeness gap between chips, "
                             "not between every channel call.")
    parser.add_argument("--resume", action="store_true",
                        help="Reuse the most recent BatchTest_* folder and skip rows whose "
                             "per-channel <safe_mpn>.json already exists.")
    parser.add_argument("--sequential", action="store_true",
                        help="Run channels one at a time per chip (old behavior). Default is "
                             "to fan out all selected channels concurrently per chip — each "
                             "channel hits a different domain so there's no per-vendor "
                             "rate-limit collision. Wallclock per chip drops from sum(channels) "
                             "to max(channels), typically a 50–60%% speedup.")
    args = parser.parse_args(argv[1:])

    channels_used = _parse_only(args.only)
    chips, skipped = load_chip_list(args.xlsx)
    if args.limit:
        chips = chips[: args.limit]
    print(f"Loaded {len(chips)} chip rows ({len(skipped)} skipped) from {args.xlsx.name}")
    print(f"Channels: {channels_used}")

    # Batch folder selection
    if args.resume:
        existing = sorted(SCRAPER_TEST_ROOT.glob("BatchTest_*"))
        if not existing:
            print("[resume] no existing BatchTest_* folder; creating new one.")
            args.resume = False
    if not args.resume:
        now = datetime.now()
        batch_dir = SCRAPER_TEST_ROOT / f"BatchTest_{now.strftime('%Y%m%d')}_{now.strftime('%H_%M_%S')}"
    else:
        batch_dir = sorted(SCRAPER_TEST_ROOT.glob("BatchTest_*"))[-1]
        print(f"[resume] using existing batch folder {batch_dir.name}")
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Batch folder: {batch_dir}")

    write_batch_input_csv(chips, batch_dir / "batch_input.csv")

    started = datetime.now(timezone.utc)
    index_rows: list[dict] = []
    compare_rows: list[dict] = []
    all_records: list[dict] = []

    parallel = (not args.sequential) and len(channels_used) > 1
    mode_label = "parallel" if parallel else "sequential"
    print(f"Channel dispatch: {mode_label} ({len(channels_used)} channel(s) per chip)")

    for i, chip in enumerate(chips, 1):
        mpn = chip["input_mpn"]
        mfr = chip["expected_mfr"]
        chip_started = time.time()
        print(f"[{i:>3}/{len(chips)}] {mpn}  (expected {mfr})")
        per_chip_rows: dict[str, dict] = {}

        if parallel:
            # Fan out: one subprocess per channel, all running concurrently.
            # Each channel hits a different domain, so there's no per-vendor
            # rate-limit collision. The chip's wallclock is max(channel times)
            # instead of sum, which is the primary speedup.
            with ThreadPoolExecutor(max_workers=len(channels_used)) as ex:
                futures = [
                    ex.submit(process_one_channel, ch, mpn, mfr, batch_dir, args.resume)
                    for ch in channels_used
                ]
                channel_results = [f.result() for f in futures]
            # Re-order back to channels_used order for deterministic output
            channel_results.sort(key=lambda t: channels_used.index(t[0]))
        else:
            channel_results = [
                process_one_channel(ch, mpn, mfr, batch_dir, args.resume)
                for ch in channels_used
            ]

        for channel, row, record_log in channel_results:
            index_rows.append(row)
            per_chip_rows[channel] = row
            all_records.append(record_log)
            qty = row.get("stock_now_qty")
            qty_s = f"{qty:,}" if isinstance(qty, int) else (str(qty) if qty is not None else "?")
            n_var = row.get("num_variants", 0)
            elapsed = row.get("elapsed_sec")
            if record_log.get("subprocess_status") == "resume_skipped":
                print(f"      {channel}: [resume] {row['status']}  qty={qty_s}  variants={n_var}")
            else:
                el_s = f"{elapsed:.1f}" if isinstance(elapsed, (int, float)) else "?"
                print(f"      {channel}: {row['status']}  qty={qty_s}  variants={n_var}  ({el_s} s)")

        if parallel:
            chip_wall = time.time() - chip_started
            print(f"      chip wallclock: {chip_wall:.1f} s")

        if len(per_chip_rows) == len(channels_used):
            compare_rows.append(make_compare_row(per_chip_rows))

        # Politeness gap between chips. With parallel channels this fires
        # once per chip (was once per channel call in sequential mode).
        if args.throttle > 0 and i < len(chips):
            time.sleep(args.throttle)

    finished = datetime.now(timezone.utc)

    # Outputs
    write_csv(index_rows, INDEX_COLUMNS, batch_dir / "batch_index.csv")
    write_xlsx(index_rows, INDEX_COLUMNS, batch_dir / "batch_index.xlsx", "batch_index")
    if compare_rows:
        write_csv(compare_rows, COMPARE_COLUMNS, batch_dir / "batch_compare.csv")
        write_xlsx(compare_rows, COMPARE_COLUMNS, batch_dir / "batch_compare.xlsx", "batch_compare")
    (batch_dir / "batch_index.json").write_text(
        json.dumps(all_records, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    write_failures(index_rows, batch_dir / "failures.md")
    write_summary_md(
        chips, index_rows, compare_rows, skipped, channels_used,
        batch_dir, started.astimezone(), finished.astimezone(),
    )

    print(f"\nDone. {len(index_rows)} index rows, {len(compare_rows)} compare rows.")
    print(f"Wrote: {batch_dir}")

    # Refresh scraper/README.md status block so bare-shell runs (no Claude Code
    # session, hence no PostToolUse hook) also keep the doc current. Best-
    # effort: never block on it.
    regen = PROJECT_ROOT / "scraper" / "scripts" / "_update_readme_status.py"
    if regen.exists():
        try:
            subprocess.run(
                [sys.executable, str(regen)],
                timeout=10,
                check=False,
                capture_output=True,
            )
            print(f"Refreshed: scraper/README.md status block")
        except (subprocess.TimeoutExpired, OSError):
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
