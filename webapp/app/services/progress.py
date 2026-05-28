"""Parse pipeline.log tail to extract current MPN progress.

Both api/scripts/batch_api_test.py and scraper/scripts/batch_scraper_test.py
print progress lines of the form:
  [ 12/107] STM32G030F6P6  (expected STMicroelectronics)
We tail the log and report the last matching N/M for the run page progress bar.

Returns None if no progress line found yet (early startup, log empty, or
phase boundary). Zero-impact on pipeline code — pure read of its stdout log.
"""
from __future__ import annotations
import re
from pathlib import Path

_PROGRESS_RE = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")
_TAIL_BYTES = 64 * 1024  # enough to catch a recent progress line in noisy log


def read_progress(log_path: Path) -> dict | None:
    """Return {'current': int, 'total': int, 'mpn': str|None} or None."""
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

    for line in reversed(chunk.splitlines()):
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
        # Heuristic: MPN often appears right after the ] bracket
        mpn = None
        after = line[m.end():].strip()
        if after:
            mpn = after.split()[0] if after.split() else None
        return {"current": cur, "total": tot, "mpn": mpn}
    return None
