"""Parse pipeline.log tail to extract current MPN progress.

Both api/scripts/batch_api_test.py and scraper/scripts/batch_scraper_test.py
print progress lines of the form:
  [ 12/107] STM32G030F6P6  (expected STMicroelectronics)

But run_pipeline.py runs them sequentially in the same pipeline.log, with
phase boundaries marked as:
  [api] $ <PYTHON> api/scripts/batch_api_test.py ...
  [scraper_main] $ <PYTHON> scraper/scripts/batch_scraper_test.py ...
  [merge] $ <PYTHON> common/merge_batch_for_procurement.py ...

If we tail naively, the moment Phase 1 finishes at "[3/3]" and Phase 2
starts, we'd misreport the Phase 1 tail as Phase 2's progress until
Phase 2 prints its own first "[1/3]" (which can take minutes for scraper).

So when caller knows the current phase, we anchor parsing AFTER the latest
matching phase boundary marker.
"""
from __future__ import annotations
import re
from pathlib import Path

_PROGRESS_RE = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")
_PHASE_MARKER_RE = re.compile(r"^\[(\w+)\] \$")
_TAIL_BYTES = 256 * 1024  # cheap; covers both phase boundary and recent progress


def read_progress(log_path: Path, current_phase: str | None = None) -> dict | None:
    """Return {'current': int, 'total': int, 'mpn': str|None} or None.

    `current_phase` ∈ {'api', 'scraper_main', 'merge'} anchors parsing to
    the window between `[<phase>] $` and the next `[<other>] $` boundary.
    Without that bounding, Phase 1's final [3/3] would be misreported as
    Phase 2's progress in the brief gap before Phase 2 prints its own [1/M].
    """
    if not log_path.exists():
        return None
    try:
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    lines = chunk.splitlines()
    start_idx = 0
    end_idx = len(lines)

    if current_phase:
        marker = f"[{current_phase}] $"
        anchor = None
        for i in range(len(lines) - 1, -1, -1):
            if marker in lines[i]:
                anchor = i
                break
        if anchor is None:
            # Phase boundary not in tail — phase just started or marker
            # scrolled out. Either way, no trustworthy progress yet.
            return None
        start_idx = anchor + 1
        for j in range(anchor + 1, len(lines)):
            if _PHASE_MARKER_RE.match(lines[j]):
                end_idx = j
                break

    latest = None
    for line in lines[start_idx:end_idx]:
        m = _PROGRESS_RE.search(line)
        if not m:
            continue
        try:
            cur = int(m.group(1))
            tot = int(m.group(2))
        except ValueError:
            continue
        if tot <= 0:
            continue
        mpn = None
        after = line[m.end():].strip()
        if after:
            parts = after.split()
            if parts:
                mpn = parts[0]
        latest = {"current": cur, "total": tot, "mpn": mpn}
    return latest
