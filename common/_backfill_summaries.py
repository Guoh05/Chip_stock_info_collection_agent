"""One-shot: regenerate <MPN>_summary.md inside every existing test/ run folder."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _summary import write_summary

TEST_ROOT = Path(__file__).resolve().parent.parent / "test"
SCAN_SUBDIRS = ("scraper", "api")


def _is_record_json(p: Path) -> bool:
    """Distinguish a normalized record file from auxiliary dumps."""
    skip_suffixes = (
        "_raw_nuxt.json", "_jsonld.json", "_next_data.json", "_xhr.json",
        "_attempt_summary.json", "_raw_next_data.json",
    )
    return not any(p.name.endswith(s) for s in skip_suffixes)


def _process_dir(d: Path) -> int:
    """Write a summary for the first record JSON in `d`; only if `extracted`
    is present (a parent aggregator record without `extracted` is skipped)."""
    json_files = [p for p in d.glob("*.json") if _is_record_json(p)]
    if not json_files:
        return 0
    rec_path = json_files[0]
    part = rec_path.stem
    try:
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[err] {rec_path.relative_to(TEST_ROOT.parent)}: {exc}")
        return 0
    # Skip aggregator records (LCSC v3 parent .json holds `variants` only)
    if "extracted" not in rec and rec.get("variants"):
        return 0
    path = write_summary(rec, d, part)
    print(f"[ok] wrote {path.relative_to(TEST_ROOT.parent)}")
    return 1


def main() -> int:
    n = 0
    # test/ is split into scraper/ and api/. Walk both.
    bases = [TEST_ROOT / s for s in SCAN_SUBDIRS if (TEST_ROOT / s).is_dir()]
    if not bases:
        # Backwards compat: fall through to old flat layout if neither subdir exists.
        bases = [TEST_ROOT]

    for base in bases:
        for run_dir in sorted(base.iterdir()):
            if not run_dir.is_dir() or not run_dir.name.startswith("Test_"):
                continue
            n += _process_dir(run_dir)
            # Walk one level deep for v3-style per-variant subfolders
            for sub in sorted(run_dir.iterdir()):
                if sub.is_dir() and not sub.name.startswith("_"):
                    n += _process_dir(sub)
    print(f"\ndone — {n} summaries written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
