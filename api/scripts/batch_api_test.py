"""Batch-run every chip in ref/Chip_DataSource_Master.xlsx through both
distributor APIs (Mouser + Digikey) and consolidate the results.

For each (input_mpn, expected_mfr) row:
  - call api_mouser.call_api  (~1 round-trip per call)
  - call api_digikey.call_api (token shared across batch via in-process cache)
  - extract a "best variant" record per channel
  - append to long-form batch_index + wide-form batch_compare aggregates

Outputs land under `test/api_test/BatchTest_<YYYYMMDD>_<HH_MM_SS>/`:
  - batch_summary.md                 — TL;DR + per-channel stats + highlights
  - batch_index.csv / .xlsx          — long-form (one row per MPN × channel)
  - batch_compare.csv / .xlsx        — wide-form (one row per MPN)
  - batch_index.json                 — machine-readable
  - batch_input.csv                  — verbatim (MPN, expected_mfr) from xlsx
  - failures.md                      — per-channel failures with attempts log
  - Test_<sanitized_mpn>_<CHANNEL>/  — per-MPN run folders (parent_summary.md,
                                       <mpn>.json, raw_response.json, per-variant
                                       subfolders) — same shape as a single-MPN
                                       call would produce.

Usage:
    .venv/Scripts/python.exe api/scripts/batch_api_test.py            # full sweep
    .venv/Scripts/python.exe api/scripts/batch_api_test.py --limit 3  # dry-run
    .venv/Scripts/python.exe api/scripts/batch_api_test.py --xlsx PATH

The script is idempotent — each invocation creates a fresh timestamped batch
folder. Re-running does NOT overwrite previous batches.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from dotenv import load_dotenv

# Project paths --------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_TEST_ROOT = PROJECT_ROOT / "test" / "api_test"
DEFAULT_XLSX = PROJECT_ROOT / "ref" / "Chip_DataSource_Master.xlsx"
ENV_PATH = PROJECT_ROOT / "api" / ".env"

# Reuse the single-call api clients ------------------------------------------
sys.path.insert(0, str(PROJECT_ROOT / "api" / "scripts"))
import api_mouser  # type: ignore  # noqa: E402
import api_digikey  # type: ignore  # noqa: E402

THROTTLE_SECONDS = 0.3

# --- helpers ----------------------------------------------------------------


def _safe_folder(mpn: str) -> str:
    """Mirror the single-call sanitization rule from api_mouser/api_digikey."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", mpn) or "UNKNOWN"


def _looks_like_real_mpn(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    if len(s) < 3:
        return False
    if "缺失" in s or "TBD" in s.upper() or "TODO" in s.upper():
        return False
    # Reject pure-Chinese strings (no ASCII letters/digits at all)
    if not re.search(r"[A-Za-z0-9]", s):
        return False
    return True


def _norm_mfr(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _mfr_match(expected: str | None, returned: str | None) -> bool:
    """Substring after case+symbol normalization. Empty expected → False."""
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


# --- input loading ----------------------------------------------------------


def load_chip_list(xlsx_path: Path) -> list[dict]:
    """Return [{row, input_mpn, expected_mfr}, ...] for valid rows only."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.worksheets[0]
    # Header is at row 4 ('MPN' | '厂商'); data starts at row 5.
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


# --- per-channel best-variant selection -------------------------------------


def pick_best_variant(payload: dict | None, channel: str, input_mpn: str) -> dict | None:
    """Given the raw Mouser/Digikey payload, choose the best variant dict to
    represent this MPN, and return its NORMALIZED record (mirroring what the
    single-call scripts produce per-variant). Returns None when nothing is
    usable.
    """
    if not payload:
        return None
    if channel == "MOUSER":
        parts = (payload.get("SearchResults") or {}).get("Parts") or []
        if not parts:
            return None
        # Pick the variant whose MPN matches input_mpn exactly (case-insensitive),
        # else the one with the highest in-stock quantity.
        exact = [
            p for p in parts
            if str(p.get("ManufacturerPartNumber", "")).strip().lower()
            == input_mpn.strip().lower()
        ]
        pool = exact or parts

        def _stock(p):
            try:
                return int(str(p.get("AvailabilityInStock") or "0").split()[0])
            except (ValueError, IndexError):
                return 0
        best_raw = max(pool, key=_stock)
        return api_mouser.normalize_part(best_raw, input_mpn)
    elif channel == "DIGIKEY":
        exact = payload.get("ExactMatches") or []
        products = payload.get("Products") or []
        candidates: list[dict] = []
        seen_mpns: set[str] = set()
        for p in exact + products:
            mpn = p.get("ManufacturerProductNumber") or ""
            if mpn and mpn not in seen_mpns:
                candidates.append(p)
                seen_mpns.add(mpn)
        if not candidates:
            return None
        target = input_mpn.strip().lower()
        match = next(
            (p for p in candidates
             if (p.get("ManufacturerProductNumber") or "").strip().lower() == target),
            None,
        )
        chosen = match or candidates[0]
        return api_digikey.normalize_product(chosen, input_mpn)
    return None


# --- per-channel call wrappers ----------------------------------------------


def run_mouser(input_mpn: str, run_dir: Path) -> dict:
    """Call api_mouser, write its per-MPN folder, return a row dict for indexing."""
    rec = api_mouser.call_api(input_mpn, run_dir)
    payload = rec.pop("raw_payload", None)
    # Persist raw + per-variant folders + parent_summary + parent json
    if payload is not None:
        (run_dir / "raw_response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    variants_for_summary: list[dict] = []
    chosen_ex: dict | None = None
    n_variants = 0
    if rec.get("status") == "ok" and payload is not None:
        parts = (payload.get("SearchResults") or {}).get("Parts") or []
        seen: dict[str, dict] = {}
        for raw_part in parts:
            mpn = raw_part.get("ManufacturerPartNumber") or "UNKNOWN"
            ex_candidate = api_mouser.normalize_part(raw_part, input_mpn)
            prev = seen.get(mpn)
            if prev is None or (
                (ex_candidate.get("stock_now_qty") or 0)
                > (prev["extracted"].get("stock_now_qty") or 0)
            ):
                seen[mpn] = {"raw": raw_part, "extracted": ex_candidate}
        for mpn, bundle in seen.items():
            info = api_mouser.write_variant(rec, bundle["extracted"], bundle["raw"], run_dir, mpn)
            variants_for_summary.append(info)
        n_variants = len(variants_for_summary)
        chosen_ex = pick_best_variant(payload, "MOUSER", input_mpn)
    parent_rec = dict(rec)
    parent_rec["variants_summary"] = [
        {
            "manufacturer_part_number": v["extracted"].get("manufacturer_part_number"),
            "mouser_part_number": v["extracted"].get("mouser_part_number"),
            "stock_now_qty": v["extracted"].get("stock_now_qty"),
            "stock_future_qty": v["extracted"].get("stock_future_qty"),
            "stock_future_ship_text": v["extracted"].get("stock_future_ship_text"),
            "subdir": v["folder"],
        }
        for v in variants_for_summary
    ]
    (run_dir / f"{_safe_folder(input_mpn)}.json").write_text(
        json.dumps(parent_rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    api_mouser.write_parent_summary(parent_rec, variants_for_summary, run_dir)
    return {"record": rec, "chosen_extracted": chosen_ex, "num_variants": n_variants}


def run_digikey(input_mpn: str, run_dir: Path) -> dict:
    rec = api_digikey.call_api(input_mpn, run_dir)
    payload = rec.pop("raw_payload", None)
    if payload is not None:
        (run_dir / "raw_response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    variants_for_summary: list[dict] = []
    chosen_ex: dict | None = None
    n_variants = 0
    if rec.get("status") == "ok" and payload is not None:
        exact = payload.get("ExactMatches") or []
        products = payload.get("Products") or []
        seen_mpns: set[str] = set()
        candidates: list[dict] = []
        for p in exact + products:
            mpn = p.get("ManufacturerProductNumber") or ""
            if mpn and mpn not in seen_mpns:
                candidates.append(p)
                seen_mpns.add(mpn)
        for product in candidates:
            mpn = product.get("ManufacturerProductNumber") or "UNKNOWN"
            extracted = api_digikey.normalize_product(product, input_mpn)
            info = api_digikey.write_variant(rec, extracted, product, run_dir, mpn)
            variants_for_summary.append(info)
        n_variants = len(variants_for_summary)
        chosen_ex = pick_best_variant(payload, "DIGIKEY", input_mpn)
    parent_rec = dict(rec)
    parent_rec["variants_summary"] = [
        {
            "manufacturer_part_number": v["extracted"].get("manufacturer_part_number"),
            "digikey_part_number": v["extracted"].get("digikey_part_number"),
            "stock_now_qty": v["extracted"].get("stock_now_qty"),
            "stock_future_qty": v["extracted"].get("stock_future_qty"),
            "stock_future_ship_text": v["extracted"].get("stock_future_ship_text"),
            "subdir": v["folder"],
        }
        for v in variants_for_summary
    ]
    (run_dir / f"{_safe_folder(input_mpn)}.json").write_text(
        json.dumps(parent_rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    api_digikey.write_parent_summary(parent_rec, variants_for_summary, run_dir)
    return {"record": rec, "chosen_extracted": chosen_ex, "num_variants": n_variants}


# --- index row construction -------------------------------------------------


def derive_price_summary(prices: list[dict]) -> dict:
    """Return (price_at_qty_1, min_break_qty, lowest_unit_price, num_price_tiers)."""
    if not prices:
        return {"price_at_qty_1": None, "min_break_qty": None,
                "lowest_unit_price": None, "num_price_tiers": 0}
    tiers = [t for t in prices if isinstance(t, dict)]
    # Numeric helper
    def _num(t):
        v = t.get("unit_price_float")
        if v is not None:
            return v
        return _parse_price_str(t.get("unit_price"))
    # Smallest break
    by_qty = sorted([t for t in tiers if t.get("min_qty") is not None],
                    key=lambda t: t.get("min_qty"))
    min_break = by_qty[0] if by_qty else None
    largest = by_qty[-1] if by_qty else None
    # Tier with qty == 1 (if any)
    one_break = next(
        (t for t in by_qty if t.get("min_qty") == 1), None
    )
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
    bundle: dict,
    run_subdir: Path,
    error: str | None = None,
) -> dict:
    rec = bundle.get("record") if bundle else {}
    ex = bundle.get("chosen_extracted") if bundle else None
    ex = ex or {}
    n_variants = bundle.get("num_variants", 0) if bundle else 0

    prices_summary = derive_price_summary(ex.get("prices") or [])
    status = (rec or {}).get("status") or ("exception" if error else "no_results")
    returned_mfr = ex.get("manufacturer") or ""
    return {
        "input_mpn": input_mpn,
        "expected_mfr": expected_mfr,
        "channel": channel,
        "status": status,
        "num_variants": n_variants,
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
        "currency": ex.get("currency") or "",
        "datasheet_url": ex.get("datasheet_url") or "",
        "run_subdir": str(run_subdir.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "error": error or "",
    }


INDEX_COLUMNS = [
    "input_mpn", "expected_mfr", "channel", "status", "num_variants",
    "returned_mpn", "returned_mfr", "mfr_match",
    "stock_now_qty", "stock_future_qty", "stock_future_ship_text",
    "price_at_qty_1", "min_break_qty", "lowest_unit_price", "num_price_tiers",
    "currency", "datasheet_url", "run_subdir", "error",
]


def make_compare_row(mouser_row: dict, digikey_row: dict) -> dict:
    """Combine the two long-form rows into one wide row for a single MPN."""
    m = mouser_row
    d = digikey_row
    m_has = (m.get("stock_now_qty") or 0) > 0
    d_has = (d.get("stock_now_qty") or 0) > 0
    if m_has and d_has:
        stock_disagreement = "both_have_stock"
    elif m_has and not d_has:
        stock_disagreement = "only_mouser"
    elif d_has and not m_has:
        stock_disagreement = "only_digikey"
    else:
        stock_disagreement = "neither"
    return {
        "input_mpn": m["input_mpn"],
        "expected_mfr": m["expected_mfr"],
        # Mouser
        "mouser_status": m["status"],
        "mouser_returned_mpn": m["returned_mpn"],
        "mouser_returned_mfr": m["returned_mfr"],
        "mouser_stock_now_qty": m["stock_now_qty"],
        "mouser_stock_future_qty": m["stock_future_qty"],
        "mouser_stock_future_ship_text": m["stock_future_ship_text"],
        "mouser_price_at_qty_1": m["price_at_qty_1"],
        "mouser_currency": m["currency"],
        "mouser_datasheet_url": m["datasheet_url"],
        "mfr_match_mouser": m["mfr_match"],
        # Digikey
        "digikey_status": d["status"],
        "digikey_returned_mpn": d["returned_mpn"],
        "digikey_returned_mfr": d["returned_mfr"],
        "digikey_stock_now_qty": d["stock_now_qty"],
        "digikey_stock_future_qty": d["stock_future_qty"],
        "digikey_stock_future_ship_text": d["stock_future_ship_text"],
        "digikey_price_at_qty_1": d["price_at_qty_1"],
        "digikey_currency": d["currency"],
        "digikey_datasheet_url": d["datasheet_url"],
        "mfr_match_digikey": d["mfr_match"],
        # Comparison
        "stock_now_disagreement": stock_disagreement,
    }


COMPARE_COLUMNS = [
    "input_mpn", "expected_mfr",
    "mouser_status", "mouser_returned_mpn", "mouser_returned_mfr",
    "mouser_stock_now_qty", "mouser_stock_future_qty",
    "mouser_stock_future_ship_text",
    "mouser_price_at_qty_1", "mouser_currency", "mouser_datasheet_url",
    "mfr_match_mouser",
    "digikey_status", "digikey_returned_mpn", "digikey_returned_mfr",
    "digikey_stock_now_qty", "digikey_stock_future_qty",
    "digikey_stock_future_ship_text",
    "digikey_price_at_qty_1", "digikey_currency", "digikey_datasheet_url",
    "mfr_match_digikey",
    "stock_now_disagreement",
]


# --- writers ----------------------------------------------------------------


def _cell_value_for_excel(v):
    """openpyxl accepts None / str / int / float / bool. Anything else → str()."""
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
    # Freeze header + reasonable column widths
    ws.freeze_panes = "A2"
    for col_idx, col in enumerate(columns, 1):
        max_len = max([len(col)] + [
            len(str(r.get(col))) for r in rows if r.get(col) is not None
        ][:300])
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 2, 10), 50)
    wb.save(path)


def write_batch_input_csv(chips: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["xlsx_row", "input_mpn", "expected_mfr"])
        for c in chips:
            w.writerow([c["row"], c["input_mpn"], c["expected_mfr"]])


# --- summary + failure docs -------------------------------------------------


def write_failures(rows: list[dict], path: Path) -> None:
    failures = [r for r in rows if r["status"] != "ok"]
    lines = ["# Batch API failures", ""]
    if not failures:
        lines.append("_No failures._")
    else:
        lines.append(f"{len(failures)} non-ok rows (out of {len(rows)} total).")
        lines.append("")
        lines.append("| input_mpn | expected_mfr | channel | status | error | run_subdir |")
        lines.append("|---|---|---|---|---|---|")
        for r in failures:
            err = (r.get("error") or "").replace("|", "\\|").replace("\n", " ")[:200]
            lines.append(
                f"| `{r['input_mpn']}` | {r['expected_mfr']} | {r['channel']} | "
                f"{r['status']} | {err} | `{r['run_subdir']}` |"
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_summary_md(
    chips: list[dict],
    rows: list[dict],
    compare_rows: list[dict],
    skipped: list[dict],
    batch_dir: Path,
    started: datetime,
    finished: datetime,
) -> None:
    lines: list[str] = []
    elapsed = (finished - started).total_seconds()
    lines.append(f"# Batch API sweep — {len(chips)} chips × Mouser + Digikey")
    lines.append("")
    lines.append(f"- **Started:** {started.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **Finished:** {finished.strftime('%Y-%m-%d %H:%M:%S')}  (elapsed {elapsed:.1f} s)")
    lines.append(f"- **Chips processed:** {len(chips)}  ({len(skipped)} row(s) skipped from xlsx)")
    lines.append(f"- **Total API calls:** {len(rows)}")
    lines.append("")

    # Per-channel pass rate
    per_channel: dict[str, dict] = {}
    for r in rows:
        d = per_channel.setdefault(r["channel"], {"ok": 0, "no_results": 0,
                                                  "failed_other": 0, "total": 0})
        d["total"] += 1
        if r["status"] == "ok":
            d["ok"] += 1
        elif r["status"] == "no_results":
            d["no_results"] += 1
        else:
            d["failed_other"] += 1

    lines.append("## Per-channel pass rate")
    lines.append("")
    lines.append("| Channel | OK | No results | Failed | Total | OK % |")
    lines.append("|---|---|---|---|---|---|")
    for ch in ("MOUSER", "DIGIKEY"):
        d = per_channel.get(ch, {"ok": 0, "no_results": 0, "failed_other": 0, "total": 0})
        pct = (100.0 * d["ok"] / d["total"]) if d["total"] else 0.0
        lines.append(f"| {ch} | {d['ok']} | {d['no_results']} | {d['failed_other']} | {d['total']} | {pct:.1f}% |")
    lines.append("")

    # Cross-channel agreement
    both_ok = 0
    both_have_stock = 0
    only_mouser_stock = 0
    only_digikey_stock = 0
    neither_stock = 0
    mfr_mismatches_mouser: list[dict] = []
    mfr_mismatches_digikey: list[dict] = []
    for c in compare_rows:
        if c["mouser_status"] == "ok" and c["digikey_status"] == "ok":
            both_ok += 1
            d = c["stock_now_disagreement"]
            if d == "both_have_stock":
                both_have_stock += 1
            elif d == "only_mouser":
                only_mouser_stock += 1
            elif d == "only_digikey":
                only_digikey_stock += 1
            else:
                neither_stock += 1
        if c["mouser_status"] == "ok" and c["mfr_match_mouser"] is False:
            mfr_mismatches_mouser.append(c)
        if c["digikey_status"] == "ok" and c["mfr_match_digikey"] is False:
            mfr_mismatches_digikey.append(c)

    lines.append("## Cross-channel agreement (only chips where BOTH channels returned ok)")
    lines.append("")
    lines.append(f"- Both channels returned a usable result for **{both_ok}** chips.")
    lines.append(f"- Both have in-stock inventory: **{both_have_stock}**")
    lines.append(f"- Only Mouser has stock: **{only_mouser_stock}**")
    lines.append(f"- Only Digikey has stock: **{only_digikey_stock}**")
    lines.append(f"- Neither has stock (factory-order only): **{neither_stock}**")
    lines.append("")

    # Highlights — top stock per channel
    def _top(channel: str, key: str, n: int = 5, *, reverse: bool = True) -> list[dict]:
        ok_rows = [r for r in rows if r["channel"] == channel and r["status"] == "ok"
                   and r.get(key) not in (None, "", 0)]
        return sorted(ok_rows, key=lambda r: r[key], reverse=reverse)[:n]

    lines.append("## Highlights — top 5 by in-stock quantity")
    lines.append("")
    for ch in ("MOUSER", "DIGIKEY"):
        lines.append(f"### {ch}")
        lines.append("")
        lines.append("| input_mpn | returned_mfr | stock_now_qty | price_at_qty_1 | currency |")
        lines.append("|---|---|---|---|---|")
        for r in _top(ch, "stock_now_qty"):
            lines.append(
                f"| `{r['input_mpn']}` | {r['returned_mfr']} | {r['stock_now_qty']:,} | "
                f"{r['price_at_qty_1'] if r['price_at_qty_1'] is not None else ''} | "
                f"{r['currency']} |"
            )
        lines.append("")

    # Manufacturer mismatches
    lines.append("## Manufacturer mismatches (returned_mfr ≠ expected_mfr after normalization)")
    lines.append("")
    for ch, mismatches in (("Mouser", mfr_mismatches_mouser),
                           ("Digikey", mfr_mismatches_digikey)):
        if not mismatches:
            lines.append(f"- **{ch}:** none")
            continue
        lines.append(f"- **{ch}: {len(mismatches)} chip(s)**")
        lines.append("")
        lines.append(f"  | input_mpn | expected_mfr | returned_mfr |")
        lines.append(f"  |---|---|---|")
        for c in mismatches[:20]:
            mfr = (c["mouser_returned_mfr"] if ch == "Mouser" else c["digikey_returned_mfr"])
            lines.append(f"  | `{c['input_mpn']}` | {c['expected_mfr']} | {mfr} |")
        if len(mismatches) > 20:
            lines.append(f"  | …and {len(mismatches)-20} more (see `batch_compare.csv`) |  |  |")
        lines.append("")

    # Skipped rows
    if skipped:
        lines.append("## Skipped xlsx rows")
        lines.append("")
        lines.append("| row | raw_mpn | raw_mfr | reason |")
        lines.append("|---|---|---|---|")
        for s in skipped:
            lines.append(
                f"| {s['row']} | `{s['raw_mpn']}` | {s['raw_mfr']} | {s['reason']} |"
            )
        lines.append("")

    # Files
    lines.append("## Files in this batch folder")
    lines.append("")
    for name, what in [
        ("batch_summary.md", "this file"),
        ("batch_index.csv / .xlsx", "long form — one row per (MPN × channel)"),
        ("batch_compare.csv / .xlsx", "wide form — one row per MPN with both channels side-by-side"),
        ("batch_index.json", "machine-readable long form"),
        ("batch_input.csv", "verbatim (MPN, expected_mfr) input rows"),
        ("failures.md", "non-ok rows with error excerpt"),
        ("Test_<sanitized_mpn>_MOUSER/", "per-MPN Mouser run folder"),
        ("Test_<sanitized_mpn>_DIGIKEY/", "per-MPN Digikey run folder"),
    ]:
        lines.append(f"- `{name}` — {what}")
    lines.append("")

    (batch_dir / "batch_summary.md").write_text("\n".join(lines), encoding="utf-8")


# --- main -------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX,
                        help=f"Path to the chip-list xlsx (default: {DEFAULT_XLSX})")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N valid MPNs (dry-run aid).")
    parser.add_argument("--only", choices=("MOUSER", "DIGIKEY"), default=None,
                        help="Run only one channel (default: both).")
    parser.add_argument("--throttle", type=float, default=THROTTLE_SECONDS,
                        help="Sleep between successive API calls (seconds).")
    args = parser.parse_args(argv[1:])

    load_dotenv(ENV_PATH)
    if args.only != "DIGIKEY" and not os.environ.get("MOUSER_API_KEY"):
        print("ERROR: MOUSER_API_KEY missing in api/.env", file=sys.stderr)
        return 2
    if args.only != "MOUSER" and not (
        os.environ.get("DIGIKEY_CLIENT_ID") and os.environ.get("DIGIKEY_CLIENT_SECRET")
    ):
        print("ERROR: DIGIKEY_CLIENT_ID/SECRET missing in api/.env", file=sys.stderr)
        return 2

    chips, skipped = load_chip_list(args.xlsx)
    if args.limit:
        chips = chips[: args.limit]
        # Don't truncate `skipped` — it's a record of what we skipped, regardless of limit.
    print(f"Loaded {len(chips)} chip rows ({len(skipped)} skipped) from {args.xlsx.name}")

    now = datetime.now()
    batch_name = f"BatchTest_{now.strftime('%Y%m%d')}_{now.strftime('%H_%M_%S')}"
    batch_dir = API_TEST_ROOT / batch_name
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Batch folder: {batch_dir}")

    write_batch_input_csv(chips, batch_dir / "batch_input.csv")

    started = datetime.now(timezone.utc)
    index_rows: list[dict] = []
    compare_rows: list[dict] = []
    all_records: list[dict] = []  # for batch_index.json (full detail)

    channels_to_run = [args.only] if args.only else ["MOUSER", "DIGIKEY"]

    for i, chip in enumerate(chips, 1):
        mpn = chip["input_mpn"]
        mfr = chip["expected_mfr"]
        print(f"[{i:>3}/{len(chips)}] {mpn}  (expected {mfr})")
        per_chip_rows: dict[str, dict] = {}

        for channel in channels_to_run:
            safe = _safe_folder(mpn)
            run_dir = batch_dir / f"Test_{safe}_{channel}"
            run_dir.mkdir(parents=True, exist_ok=True)
            error: str | None = None
            bundle: dict | None = None
            t0 = time.time()
            try:
                if channel == "MOUSER":
                    bundle = run_mouser(mpn, run_dir)
                else:
                    bundle = run_digikey(mpn, run_dir)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            dt = time.time() - t0
            row = make_index_row(mpn, mfr, channel, bundle or {}, run_dir, error)
            index_rows.append(row)
            per_chip_rows[channel] = row
            all_records.append({
                "channel": channel,
                "input_mpn": mpn,
                "expected_mfr": mfr,
                "elapsed_sec": round(dt, 3),
                "record": (bundle or {}).get("record"),
                "extracted_best": (bundle or {}).get("chosen_extracted"),
                "error": error,
            })
            qty = row["stock_now_qty"] if row["stock_now_qty"] is not None else "?"
            print(f"      {channel}: {row['status']}  qty={qty}  "
                  f"variants={row['num_variants']}  ({dt:.2f} s)")
            if args.throttle > 0:
                time.sleep(args.throttle)

        # Build compare row (only when both channels were attempted)
        if "MOUSER" in per_chip_rows and "DIGIKEY" in per_chip_rows:
            compare_rows.append(
                make_compare_row(per_chip_rows["MOUSER"], per_chip_rows["DIGIKEY"])
            )

    finished = datetime.now(timezone.utc)

    # Write all output files
    write_csv(index_rows, INDEX_COLUMNS, batch_dir / "batch_index.csv")
    write_xlsx(index_rows, INDEX_COLUMNS, batch_dir / "batch_index.xlsx", "batch_index")
    if compare_rows:
        write_csv(compare_rows, COMPARE_COLUMNS, batch_dir / "batch_compare.csv")
        write_xlsx(compare_rows, COMPARE_COLUMNS, batch_dir / "batch_compare.xlsx", "batch_compare")
    (batch_dir / "batch_index.json").write_text(
        json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_failures(index_rows, batch_dir / "failures.md")
    write_summary_md(
        chips,
        index_rows,
        compare_rows,
        skipped,
        batch_dir,
        started.astimezone(),
        finished.astimezone(),
    )

    print(f"\nDone. {len(index_rows)} index rows, {len(compare_rows)} compare rows.")
    print(f"Wrote: {batch_dir}")

    # Refresh api/README.md status block so bare-shell runs (no Claude Code
    # session, hence no PostToolUse hook) also keep the doc current. Best-
    # effort: never block on it.
    regen = PROJECT_ROOT / "api" / "scripts" / "_update_readme_status.py"
    if regen.exists():
        try:
            import subprocess
            subprocess.run(
                [sys.executable, str(regen)],
                timeout=10,
                check=False,
                capture_output=True,
            )
            print(f"Refreshed: api/README.md status block")
        except (subprocess.TimeoutExpired, OSError):
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
