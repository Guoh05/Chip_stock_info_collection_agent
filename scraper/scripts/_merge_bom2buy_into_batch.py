"""Merge bom2buy per-cell results into an existing BatchTest folder.

Reads each `Test_<MPN>_BOM2BUY/<MPN>.json` cell folder, generates v3-schema
warehouse-exploded rows (one row per distinct distributor inside the cell's
canonical stock_breakdown[]), and appends them to the batch's existing
batch_index.csv / .xlsx / .json. Updates batch_summary.md to include the new
source.

Critical rule (per user 2026-05-20): each warehouse row's `min_break_qty /
max_break_qty / num_price_tiers / price_at_min_qty / price_at_max_qty` MUST
be derived from THAT distributor's own tier list (`stock_breakdown[i].prices`),
not from the cell-level top-level `extracted.prices[]`. bom2buy is the only
source where each warehouse has an independent tier structure.

Usage:
    .venv/Scripts/python.exe scraper/scripts/_merge_bom2buy_into_batch.py <batch_dir>
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SOURCE_LABEL = "BOM2BUY_买芯片网"

INDEX_COLUMNS = [
    "input_mpn", "expected_mfr", "source", "status",
    "returned_mpn", "vendor_sku", "returned_mfr", "mfr_match",
    "warehouse", "warehouse_idx", "ships_from",
    "stockpool_qty", "ship_text", "lead_time_days", "moq",
    "min_break_qty", "price_at_min_qty", "max_break_qty", "price_at_max_qty",
    "num_price_tiers", "currency", "datasheet_url",
    "run_subdir", "error",
    "elapsed_sec", "num_variants",
]


# ---- helpers ----

def _norm_mfr(s: str) -> str:
    """Normalise a mfr string for comparison: lowercase, strip aliases /
    Chinese annotations / common suffixes."""
    if not s:
        return ""
    s = s.lower()
    # Strip Chinese parenthetical aliases like "WeEn(瑞能)" -> "ween"
    s = re.sub(r"[（(][^)）]*[)）]", "", s)
    # Strip common corporate suffixes
    for suf in [" inc.", " inc", " corp.", " corp", " co.,?\\s*ltd\\.?",
                " ltd\\.?", " gmbh", " technology", " technologies",
                " semiconductor", " 瑞能", " 美国微芯", " 意法", " 微芯"]:
        s = re.sub(suf, "", s, flags=re.IGNORECASE)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def _mfr_match(expected: str, returned: str) -> str:
    """Return 'True' / 'False' / '' (empty if either side is missing)."""
    if not expected or not returned:
        return ""
    e = _norm_mfr(expected); r = _norm_mfr(returned)
    if not e or not r:
        return ""
    return str(e == r or e in r or r in e)


_LEAD_TIME_PATTERNS = [
    (re.compile(r"Factory Lead Time:\s*(\d+)\s*Weeks?", re.I), lambda n: int(n) * 7),
    (re.compile(r"Factory Lead Time:\s*(\d+)\s*Days?", re.I), int),
    (re.compile(r"原厂(?:标准)?交货期\s*(\d+)\s*周"), lambda n: int(n) * 7),
    (re.compile(r"原厂(?:标准)?交货期\s*(\d+)\s*[天日]"), int),
    (re.compile(r"lead\s*(\d+)\s*天", re.I), int),
    # bom2buy / oneyac form: "16W"
    (re.compile(r"(\d+)\s*W\b", re.I), lambda n: int(n) * 7),
    # bom2buy form: "中国大陆: 7-10个工作日" — take the upper bound (more conservative)
    (re.compile(r"(\d+)[\s-]*(\d+)\s*个工作日"), lambda n: int(n)),  # n is the SECOND capture; see below
    # bom2buy form: "中国大陆: 10个工作日"
    (re.compile(r"(\d+)\s*个工作日"), int),
]


def _parse_lead_time_days(ship_text: str | None) -> int | None:
    if not ship_text:
        return None
    # Specialised: "N-M个工作日" → use M
    if m := re.search(r"(\d+)\s*-\s*(\d+)\s*个工作日", ship_text):
        return int(m.group(2))
    if m := re.search(r"(\d+)\s*个工作日", ship_text):
        return int(m.group(1))
    for pat, fn in _LEAD_TIME_PATTERNS[:6]:  # original (non-bom2buy) patterns
        if m := pat.search(ship_text):
            return fn(m.group(1))
    return None


def _price_summary_for_row(prices: list[dict]) -> dict:
    """Compute min_break_qty / max_break_qty / price_at_min_qty / price_at_max_qty /
    num_price_tiers from a per-row tier list."""
    out = {
        "min_break_qty": None, "max_break_qty": None,
        "price_at_min_qty": None, "price_at_max_qty": None,
        "num_price_tiers": 0,
    }
    if not prices:
        return out
    valid = [p for p in prices if p.get("min_qty") is not None and p.get("unit_price") is not None]
    if not valid:
        return out
    valid_sorted = sorted(valid, key=lambda p: p["min_qty"])
    out["min_break_qty"] = valid_sorted[0]["min_qty"]
    out["price_at_min_qty"] = valid_sorted[0]["unit_price"]
    out["max_break_qty"] = valid_sorted[-1]["min_qty"]
    out["price_at_max_qty"] = valid_sorted[-1]["unit_price"]
    out["num_price_tiers"] = len(valid)
    return out


# ---- per-cell to v3 rows ----

def _make_bom2buy_rows(record: dict, batch_dir: Path) -> list[dict]:
    """Build v3-schema warehouse-exploded rows for one bom2buy cell."""
    input_mpn = record.get("input_mpn") or ""
    expected_mfr = record.get("expected_mfr") or ""
    status = record.get("status") or ""
    ex = record.get("extracted") or {}
    elapsed_sec = record.get("elapsed_sec")
    num_variants = len(record.get("variants") or []) or (1 if status == "ok" else 0)

    # Build run_subdir relative path to batch_dir
    safe_mpn = re.sub(r"[^A-Za-z0-9._-]", "_", input_mpn)
    cell_dir = batch_dir / f"Test_{safe_mpn}_BOM2BUY"
    run_subdir = str(cell_dir.relative_to(PROJECT_ROOT)).replace("\\", "/")

    returned_mpn = ex.get("manufacturer_part_number") or ""
    returned_mfr = ex.get("manufacturer") or ""
    mfr_match = _mfr_match(expected_mfr, returned_mfr)
    datasheet_url = ex.get("datasheet_url") or ""
    error = (record.get("error") or "")[:300]

    base = {
        "input_mpn": input_mpn,
        "expected_mfr": expected_mfr,
        "source": SOURCE_LABEL,
        "status": status,
        "returned_mpn": returned_mpn,
        "returned_mfr": returned_mfr,
        "mfr_match": mfr_match,
        "datasheet_url": datasheet_url,
        "run_subdir": run_subdir,
        "error": error,
        "elapsed_sec": elapsed_sec,
        "num_variants": num_variants,
        "currency": "CNY",
    }

    # Non-ok cells emit exactly one fallback row (matches LCSC/Future/etc. pattern)
    sb = ex.get("stock_breakdown") or []
    if status != "ok" or not sb:
        return [{
            **base,
            "vendor_sku": "",
            "warehouse": "", "warehouse_idx": "", "ships_from": "",
            "stockpool_qty": "", "ship_text": "", "lead_time_days": "", "moq": "",
            "min_break_qty": "", "price_at_min_qty": "",
            "max_break_qty": "", "price_at_max_qty": "", "num_price_tiers": 0,
        }]

    rows = []
    for i, d in enumerate(sb, start=1):
        per_row_prices = d.get("prices") or []
        ps = _price_summary_for_row(per_row_prices)
        rows.append({
            **base,
            "vendor_sku": d.get("vendor_sku") or "",
            "warehouse": d.get("warehouse") or "",
            "warehouse_idx": i,
            "ships_from": "",
            "stockpool_qty": d.get("quantity") if d.get("quantity") is not None else "",
            "ship_text": d.get("ship_text") or "",
            "lead_time_days": _parse_lead_time_days(d.get("ship_text")) or "",
            "moq": d.get("moq") or "",
            "min_break_qty": ps["min_break_qty"] if ps["min_break_qty"] is not None else "",
            "price_at_min_qty": ps["price_at_min_qty"] if ps["price_at_min_qty"] is not None else "",
            "max_break_qty": ps["max_break_qty"] if ps["max_break_qty"] is not None else "",
            "price_at_max_qty": ps["price_at_max_qty"] if ps["price_at_max_qty"] is not None else "",
            "num_price_tiers": ps["num_price_tiers"],
        })
    return rows


# ---- main merge ----

def main():
    if len(sys.argv) < 2:
        print("Usage: _merge_bom2buy_into_batch.py <batch_dir>"); sys.exit(2)
    batch_dir = Path(sys.argv[1])
    if not batch_dir.is_absolute():
        batch_dir = PROJECT_ROOT / batch_dir
    if not batch_dir.is_dir():
        print(f"Not a directory: {batch_dir}"); sys.exit(2)

    # Load existing batch_index.csv (824 rows from 8 sources)
    idx_csv = batch_dir / "batch_index.csv"
    existing_rows = list(csv.DictReader(open(idx_csv, encoding="utf-8-sig")))
    print(f"Loaded {len(existing_rows)} existing rows from batch_index.csv")
    existing_sources = sorted({r.get("source") for r in existing_rows})
    print(f"Sources present: {existing_sources}")

    # Drop any existing bom2buy rows (defensive — in case of partial re-run)
    existing_rows = [r for r in existing_rows if r.get("source") != SOURCE_LABEL]
    print(f"After dropping existing bom2buy rows: {len(existing_rows)}")

    # Collect bom2buy cells from disk
    cell_dirs = sorted(d for d in batch_dir.iterdir()
                       if d.is_dir() and d.name.startswith("Test_") and d.name.endswith("_BOM2BUY"))
    print(f"Found {len(cell_dirs)} bom2buy cell folders")

    # Build input_mpn → record map
    bom2buy_rows = []
    skipped = []
    for cd in cell_dirs:
        jfs = sorted(cd.glob("*.json"))
        # Pick the canonical per-cell JSON (not the variant subfolder ones)
        # Top-level JSON has the same MPN as the safe folder name
        chosen = None
        for jf in jfs:
            if jf.parent == cd:  # only top-level
                chosen = jf
                break
        if not chosen:
            skipped.append(cd.name)
            continue
        try:
            rec = json.load(open(chosen, encoding="utf-8"))
        except Exception as e:
            print(f"  [warn] {cd.name}: {e}")
            skipped.append(cd.name)
            continue
        bom2buy_rows.extend(_make_bom2buy_rows(rec, batch_dir))

    print(f"Emitted {len(bom2buy_rows)} bom2buy v3 rows (from {len(cell_dirs)} cells)")
    if skipped:
        print(f"Skipped: {skipped}")

    # Append + rewrite batch_index.csv
    all_rows = existing_rows + bom2buy_rows
    with open(idx_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
        w.writeheader()
        for r in all_rows:
            # Coerce all keys to strings to satisfy DictWriter (some columns may be missing)
            w.writerow({k: r.get(k, "") for k in INDEX_COLUMNS})
    print(f"Wrote {len(all_rows)} rows to batch_index.csv")

    # Regenerate batch_index.xlsx from the CSV
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "batch_index"
        ws.append(INDEX_COLUMNS)
        for r in all_rows:
            ws.append([r.get(k, "") for k in INDEX_COLUMNS])
        wb.save(batch_dir / "batch_index.xlsx")
        print("Wrote batch_index.xlsx")
    except Exception as e:
        print(f"  [warn] xlsx write failed: {e}")

    # Update batch_index.json — append per-cell bom2buy records to the records list
    idx_json = batch_dir / "batch_index.json"
    if idx_json.exists():
        try:
            existing_records = json.load(open(idx_json, encoding="utf-8"))
            if not isinstance(existing_records, list):
                existing_records = []
        except Exception:
            existing_records = []
    else:
        existing_records = []
    # Drop any bom2buy entries (defensive)
    def _is_bom2buy(rec):
        return (rec.get("channel") == "BOM2BUY" or
                rec.get("source") == "bom2buy.com" or
                (rec.get("subprocess", {}).get("argv", []) or [""])[0:1] == ["scrape_bom2buy.py"])
    existing_records = [r for r in existing_records if not _is_bom2buy(r)]
    new_records = []
    for cd in cell_dirs:
        jfs = [j for j in cd.glob("*.json") if j.parent == cd]
        if not jfs:
            continue
        try:
            rec = json.load(open(jfs[0], encoding="utf-8"))
            # The cell's record carries everything; tag it for filterability
            rec["channel"] = "BOM2BUY"
            new_records.append(rec)
        except Exception:
            continue
    existing_records.extend(new_records)
    idx_json.write_text(json.dumps(existing_records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Updated batch_index.json ({len(new_records)} bom2buy records appended)")

    # Regenerate batch_summary.md to add bom2buy stats
    summary_md = _render_batch_summary(all_rows, batch_dir)
    (batch_dir / "batch_summary.md").write_text(summary_md, encoding="utf-8")
    print("Updated batch_summary.md")


def _render_batch_summary(all_rows: list[dict], batch_dir: Path) -> str:
    """Render a v3 batch_summary.md based on the full deduped (input_mpn, source) cell list."""
    from collections import defaultdict, Counter

    # Dedupe to one row per (input_mpn, source) — warehouse rows share status
    cell_rows: dict[tuple[str, str], dict] = {}
    for r in all_rows:
        key = (r.get("input_mpn", ""), r.get("source", ""))
        cell_rows.setdefault(key, r)

    # Per-source pass-rate
    per_src: dict[str, dict] = defaultdict(lambda: {"ok": 0, "no_results": 0, "blocked": 0, "failed": 0, "total": 0})
    for (mpn, src), r in cell_rows.items():
        d = per_src[src]
        d["total"] += 1
        s = r.get("status", "")
        if s == "ok":
            d["ok"] += 1
        elif s == "no_results":
            d["no_results"] += 1
        elif s == "blocked":
            d["blocked"] += 1
        else:
            d["failed"] += 1

    # Cross-source coverage
    by_chip: dict[str, set[str]] = defaultdict(set)
    for (mpn, src), r in cell_rows.items():
        if r.get("status") == "ok":
            by_chip[mpn].add(src)
    all_chips = {mpn for (mpn, _) in cell_rows.keys()}
    coverage = Counter(len(by_chip.get(c, set())) for c in all_chips)

    lines = [
        f"# Batch summary — {batch_dir.name}",
        "",
        f"- Total cells: {len(cell_rows)} ({len(all_chips)} chips × {len(per_src)} sources)",
        "",
        "## Per-source pass rate",
        "",
        "| Source | OK | No results | Blocked | Failed | OK % |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for src in sorted(per_src.keys()):
        d = per_src[src]
        pct = (100.0 * d["ok"] / d["total"]) if d["total"] else 0.0
        lines.append(f"| {src} | {d['ok']} | {d['no_results']} | {d['blocked']} | {d['failed']} | {pct:.1f} % |")
    lines += ["", "## Cross-source coverage (chips by # of sources that returned ok)", ""]
    lines.append("| # sources ok | chips |")
    lines.append("|---:|---:|")
    for n in sorted(coverage.keys(), reverse=True):
        lines.append(f"| {n} | {coverage[n]} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
