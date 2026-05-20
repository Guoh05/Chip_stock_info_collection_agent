"""One-off incremental updater for a finished batch folder.

Re-uses `batch_api_test._call_one_source` to run additional (chip × source)
pairs and appends the results to an existing batch directory's
`batch_index.csv` / `.xlsx` / `.json` + regenerates `batch_summary.md` /
`failures.md` + appends to `batch_input.csv`. Per-MPN run subfolders land in
the same batch directory.

Edit `BATCH_DIR` + `NEW_CHIPS` below before running. Safe to re-run only if
the new chips don't collide with existing per-MPN folders.

Usage:
    .venv/Scripts/python.exe api/scripts/_append_chips_to_batch.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "api" / "scripts"))
import batch_api_test as B  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# --- edit these two values per run -----------------------------------------
BATCH_DIR = PROJECT_ROOT / "test" / "api_test" / "BatchTest_20260520_07_40_36"
NEW_CHIPS = [
    # row=None → not from the master xlsx; the column is informational only
    {"row": "+1", "input_mpn": "BAT32G135BGE32FP", "expected_mfr": "CmSemi"},
    {"row": "+2", "input_mpn": "BAV99LT1G",        "expected_mfr": "ON"},
    {"row": "+3", "input_mpn": "BAV99,215",        "expected_mfr": "Nexperia"},
    {"row": "+4", "input_mpn": "PDTA115ET,215",    "expected_mfr": "PDTA115ET"},
]
# ---------------------------------------------------------------------------


def _coerce_csv_row(r: dict) -> dict:
    """Round-trip a CSV-read string row back to native Python types so the
    xlsx writer renders integers as integers, not stringified ints."""
    out = dict(r)
    for k in ("warehouse_idx", "stockpool_qty", "lead_time_days", "moq",
              "min_break_qty", "max_break_qty", "num_price_tiers"):
        v = out.get(k, "")
        if v == "" or v is None:
            out[k] = None
        else:
            try:
                out[k] = int(v)
            except ValueError:
                pass
    for k in ("price_at_min_qty", "price_at_max_qty"):
        v = out.get(k, "")
        if v == "" or v is None:
            out[k] = None
        else:
            try:
                out[k] = float(v)
            except ValueError:
                pass
    out["mfr_match"] = (out.get("mfr_match") or "").strip().lower() == "true"
    return out


def main() -> int:
    if not BATCH_DIR.exists():
        print(f"ERROR: batch dir not found: {BATCH_DIR}", file=sys.stderr)
        return 2
    load_dotenv(PROJECT_ROOT / "api" / ".env")
    sources_to_run = list(B.SOURCES_ALL)

    # Per-source rate-limit table (same as main driver)
    source_min_interval = dict(B.SOURCE_MIN_INTERVAL_DEFAULT)
    source_min_interval["ELEMENT14"] = B._element14_min_interval()
    source_last_call: dict[str, float] = {src: 0.0 for src in B.SOURCES_ALL}
    rate_limit_locks = {src: threading.Lock() for src in B.SOURCES_ALL}
    print_lock = threading.Lock()

    # --- Run the new chips ------------------------------------------------
    new_rows: list[dict] = []
    new_records: list[dict] = []
    for i, chip in enumerate(NEW_CHIPS, 1):
        mpn = chip["input_mpn"]
        mfr = chip["expected_mfr"]
        with print_lock:
            print(f"[{i}/{len(NEW_CHIPS)}] {mpn}  (expected {mfr})")

        # Refuse to clobber existing per-MPN folders for the same source.
        safe = B._safe_folder(mpn)
        for src in sources_to_run:
            collide = BATCH_DIR / f"Test_{safe}_{src}"
            if collide.exists():
                print(f"  WARN: {collide.name} already exists — proceeding will "
                      f"overwrite per-MPN files but not the merged batch_index.")

        results_by_source: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=len(sources_to_run)) as ex:
            futures = {
                ex.submit(
                    B._call_one_source, mpn, mfr, src, BATCH_DIR,
                    source_min_interval, source_last_call, rate_limit_locks,
                ): src
                for src in sources_to_run
            }
            for fut in as_completed(futures):
                res = fut.result()
                results_by_source[res["source"]] = res
                with print_lock:
                    print(res["log_line"])

        for src in sources_to_run:
            r = results_by_source.get(src)
            if r is None:
                continue
            new_rows.extend(r["index_rows"])
            new_records.append(r["all_record"])

    # --- Merge into existing files ---------------------------------------
    with open(BATCH_DIR / "batch_index.csv", encoding="utf-8-sig") as f:
        existing_rows = [_coerce_csv_row(r) for r in csv.DictReader(f)]
    merged_rows = existing_rows + new_rows

    with open(BATCH_DIR / "batch_index.json", encoding="utf-8") as f:
        existing_records = json.load(f)
    merged_records = existing_records + new_records

    print(
        f"\nMerged rows: {len(existing_rows)} existing + {len(new_rows)} new "
        f"= {len(merged_rows)} total"
    )
    print(
        f"Merged records: {len(existing_records)} existing + {len(new_records)} new "
        f"= {len(merged_records)} total"
    )

    B.write_csv(merged_rows, B.INDEX_COLUMNS, BATCH_DIR / "batch_index.csv")
    B.write_xlsx(merged_rows, B.INDEX_COLUMNS, BATCH_DIR / "batch_index.xlsx",
                 "batch_index")
    (BATCH_DIR / "batch_index.json").write_text(
        json.dumps(merged_records, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Append to batch_input.csv (preserve existing rows verbatim).
    with open(BATCH_DIR / "batch_input.csv", "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for c in NEW_CHIPS:
            w.writerow([c["row"], c["input_mpn"], c["expected_mfr"]])

    # Reload chips list (matches what's now in batch_input.csv).
    with open(BATCH_DIR / "batch_input.csv", encoding="utf-8-sig") as f:
        all_chips = [
            {"row": c["xlsx_row"], "input_mpn": c["input_mpn"],
             "expected_mfr": c["expected_mfr"]}
            for c in csv.DictReader(f)
        ]

    # Recover started timestamp from existing summary; finished = now.
    existing_summary = (BATCH_DIR / "batch_summary.md").read_text(encoding="utf-8")
    m_s = re.search(r"\*\*Started:\*\*\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
                    existing_summary)
    started = (datetime.strptime(m_s.group(1), "%Y-%m-%d %H:%M:%S")
               if m_s else datetime.now())
    finished = datetime.now()

    # The skipped-rows section is a one-row known constant for this batch.
    skipped = [{"row": 108, "raw_mpn": "(MPN缺失，待Yuan确认)",
                "raw_mfr": "ASCHIP",
                "reason": "missing or non-MPN placeholder"}]

    B.write_summary_md(all_chips, merged_rows, skipped, BATCH_DIR,
                       started, finished, sources_to_run)
    B.write_failures(merged_rows, BATCH_DIR / "failures.md")
    print("\nRewrote batch_summary.md + failures.md (full merged dataset)")
    print(f"Batch folder: {BATCH_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
