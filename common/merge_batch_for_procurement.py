"""Merge latest API + Scraper batch_index.csv into a procurement-facing xlsx.

Rules:
1. status=ok only.
2. Drop source HQEW_*.
3. API wins per (input_mpn, source): if the API CSV has any status=ok rows for
   that pair, drop ALL scraper rows for the same pair (even if API qty=0).
4. mfr_match=False rows are kept; flagged via column.
5. in_stock = stockpool_qty is not None AND > 0.
6. Cross-validation: for (mpn, source) where BOTH tracks have status=ok rows
   and no scraper qty matches any API warehouse qty, scraper rows go to a
   reference sheet (Sheet 3) for QA.

Output: test/merged/Merge_<api_ts>__<scr_ts>/merged_procurement.xlsx
        + merged_procurement.csv (= Sheet 2 contents)
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_TEST_DIR = PROJECT_ROOT / "test" / "api_test"
SCR_TEST_DIR = PROJECT_ROOT / "test" / "scraper_test"
MERGED_DIR = PROJECT_ROOT / "test" / "merged"

DROP_SOURCE_PREFIXES = ("HQEW_",)

# Final column order in the output sheets.
OUTPUT_COLUMNS = [
    "input_mpn", "expected_mfr", "source", "track", "in_stock",
    "returned_mpn", "vendor_sku", "returned_mfr", "mfr_match",
    "warehouse", "warehouse_idx", "ships_from",
    "stockpool_qty", "ship_text", "lead_time_days",
    "moq", "min_break_qty", "price_at_min_qty",
    "max_break_qty", "price_at_max_qty", "num_price_tiers", "currency",
    "is_mirror", "datasheet_url", "status", "run_subdir", "error",
]

MISMATCH_COLUMNS = [
    "input_mpn", "expected_mfr", "source", "in_stock",
    "returned_mpn", "warehouse", "stockpool_qty", "ship_text",
    "lead_time_days", "moq", "price_at_min_qty", "currency",
    "datasheet_url", "note", "run_subdir",
]

GREEN = PatternFill(start_color="FFC6EFCE", end_color="FFC6EFCE", fill_type="solid")
GREY = PatternFill(start_color="FFEEEEEE", end_color="FFEEEEEE", fill_type="solid")


# ---------- helpers ---------------------------------------------------------

def latest_batch(parent: Path) -> Path:
    candidates = sorted(
        (p for p in parent.iterdir() if p.is_dir() and p.name.startswith("BatchTest_")),
        key=lambda p: p.name,
    )
    if not candidates:
        raise SystemExit(f"No BatchTest_* folder in {parent}")
    return candidates[-1]


def parse_qty(s: str):
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def read_ok_rows(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "ok":
                continue
            src = r.get("source") or ""
            if any(src.startswith(p) for p in DROP_SOURCE_PREFIXES):
                continue
            # Normalize qty to int / None
            r["stockpool_qty"] = parse_qty(r.get("stockpool_qty", ""))
            rows.append(r)
    return rows


def detect_mirror(r: dict) -> bool:
    src = r.get("source") or ""
    wh = (r.get("warehouse") or "")
    if " — mirror" in wh:
        return True
    if src.startswith("FUTURE_") and "(global)" in wh:
        idx = (r.get("warehouse_idx") or "").strip()
        try:
            return int(idx) > 1
        except ValueError:
            return False
    if src.startswith("ELEMENT14_") and wh.startswith("Element14 (") and wh.endswith(")"):
        return True
    return False


def annotate(rows: list[dict], track: str) -> None:
    for r in rows:
        r["track"] = track
        qty = r.get("stockpool_qty")
        r["in_stock"] = bool(qty is not None and qty > 0)
        r["is_mirror"] = detect_mirror(r)


def classify_qty(api_qtys: list, scr_qtys: list) -> str:
    api_set = {q for q in api_qtys if q is not None}
    scr_set = {q for q in scr_qtys if q is not None}
    if not scr_set and not api_set:
        return "both_null"
    if not scr_set or not api_set:
        return "one_side_null"
    if api_set & scr_set:
        return "match"
    return "mismatch"


# ---------- writers ---------------------------------------------------------

def _cell_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (str, int, float)):
        return v
    return str(v)


def _style_workbook(ws, columns: list[str], rows: list[dict]) -> None:
    header_font = Font(bold=True)
    header_fill = PatternFill(start_color="FFE2E2E2", end_color="FFE2E2E2", fill_type="solid")
    for col_idx, col in enumerate(columns, 1):
        c = ws.cell(row=1, column=col_idx, value=col)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(vertical="center")
    for row_idx, r in enumerate(rows, 2):
        for col_idx, col in enumerate(columns, 1):
            ws.cell(row=row_idx, column=col_idx, value=_cell_value(r.get(col)))
    ws.freeze_panes = "A2"
    for col_idx, col in enumerate(columns, 1):
        sample = [str(r.get(col)) for r in rows if r.get(col) is not None][:300]
        max_len = max([len(col)] + [len(s) for s in sample] + [0])
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 2, 10), 50)


def _apply_row_fills(ws, columns: list[str], rows: list[dict]) -> None:
    ncols = len(columns)
    for row_idx, r in enumerate(rows, 2):
        if r.get("in_stock"):
            fill = GREEN
        elif r.get("stockpool_qty") == 0:
            fill = GREY
        else:
            continue
        for col_idx in range(1, ncols + 1):
            ws.cell(row=row_idx, column=col_idx).fill = fill


def write_csv(rows: list[dict], columns: list[str], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})


# ---------- main ------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", type=Path, default=None,
                    help="API BatchTest folder (default: newest under test/api_test/)")
    ap.add_argument("--scr", type=Path, default=None,
                    help="Scraper BatchTest folder (default: newest under test/scraper_test/)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir (default: test/merged/Merge_<api_ts>__<scr_ts>/)")
    args = ap.parse_args()

    api_dir = args.api or latest_batch(API_TEST_DIR)
    scr_dir = args.scr or latest_batch(SCR_TEST_DIR)
    api_csv = api_dir / "batch_index.csv"
    scr_csv = scr_dir / "batch_index.csv"
    if not api_csv.exists():
        raise SystemExit(f"missing {api_csv}")
    if not scr_csv.exists():
        raise SystemExit(f"missing {scr_csv}")

    api_ts = re.sub(r"^BatchTest_", "", api_dir.name)
    scr_ts = re.sub(r"^BatchTest_", "", scr_dir.name)
    out_dir = args.out or (MERGED_DIR / f"Merge_{api_ts}__{scr_ts}")
    out_dir.mkdir(parents=True, exist_ok=True)

    api_rows = read_ok_rows(api_csv)
    scr_rows = read_ok_rows(scr_csv)
    annotate(api_rows, "api")
    annotate(scr_rows, "scraper")

    # Primary merge — API wins per (mpn, source).
    api_keys = {(r["input_mpn"], r["source"]) for r in api_rows}
    scr_kept = [r for r in scr_rows if (r["input_mpn"], r["source"]) not in api_keys]
    scr_suppressed = [r for r in scr_rows if (r["input_mpn"], r["source"]) in api_keys]
    merged = list(api_rows) + scr_kept

    # Cross-validation sheet — only over suppressed scraper rows for which API exists.
    api_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in api_rows:
        api_by_key[(r["input_mpn"], r["source"])].append(r)
    scr_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in scr_suppressed:
        scr_by_key[(r["input_mpn"], r["source"])].append(r)

    mismatch_rows: list[dict] = []
    for key, scr_group in scr_by_key.items():
        api_group = api_by_key[key]
        verdict = classify_qty(
            [r["stockpool_qty"] for r in api_group],
            [r["stockpool_qty"] for r in scr_group],
        )
        if verdict != "mismatch":
            continue
        api_max = max((q for q in (r["stockpool_qty"] for r in api_group) if q is not None), default=0)
        scr_max = max((q for q in (r["stockpool_qty"] for r in scr_group) if q is not None), default=0)
        note = f"API max qty {api_max} vs scraper max {scr_max}"
        for r in scr_group:
            r2 = dict(r)
            r2["note"] = note
            mismatch_rows.append(r2)

    # Sheets.
    sheet1_rows = [r for r in merged if r.get("in_stock")]
    sheet1_rows.sort(
        key=lambda r: (r["input_mpn"], -(r["stockpool_qty"] or 0), r["source"])
    )

    sheet2_rows = list(merged)
    sheet2_rows.sort(
        key=lambda r: (r["input_mpn"], r["source"],
                       0 if r.get("track") == "api" else 1,
                       int((r.get("warehouse_idx") or "0") or 0))
    )

    sheet3_rows = mismatch_rows
    sheet3_rows.sort(key=lambda r: (r["input_mpn"], r["source"]))

    # Write xlsx.
    xlsx_path = out_dir / "merged_procurement.xlsx"
    wb = openpyxl.Workbook()
    # Sheet 1
    ws1 = wb.active
    ws1.title = "现货优先"
    _style_workbook(ws1, OUTPUT_COLUMNS, sheet1_rows)
    _apply_row_fills(ws1, OUTPUT_COLUMNS, sheet1_rows)
    # Sheet 2
    ws2 = wb.create_sheet("全量数据")
    _style_workbook(ws2, OUTPUT_COLUMNS, sheet2_rows)
    _apply_row_fills(ws2, OUTPUT_COLUMNS, sheet2_rows)
    # Sheet 3
    ws3 = wb.create_sheet("scraper参考_库存不一致")
    _style_workbook(ws3, MISMATCH_COLUMNS, sheet3_rows)
    wb.save(xlsx_path)

    # Companion CSV (= Sheet 2).
    csv_path = out_dir / "merged_procurement.csv"
    write_csv(sheet2_rows, OUTPUT_COLUMNS, csv_path)

    # Stdout summary.
    n_api_ok = len(api_rows)
    n_scr_ok_total = len(scr_rows)
    n_scr_kept = len(scr_kept)
    n_scr_suppressed = len(scr_suppressed)
    chips_in_stock = len({r["input_mpn"] for r in sheet1_rows})
    print(f"API rows (status=ok, !HQEW)     : {n_api_ok}")
    print(f"Scraper rows (status=ok, !HQEW) : {n_scr_ok_total}")
    print(f"  - kept (API has no coverage)  : {n_scr_kept}")
    print(f"  - suppressed (API took over)  : {n_scr_suppressed}")
    print(f"Merged total (Sheet 2)          : {len(sheet2_rows)}")
    print(f"In-stock rows (Sheet 1)         : {len(sheet1_rows)} ({chips_in_stock} distinct MPNs)")
    print(f"Cross-val mismatches (Sheet 3)  : {len(sheet3_rows)}")
    print(f"\nWritten:")
    print(f"  {xlsx_path}")
    print(f"  {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
