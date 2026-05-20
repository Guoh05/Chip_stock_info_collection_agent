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
7. Output columns are renamed / reordered per
   ref/merged_output_fields_mapping.xlsx and enriched with project metadata
   from ref/Shortage Emergency Response List_v2.xlsx (sheet "Part List
   Modify"). Lead time converted from days to weeks (1 decimal).
8. Sheet 1 ("高风险有货") = procurement priority view: risk == "high" AND
   Available Quantity > 0.

Output: <env_root>/merged/Merge_<api_ts>__<scr_ts>/merged_procurement.xlsx
        + merged_procurement.csv (= Sheet 2 contents)

`<env_root>` is `test/` (default) or `production/` (with --env prod). The
same flag also picks where to read the API + Scraper batches from
(`<env_root>/api/` and `<env_root>/scraper/`).
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
ENV_ROOTS = {
    "test": PROJECT_ROOT / "test",
    "prod": PROJECT_ROOT / "production",
}
CHIP_LIST_PATH = PROJECT_ROOT / "ref" / "Shortage Emergency Response List_v2.xlsx"
CHIP_LIST_SHEET = "Part List Modify"

DROP_SOURCE_PREFIXES = ("HQEW_",)

# Chip-list headers we copy into the merged output (first-row per MPN).
CHIP_FIELDS = [
    "Category", "Project", "EMS/Finish Goods", "12NC_PCBA",
    "Quantity", "Currency", "Current Price", "Type", "risk",
]

# Per-column source:
#   ("merge", <old_csv_key>)        → copy from upstream CSV row (renamed)
#   ("chip",  <chip_list_header>)   → look up in chip_meta[normalized_mpn]
#   ("lead_time", <old_csv_key>)    → days→weeks transform
#   ("blank", None)                 → empty (business to fill)
COLUMN_SOURCE_MAP: dict[str, tuple[str, str | None]] = {
    "Category":                              ("chip", "Category"),
    "Project":                               ("chip", "Project"),
    "EMS/Finish Goods":                      ("chip", "EMS/Finish Goods"),
    "12NC_PCBA":                             ("chip", "12NC_PCBA"),
    "Manufacture Part Number":               ("merge", "input_mpn"),
    "Manufacture":                           ("merge", "expected_mfr"),
    "Quantity":                              ("chip", "Quantity"),
    "Currency":                              ("chip", "Currency"),
    "Current Price":                         ("chip", "Current Price"),
    "Type":                                  ("chip", "Type"),
    "risk":                                  ("chip", "risk"),
    "Broker name":                           ("merge", "source"),
    "Data collect method":                   ("merge", "track"),
    "in_stock":                              ("merge", "in_stock"),
    "Warehouse/vender":                      ("merge", "warehouse"),
    "Stock Location":                        ("merge", "ships_from"),
    "Available Quantity":                    ("merge", "stockpool_qty"),
    "ship infor after order placed":         ("merge", "ship_text"),
    "Lead Time (Week)":                      ("lead_time", "lead_time_days"),
    "MOQ":                                   ("merge", "moq"),
    "Minimum order qty":                     ("merge", "min_break_qty"),
    "Unit price (min qty)":                  ("merge", "price_at_min_qty"),
    "Maximum order qty":                     ("merge", "max_break_qty"),
    "Unit price (max qty)":                  ("merge", "price_at_max_qty"),
    "Number of price tiers":                 ("merge", "num_price_tiers"),
    "Trade \nCurrency":                      ("merge", "currency"),
    "Date of Code":                          ("blank", None),
    "Reel/Cut Reel":                         ("blank", None),
    "Certificate of Conformity(Yes/No)":     ("blank", None),
    "ref_Warehouse/vender ID":               ("merge", "warehouse_idx"),
    "ref_returned_mpn":                      ("merge", "returned_mpn"),
    "ref_vendor_sku":                        ("merge", "vendor_sku"),
    "ref_returned_mfr":                      ("merge", "returned_mfr"),
    "ref_mfr_match":                         ("merge", "mfr_match"),
    "ref_is_mirror":                         ("merge", "is_mirror"),
    "ref_datasheet_url":                     ("merge", "datasheet_url"),
    "ref_status":                            ("merge", "status"),
    "ref_error":                             ("merge", "error"),
}
OUTPUT_COLUMNS = list(COLUMN_SOURCE_MAP.keys())   # 38 columns, in output order

# Sheet 3 (cross-validation reference) — narrower subset, same renames, plus
# the unmapped `note` column (not in COLUMN_SOURCE_MAP — handled in builder).
MISMATCH_COLUMNS = [
    "Category", "Project", "EMS/Finish Goods", "12NC_PCBA",
    "Manufacture Part Number", "Manufacture",
    "Quantity", "Currency", "Current Price", "Type", "risk",
    "Broker name", "in_stock",
    "ref_returned_mpn", "Warehouse/vender", "Available Quantity",
    "ship infor after order placed", "Lead Time (Week)",
    "MOQ", "Unit price (min qty)", "Trade \nCurrency",
    "ref_datasheet_url", "note",
]

LEAD_TIME_HEADER = "Lead Time (Week)"
QTY_HEADER = "Available Quantity"

# Per-column widths from ref/merged_header_example (sheet `header`) — matched
# to procurement's preferred layout. Columns not listed fall back to auto-fit.
COLUMN_WIDTHS: dict[str, float] = {
    "Category": 12.0, "Project": 11.0, "EMS/Finish Goods": 18.0, "12NC_PCBA": 27.0,
    "Manufacture Part Number": 25.0, "Manufacture": 13.0,
    "Quantity": 10.0, "Currency": 13.0, "Current Price": 15.0, "Type": 10.0, "risk": 13.0,
    "Broker name": 33.0, "Data collect method": 21.0, "in_stock": 10.0,
    "Warehouse/vender": 58.9, "Stock Location": 26.0, "Available Quantity": 29.9,
    "ship infor after order placed": 45.0, "Lead Time (Week)": 18.0,
    "MOQ": 10.0, "Minimum order qty": 19.0, "Unit price (min qty)": 22.0,
    "Maximum order qty": 19.0, "Unit price (max qty)": 22.0, "Number of price tiers": 23.0,
    "Trade \nCurrency": 10.0,
    "Date of Code": 14.0, "Reel/Cut Reel": 15.0, "Certificate of Conformity(Yes/No)": 35.0,
    "ref_Warehouse/vender ID": 25.0, "ref_returned_mpn": 18.0, "ref_vendor_sku": 19.0,
    "ref_returned_mfr": 31.0, "ref_mfr_match": 15.0, "ref_is_mirror": 13.0,
    "ref_datasheet_url": 50.0, "ref_status": 12.0, "ref_error": 11.0,
    "note": 40.0,
}

# Sort priority for `risk` column. Higher priority = lower rank number.
RISK_RANK = {"high": 0, "low": 1}

# Per-row highlight fills (Sheet 2 only).
GREEN = PatternFill(start_color="FFC6EFCE", end_color="FFC6EFCE", fill_type="solid")
GREY = PatternFill(start_color="FFEEEEEE", end_color="FFEEEEEE", fill_type="solid")

# Three-zone header palette:
#   A–K   (cols  1–11): chip-list metadata + MPN/Manufacture     → dark blue + white text
#   L–AC  (cols 12–29): distributor data + 3 business-fill cols  → light orange + black text
#   AD+   (cols 30+ ):  ref_* technical / audit fields           → dark grey + white text
HEADER_FILL_BLUE = PatternFill(start_color="FF1F4E78", end_color="FF1F4E78", fill_type="solid")
HEADER_FILL_ORANGE = PatternFill(start_color="FFFCE4D6", end_color="FFFCE4D6", fill_type="solid")
HEADER_FILL_GREY = PatternFill(start_color="FF595959", end_color="FF595959", fill_type="solid")
HEADER_FONT_WHITE = Font(bold=True, color="FFFFFFFF")
HEADER_FONT_BLACK = Font(bold=True, color="FF000000")


def _header_style(col_idx: int) -> tuple[PatternFill, Font]:
    """Return (fill, font) for the header cell at the given 1-based column index."""
    if col_idx <= 11:
        return HEADER_FILL_BLUE, HEADER_FONT_WHITE
    if col_idx <= 29:
        return HEADER_FILL_ORANGE, HEADER_FONT_BLACK
    return HEADER_FILL_GREY, HEADER_FONT_WHITE


# ---------- helpers ---------------------------------------------------------

_BATCH_NAME_RE = re.compile(r"^BatchTest_\d{8}_\d{2}_\d{2}_\d{2}$")


def latest_batch(parent: Path) -> Path:
    """Newest folder matching the standard BatchTest_<YYYYMMDD>_<HH>_<MM>_<SS>/
    naming. Ad-hoc probe folders with extra suffixes (e.g.
    `BatchTest_..._bom2buy`) are skipped — their CSV schemas can differ."""
    candidates = sorted(
        (p for p in parent.iterdir() if p.is_dir() and _BATCH_NAME_RE.match(p.name)),
        key=lambda p: p.name,
    )
    if not candidates:
        raise SystemExit(f"No standard BatchTest_<ts>/ folder in {parent}")
    return candidates[-1]


def normalize_mpn(s) -> str:
    if s is None:
        return ""
    return str(s).strip().replace("\xa0", "")


def parse_qty(s):
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _normalize_alnum(s) -> str:
    """Strip everything except letters and digits, upper-case the result.
    Used to compare MPNs across punctuation noise: 'BAV99,215' == 'BAV99-215'."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def returned_mpn_matches_input(raw_row: dict) -> bool:
    """True iff input_mpn and returned_mpn agree after stripping punctuation.

    Drops rows where the channel returned a different MPN string than the one
    we searched for — e.g. searched `GD32E230K4T6`, channel returned
    `GD32E230K6T6`. Empty returned_mpn → False (always dropped).
    """
    inp = _normalize_alnum(raw_row.get("input_mpn"))
    ret = _normalize_alnum(raw_row.get("returned_mpn"))
    return bool(inp) and inp == ret


def days_to_weeks(s):
    """Convert a days-as-string value to weeks (1 decimal). None on missing."""
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return round(int(s) / 7, 1)
    except ValueError:
        try:
            return round(float(s) / 7, 1)
        except ValueError:
            return None


def load_chip_meta(path: Path) -> dict[str, dict]:
    """Build {normalized_mpn: {chip-field: value, ...}} using first-row per MPN.

    Reads sheet `Part List Modify`. Required columns: `Manufacture Part Number`
    plus everything in CHIP_FIELDS. Duplicates after the first are ignored.
    """
    if not path.exists():
        raise SystemExit(f"chip list not found: {path}")
    wb = openpyxl.load_workbook(path, data_only=True)
    if CHIP_LIST_SHEET not in wb.sheetnames:
        raise SystemExit(f"chip list missing sheet '{CHIP_LIST_SHEET}' in {path}")
    ws = wb[CHIP_LIST_SHEET]
    header_to_col: dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(1, c).value
        if v is None:
            continue
        header_to_col[str(v).strip()] = c
    needed = ["Manufacture Part Number", *CHIP_FIELDS]
    missing = [h for h in needed if h not in header_to_col]
    if missing:
        raise SystemExit(f"chip list missing required columns: {missing}")
    mpn_col = header_to_col["Manufacture Part Number"]
    meta: dict[str, dict] = {}
    for r in range(2, ws.max_row + 1):
        raw_mpn = ws.cell(r, mpn_col).value
        mpn = normalize_mpn(raw_mpn)
        if not mpn or mpn in meta:
            continue
        meta[mpn] = {h: ws.cell(r, header_to_col[h]).value for h in CHIP_FIELDS}
    return meta


def read_ok_rows(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "ok":
                continue
            src = r.get("source") or ""
            if any(src.startswith(p) for p in DROP_SOURCE_PREFIXES):
                continue
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


def build_output_row(raw: dict, chip_meta: dict, columns: list[str]) -> dict:
    """Produce an output dict whose keys are the new column headers."""
    mpn = normalize_mpn(raw.get("input_mpn"))
    meta = chip_meta.get(mpn, {})
    out: dict = {}
    for col in columns:
        # Sheet 3 carries `note`, which is not in COLUMN_SOURCE_MAP.
        if col == "note":
            out[col] = raw.get("note")
            continue
        kind, key = COLUMN_SOURCE_MAP.get(col, (None, None))
        if kind == "merge":
            out[col] = raw.get(key)
        elif kind == "chip":
            out[col] = meta.get(key)
        elif kind == "lead_time":
            out[col] = days_to_weeks(raw.get(key))
        else:  # blank or unknown
            out[col] = None
    return out


def is_high_risk_in_stock(out_row: dict) -> bool:
    risk = out_row.get("risk")
    if not isinstance(risk, str):
        if risk is None:
            return False
        risk = str(risk)
    if risk.strip().lower() != "high":
        return False
    qty = out_row.get(QTY_HEADER)
    return isinstance(qty, int) and qty > 0


def _sort_key(out_row: dict) -> tuple:
    """Procurement sort: risk (high first), MPN, Broker, then qty desc."""
    risk = out_row.get("risk")
    risk_str = risk.strip().lower() if isinstance(risk, str) else ""
    rank = RISK_RANK.get(risk_str, 2 if risk_str else 3)
    mpn = out_row.get("Manufacture Part Number") or ""
    broker = out_row.get("Broker name") or ""
    qty = out_row.get(QTY_HEADER)
    qty_neg = -(qty if isinstance(qty, int) else 0)
    return (rank, str(mpn), str(broker), qty_neg)


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
    header_align = Alignment(vertical="center", wrap_text=True)
    for col_idx, col in enumerate(columns, 1):
        c = ws.cell(row=1, column=col_idx, value=col)
        fill, font = _header_style(col_idx)
        c.fill = fill
        c.font = font
        c.alignment = header_align
    for row_idx, r in enumerate(rows, 2):
        for col_idx, col in enumerate(columns, 1):
            ws.cell(row=row_idx, column=col_idx, value=_cell_value(r.get(col)))
    ws.freeze_panes = "A2"
    for col_idx, col in enumerate(columns, 1):
        if col in COLUMN_WIDTHS:
            ws.column_dimensions[get_column_letter(col_idx)].width = COLUMN_WIDTHS[col]
            continue
        header_widest = max((len(line) for line in str(col).split("\n")), default=0)
        sample = [str(r.get(col)) for r in rows if r.get(col) is not None][:300]
        sample_widest = max((len(s) for s in sample), default=0)
        width = max(header_widest, sample_widest) + 2
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(width, 10), 50)
    # AutoFilter dropdowns across header + data range.
    last_col = get_column_letter(len(columns))
    last_row = max(2, 1 + len(rows))
    ws.auto_filter.ref = f"A1:{last_col}{last_row}"


def _apply_row_fills(ws, columns: list[str], rows: list[dict]) -> None:
    ncols = len(columns)
    for row_idx, r in enumerate(rows, 2):
        if r.get("in_stock"):
            fill = GREEN
        elif r.get(QTY_HEADER) == 0:
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
    ap.add_argument("--env", choices=("test", "prod"), default="test",
                    help="Environment root: 'test' → test/{api,scraper,merged}/ (default), "
                         "'prod' → production/{api,scraper,merged}/.")
    ap.add_argument("--api", type=Path, default=None,
                    help="API BatchTest folder (default: newest under <env_root>/api/)")
    ap.add_argument("--scr", type=Path, default=None,
                    help="Scraper BatchTest folder (default: newest under <env_root>/scraper/)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir (default: <env_root>/merged/Merge_<api_ts>__<scr_ts>/)")
    ap.add_argument("--chip-list", type=Path, default=CHIP_LIST_PATH,
                    help=f"Chip metadata xlsx (default: {CHIP_LIST_PATH})")
    args = ap.parse_args()

    env_root = ENV_ROOTS[args.env]
    api_root = env_root / "api"
    scr_root = env_root / "scraper"
    merged_root = env_root / "merged"

    api_dir = args.api or latest_batch(api_root)
    scr_dir = args.scr or latest_batch(scr_root)
    api_csv = api_dir / "batch_index.csv"
    scr_csv = scr_dir / "batch_index.csv"
    if not api_csv.exists():
        raise SystemExit(f"missing {api_csv}")
    if not scr_csv.exists():
        raise SystemExit(f"missing {scr_csv}")

    chip_meta = load_chip_meta(args.chip_list)

    api_ts = re.sub(r"^BatchTest_", "", api_dir.name)
    scr_ts = re.sub(r"^BatchTest_", "", scr_dir.name)
    out_dir = args.out or (merged_root / f"Merge_{api_ts}__{scr_ts}")
    out_dir.mkdir(parents=True, exist_ok=True)

    api_rows_raw = read_ok_rows(api_csv)
    scr_rows_raw = read_ok_rows(scr_csv)

    # MPN-match filter: drop rows where the channel returned a different MPN
    # than what we searched for (punctuation-insensitive comparison).
    api_rows = [r for r in api_rows_raw if returned_mpn_matches_input(r)]
    scr_rows = [r for r in scr_rows_raw if returned_mpn_matches_input(r)]
    n_api_dropped_mpn = len(api_rows_raw) - len(api_rows)
    n_scr_dropped_mpn = len(scr_rows_raw) - len(scr_rows)

    annotate(api_rows, "api")
    annotate(scr_rows, "scraper")

    # Primary merge — API wins per (mpn, source).
    api_keys = {(r["input_mpn"], r["source"]) for r in api_rows}
    scr_kept = [r for r in scr_rows if (r["input_mpn"], r["source"]) not in api_keys]
    scr_suppressed = [r for r in scr_rows if (r["input_mpn"], r["source"]) in api_keys]
    merged = list(api_rows) + scr_kept

    # Cross-validation — over (mpn, source) where both tracks had data.
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

    # Project RAW rows → OUTPUT rows (new column names, lead-time converted,
    # chip-list enrichment applied).
    sheet2_rows = [build_output_row(r, chip_meta, OUTPUT_COLUMNS) for r in merged]
    sheet3_rows = [build_output_row(r, chip_meta, MISMATCH_COLUMNS) for r in mismatch_rows]

    # Unified sort across all three sheets: risk → MPN → Broker → qty desc.
    sheet2_rows.sort(key=_sort_key)
    sheet3_rows.sort(key=_sort_key)

    # Sheet 1 is a strict subset of sheet 2: risk=high AND qty>0.
    sheet1_rows = [r for r in sheet2_rows if is_high_risk_in_stock(r)]
    sheet1_rows.sort(key=_sort_key)

    # Write xlsx.
    xlsx_path = out_dir / "merged_procurement.xlsx"
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "高风险有货"
    _style_workbook(ws1, OUTPUT_COLUMNS, sheet1_rows)
    # Sheet 1 is "all in-stock by definition" → no per-row green fill (would be
    # uniformly green and uninformative).

    ws2 = wb.create_sheet("全量数据")
    _style_workbook(ws2, OUTPUT_COLUMNS, sheet2_rows)
    _apply_row_fills(ws2, OUTPUT_COLUMNS, sheet2_rows)

    ws3 = wb.create_sheet("scraper参考_库存不一致")
    _style_workbook(ws3, MISMATCH_COLUMNS, sheet3_rows)
    wb.save(xlsx_path)

    # Companion CSV (= sheet 2).
    csv_path = out_dir / "merged_procurement.csv"
    write_csv(sheet2_rows, OUTPUT_COLUMNS, csv_path)

    # Run summary.
    n_api_ok = len(api_rows)
    n_scr_ok_total = len(scr_rows)
    n_scr_kept = len(scr_kept)
    n_scr_suppressed = len(scr_suppressed)
    chips_high_risk = len({r.get("Manufacture Part Number") for r in sheet1_rows})
    chips_total_merged = len({r.get("Manufacture Part Number") for r in sheet2_rows})
    chips_matched = sum(1 for m in {r.get("Manufacture Part Number") for r in sheet2_rows}
                        if m and normalize_mpn(m) in chip_meta)
    print(f"Chip list (Part List Modify)        : {len(chip_meta)} unique MPNs")
    print(f"API rows (status=ok, !HQEW)         : {n_api_ok}  (dropped {n_api_dropped_mpn} for returned_mpn mismatch)")
    print(f"Scraper rows (status=ok, !HQEW)    : {n_scr_ok_total}  (dropped {n_scr_dropped_mpn} for returned_mpn mismatch)")
    print(f"  - kept (API has no coverage)     : {n_scr_kept}")
    print(f"  - suppressed (API took over)     : {n_scr_suppressed}")
    print(f"Merged total (Sheet 2 '全量数据')     : {len(sheet2_rows)} ({chips_total_merged} MPNs)")
    print(f"  - matched against chip list      : {chips_matched}")
    print(f"  - no chip-list row               : {chips_total_merged - chips_matched}")
    print(f"Sheet 1 '高风险有货' (risk=high + qty>0) : {len(sheet1_rows)} rows, {chips_high_risk} MPNs")
    print(f"Sheet 3 '库存不一致'                  : {len(sheet3_rows)} rows")
    print()
    print(f"Written:")
    print(f"  {xlsx_path}")
    print(f"  {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
