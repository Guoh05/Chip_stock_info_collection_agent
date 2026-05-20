"""One-off ad-hoc fetch — 3 GD32E230 MCUs through all 5 distributor APIs.

Output lands in `test/api/temp_GD32E230_<ts>/` (the `temp_` prefix keeps
it visibly separate from the canonical `BatchTest_<ts>/` folders, and signals
the data is not part of the master xlsx).

Re-uses `batch_api_test._call_one_source` + writers. Safe to delete after the
user has reviewed the result; not referenced from anywhere else.

Usage:
    .venv/Scripts/python.exe api/scripts/_adhoc_fetch_gd32e230.py
"""

from __future__ import annotations

import csv
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "api" / "scripts"))
import batch_api_test as B  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# Ad-hoc input — not in the master xlsx.
CHIPS = [
    {"row": "adhoc-1", "input_mpn": "GD32E230K4T6", "expected_mfr": "GigaDevice"},
    {"row": "adhoc-2", "input_mpn": "GD32E230K6T6", "expected_mfr": "GigaDevice"},
    {"row": "adhoc-3", "input_mpn": "GD32E230K8T6", "expected_mfr": "GigaDevice"},
]


def main() -> int:
    load_dotenv(PROJECT_ROOT / "api" / ".env")
    sources_to_run = list(B.SOURCES_ALL)

    ts = datetime.now().strftime("%Y%m%d_%H_%M_%S")
    out_dir = PROJECT_ROOT / "test" / "api" / f"temp_GD32E230_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output folder: {out_dir}")

    source_min_interval = dict(B.SOURCE_MIN_INTERVAL_DEFAULT)
    source_min_interval["ELEMENT14"] = B._element14_min_interval()
    source_last_call: dict[str, float] = {src: 0.0 for src in B.SOURCES_ALL}
    rate_limit_locks = {src: threading.Lock() for src in B.SOURCES_ALL}
    print_lock = threading.Lock()

    started = datetime.now(timezone.utc)
    index_rows: list[dict] = []
    all_records: list[dict] = []

    for i, chip in enumerate(CHIPS, 1):
        mpn = chip["input_mpn"]
        mfr = chip["expected_mfr"]
        with print_lock:
            print(f"[{i}/{len(CHIPS)}] {mpn}  (expected {mfr})")
        results_by_source: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=len(sources_to_run)) as ex:
            futures = {
                ex.submit(
                    B._call_one_source, mpn, mfr, src, out_dir,
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
            index_rows.extend(r["index_rows"])
            all_records.append(r["all_record"])

    finished = datetime.now(timezone.utc)

    # Write batch_input.csv (verbatim) + the standard 4 batch outputs.
    with open(out_dir / "batch_input.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["xlsx_row", "input_mpn", "expected_mfr"])
        for c in CHIPS:
            w.writerow([c["row"], c["input_mpn"], c["expected_mfr"]])

    B.write_csv(index_rows, B.INDEX_COLUMNS, out_dir / "batch_index.csv")
    B.write_xlsx(index_rows, B.INDEX_COLUMNS, out_dir / "batch_index.xlsx", "batch_index")
    (out_dir / "batch_index.json").write_text(
        json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    B.write_failures(index_rows, out_dir / "failures.md")
    B.write_summary_md(
        CHIPS, index_rows, [], out_dir,
        started.astimezone(), finished.astimezone(), sources_to_run,
    )
    print(f"\nDone. {len(index_rows)} warehouse rows across {len(CHIPS)} chips × "
          f"{len(sources_to_run)} sources.")
    print(f"Wrote: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
