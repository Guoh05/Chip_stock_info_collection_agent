"""Re-emit per-cell JSON / summary / batch_index.csv / batch_summary.md from
the saved `*_product.html` files in a bom2buy BatchTest folder, applying the
current scrape_bom2buy.py extraction logic. No Opera, no network — just
re-parses on-disk HTMLs.

Useful after tweaking `_extract_variants` / `_canonical_from_variant` so we
don't burn captcha sessions to refresh the canonical output.

Usage:
    .venv/Scripts/python.exe scraper/scripts/_reemit_bom2buy_batch.py <batch_dir>
"""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(PROJECT_ROOT / "common"))

import scrape_bom2buy as B
from _summary import write_summary


def reemit_cell(cell_dir: Path) -> dict | None:
    """Re-parse the saved HTML in a per-cell folder and overwrite outputs."""
    html_files = sorted(cell_dir.glob("*_product.html"))
    if not html_files:
        # no_results cells from the previous run won't have a _product.html;
        # leave them untouched but emit a minimal summary row
        return None
    html_path = html_files[0]
    safe_mpn = html_path.stem.removesuffix("_product")
    # Recover input_mpn from the existing JSON (if present) or from the safe_mpn
    json_files = sorted(cell_dir.glob("*.json"))
    existing = {}
    input_mpn = None
    expected_mfr = None
    if json_files:
        try:
            existing = json.load(open(json_files[0], encoding="utf-8"))
            input_mpn = existing.get("input_mpn")
            expected_mfr = existing.get("expected_mfr")
        except Exception:
            pass
    if not input_mpn:
        # Fall back: derive from folder name Test_<safe>_BOM2BUY
        folder = cell_dir.name.removeprefix("Test_").removesuffix("_BOM2BUY")
        input_mpn = folder
    html = html_path.read_text(encoding="utf-8")
    variants, page_meta = B._extract_variants(html, input_mpn)

    # Build a record matching scrape_one's output shape
    record = {
        "method": "playwright_opera",
        "url_search": existing.get("url_search") or f"https://www.bom2buy.com/search?part={input_mpn}&qty=1",
        "input_mpn": input_mpn,
        "expected_mfr": expected_mfr,
        "started_at": existing.get("started_at") or datetime.now().isoformat(timespec="seconds"),
        "channel": B.CHANNEL,
        "source": "bom2buy.com",
        "scraped_at_utc": existing.get("scraped_at_utc") or datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "paywall": "none",
        "attempts": [{"step": "reparse_saved_html", "outcome": "ok", "html_path": html_path.name}],
        "site_title": existing.get("site_title"),
        "page_meta": page_meta,
        "elapsed_sec": existing.get("elapsed_sec"),
    }

    if not variants:
        record.update(status="no_results", extracted=None, data_quality="none")
        # Overwrite outputs
        (cell_dir / f"{safe_mpn}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            write_summary(record, cell_dir, safe_mpn)
        except Exception as e:
            print(f"  [warn] write_summary failed for {input_mpn}: {e}")
        return {"input_mpn": input_mpn, "expected_mfr": expected_mfr, "status": "no_results",
                "returned_mpn": None, "returned_mfr": None,
                "stock_now_qty": None, "distributors": 0, "num_price_tiers": 0,
                "datasheet_url": None, "lifecycle_status": None, "min_order_qty": None,
                "elapsed_sec": record.get("elapsed_sec"), "error": ""}

    canonical_variants = [B._canonical_from_variant(v, input_mpn) for v in variants]
    chosen_idx = 0
    chosen = B._pick_variant(variants, input_mpn)
    if chosen is not None:
        for i, v in enumerate(variants):
            if v["variant_mpn"] == chosen["variant_mpn"]:
                chosen_idx = i
                break
    canonical_chosen = canonical_variants[chosen_idx]
    record["variants"] = canonical_variants
    record["extracted"] = canonical_chosen
    record["status"] = "ok"
    record["data_quality"] = "high" if canonical_chosen.get("stock_breakdown") else "medium"

    # Overwrite outputs
    (cell_dir / f"{safe_mpn}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        write_summary(record, cell_dir, safe_mpn)
    except Exception as e:
        print(f"  [warn] write_summary failed for {input_mpn}: {e}")

    # If multi-variant, regenerate sub-variant artifacts too
    if len(canonical_variants) > 1:
        for i, cv in enumerate(canonical_variants):
            safe_v = B._safe(cv["manufacturer_part_number"])
            sub = cell_dir / safe_v
            if not sub.exists():
                continue
            sub_record = {
                **record,
                "extracted": cv,
                "variant_index": i,
                "data_quality": "high" if cv["stock_breakdown"] else "medium",
            }
            sub_record.pop("variants", None)
            (sub / f"{safe_v}.json").write_text(
                json.dumps(sub_record, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                write_summary(sub_record, sub, safe_v)
            except Exception as e:
                print(f"  [warn] write_summary failed for variant {safe_v}: {e}")

    sb = canonical_chosen.get("stock_breakdown") or []
    pr = canonical_chosen.get("prices") or []
    return {
        "input_mpn": input_mpn,
        "expected_mfr": expected_mfr,
        "status": "ok",
        "returned_mpn": canonical_chosen.get("manufacturer_part_number"),
        "returned_mfr": canonical_chosen.get("manufacturer"),
        "stock_now_qty": canonical_chosen.get("stock_now_qty"),
        "distributors": len(sb),
        "num_price_tiers": len(pr),
        "datasheet_url": canonical_chosen.get("datasheet_url"),
        "lifecycle_status": canonical_chosen.get("lifecycle_status"),
        "min_order_qty": canonical_chosen.get("min_order_qty"),
        "elapsed_sec": record.get("elapsed_sec"),
        "error": "",
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: _reemit_bom2buy_batch.py <batch_dir>")
        sys.exit(2)
    batch_dir = Path(sys.argv[1])
    if not batch_dir.is_absolute():
        batch_dir = PROJECT_ROOT / batch_dir
    if not batch_dir.is_dir():
        print(f"Not a directory: {batch_dir}"); sys.exit(2)

    # Discover per-cell folders
    cells = sorted(d for d in batch_dir.iterdir() if d.is_dir() and d.name.startswith("Test_"))
    print(f"Re-emitting {len(cells)} cells in {batch_dir}")

    summary_rows = []
    # Also recover no_results cells that have only .json (no html) — keep them as-is
    for cd in cells:
        try:
            row = reemit_cell(cd)
        except Exception as e:
            print(f"  [err] {cd.name}: {e}")
            continue
        if row is None:
            # no-results cell with no html — try to load existing summary fields from JSON
            jfs = sorted(cd.glob("*.json"))
            if jfs:
                try:
                    rec = json.load(open(jfs[0], encoding="utf-8"))
                    row = {
                        "input_mpn": rec.get("input_mpn"),
                        "expected_mfr": rec.get("expected_mfr"),
                        "status": rec.get("status") or "no_results",
                        "returned_mpn": None, "returned_mfr": None,
                        "stock_now_qty": None, "distributors": 0, "num_price_tiers": 0,
                        "datasheet_url": None, "lifecycle_status": None, "min_order_qty": None,
                        "elapsed_sec": rec.get("elapsed_sec"),
                        "error": (rec.get("error") or "")[:200],
                    }
                except Exception:
                    continue
            else:
                continue
        ex_mpn = row.get('input_mpn') or '(unknown)'
        print(f"  {ex_mpn:25}  status={row.get('status'):12}  distributors={row.get('distributors')}  stock={row.get('stock_now_qty')}  prices={row.get('num_price_tiers')}")
        summary_rows.append(row)

    # Sort by input order — read batch_input.csv if available, else by folder name
    batch_input = batch_dir / "batch_input.csv"
    if batch_input.exists():
        with open(batch_input, encoding="utf-8-sig") as f:
            order = [r["input_mpn"] for r in csv.DictReader(f)]
        summary_rows.sort(key=lambda r: (order.index(r["input_mpn"]) if r["input_mpn"] in order else 999))

    # Overwrite batch_index.csv
    if summary_rows:
        with open(batch_dir / "batch_index.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            w.writerows(summary_rows)
        (batch_dir / "batch_summary.md").write_text(B._render_batch_summary(summary_rows, batch_dir), encoding="utf-8")
        print(f"Wrote batch_index.csv + batch_summary.md ({len(summary_rows)} rows)")


if __name__ == "__main__":
    main()
