"""Regenerate the auto-managed status section of scraper/README.md.

Reads the most recent test/scraper_test/BatchTest_<ts>/ folder and replaces the
text between
    <!-- BEGIN AUTO:status ... -->
and
    <!-- END AUTO:status -->
in scraper/README.md with a fresh snapshot. The rest of the README is hand-
written and left untouched.

Idempotent. Side-effect-free outside the README. Exit code 0 on success or
no-op; 1 only if the README is missing the markers (caller can decide whether
to treat that as fatal).

Usage:
    .venv/Scripts/python.exe scraper/scripts/_update_readme_status.py
"""

from __future__ import annotations

import csv
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
README = PROJECT_ROOT / "scraper" / "README.md"
SCRAPER_TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
BEGIN_RE = re.compile(r"<!-- BEGIN AUTO:status[^>]*-->", re.DOTALL)
END_MARKER = "<!-- END AUTO:status -->"

# Channel state changes rarely; hand-maintained list (edit when a blocker is
# resolved or a new channel goes live). The order here is also the display
# order in the README table and (for working channels) the OK-rate table.
CHANNEL_STATUS = [
    ("**LCSC** (szlcsc.com)",
     "Playwright Chromium `--headless=new` + `__NEXT_DATA__` + DOM right-panel",
     "ok"),
    ("**Digikey** (digikey.cn)",
     "Playwright stealth Chromium + `__NEXT_DATA__` envelope",
     "ok"),
    ("**HQEW** (华强电子网, hqew.com)",
     "Playwright Chromium + supplier-table DOM scrape",
     "ok"),
    ("**Future** (futureelectronics.com)",
     "Playwright **Firefox** (Akamai HTTP/2 bypass)",
     "ok"),
    ("Mouser (mouser.cn)",
     "curl_cffi cascade — blocked by Akamai BotManager `bm-verify`",
     "blocked"),
    ("Arrow (arrow.com)",
     "curl_cffi cascade — blocked by Akamai BotManager `_abck`",
     "blocked"),
]
STATUS_ICON = {"ok": "✅", "pending": "⏳", "blocked": "❌"}

# Channel codes that appear in batch_index.csv. Used to order the per-channel
# pass-rate table and to read the right `<ch>_status` columns from batch_compare.
WORKING_CHANNELS = ["LCSC", "DIGIKEY", "HQEW", "FUTURE"]

# Display form for the per-channel results table (initialisms stay uppercase,
# proper nouns are capitalized). Unknown channels fall back to .title().
CHANNEL_DISPLAY = {
    "LCSC": "LCSC",
    "DIGIKEY": "Digikey",
    "HQEW": "HQEW",
    "FUTURE": "Future",
    "MOUSER": "Mouser",
    "ARROW": "Arrow",
}


def find_latest_batch() -> Path | None:
    if not SCRAPER_TEST_ROOT.exists():
        return None
    batches = sorted(SCRAPER_TEST_ROOT.glob("BatchTest_*"))
    return batches[-1] if batches else None


def parse_batch(batch_dir: Path) -> dict:
    """Return per-channel counts + cross-channel coverage stats. Empty dict on
    any read failure (we'd rather render a degraded snapshot than crash)."""
    idx_path = batch_dir / "batch_index.csv"
    cmp_path = batch_dir / "batch_compare.csv"
    if not idx_path.exists():
        return {}
    try:
        with open(idx_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return {}
    per_ch: dict[str, dict] = {}
    for r in rows:
        d = per_ch.setdefault(
            r["channel"],
            {"ok": 0, "no_results": 0, "blocked": 0, "failed": 0, "total": 0},
        )
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

    # Cross-channel coverage histogram: per chip, count channels that returned ok.
    coverage_hist: dict[int, int] = {}
    mfr_mismatches: list[dict] = []
    comparisons: list[dict] = []
    if cmp_path.exists():
        try:
            with open(cmp_path, encoding="utf-8-sig") as f:
                comparisons = list(csv.DictReader(f))
        except OSError:
            comparisons = []
        for c in comparisons:
            n_ok = sum(
                1 for ch in WORKING_CHANNELS
                if c.get(f"{ch.lower()}_status") == "ok"
            )
            coverage_hist[n_ok] = coverage_hist.get(n_ok, 0) + 1
            for ch in WORKING_CHANNELS:
                if c.get(f"{ch.lower()}_status") == "ok" and (
                    c.get(f"{ch.lower()}_mfr_match", "") or ""
                ).lower() == "false":
                    returned = c.get(f"{ch.lower()}_returned_mfr", "")
                    if not returned:
                        continue
                    mfr_mismatches.append(
                        {
                            "mpn": c.get("input_mpn", ""),
                            "channel": ch,
                            "expected": c.get("expected_mfr", ""),
                            "returned": returned,
                        }
                    )
    return {
        "n_chips": len(comparisons) or len({r["input_mpn"] for r in rows}),
        "per_channel": per_ch,
        "coverage_hist": coverage_hist,
        "mfr_mismatches": mfr_mismatches,
    }


def render_block(today: str, batch_dir: Path | None, stats: dict) -> str:
    """Render everything between (and including) the BEGIN/END markers."""
    out: list[str] = []
    out.append(
        '<!-- BEGIN AUTO:status — managed by scraper/scripts/_update_readme_status.py '
        '(see "Auto-updating this README" at bottom) -->'
    )
    out.append("")
    out.append(f"## Status snapshot ({today})")
    out.append("")
    out.append("| Channel | Method | Working? |")
    out.append("|---|---|---|")
    for ch, method, st in CHANNEL_STATUS:
        out.append(f"| {ch} | {method} | {STATUS_ICON.get(st, '?')} |")
    out.append("")
    if not batch_dir or not stats or not stats.get("per_channel"):
        out.append("_No batch runs yet in `test/scraper_test/`._")
        out.append("")
    else:
        rel = batch_dir.relative_to(PROJECT_ROOT).as_posix()
        n_chips = stats["n_chips"]
        total_calls = sum(d["total"] for d in stats["per_channel"].values())
        n_ch_run = len(stats["per_channel"])
        out.append(
            f"**Latest batch run:** `{rel}/` — {n_chips} MPNs × "
            f"{n_ch_run} channel(s) = {total_calls} calls."
        )
        out.append("")
        out.append("| Channel | OK | No results | Blocked | Failed | OK % |")
        out.append("|---|---|---|---|---|---|")
        ordered = [c for c in WORKING_CHANNELS if c in stats["per_channel"]] + sorted(
            c for c in stats["per_channel"] if c not in WORKING_CHANNELS
        )
        for ch in ordered:
            d = stats["per_channel"][ch]
            pct = 100.0 * d["ok"] / d["total"] if d["total"] else 0.0
            label = CHANNEL_DISPLAY.get(ch, ch.title())
            out.append(
                f"| {label} | {d['ok']} | {d['no_results']} | "
                f"{d['blocked']} | {d['failed']} | {pct:.1f} % |"
            )
        out.append("")
        ch_hist = stats.get("coverage_hist") or {}
        if ch_hist:
            # Render as a short sentence rather than a table — most readable in
            # a README snapshot. Example: "25 chips returned ok on all 4
            # channels; 28 on 3; 28 on 2; 19 on 1; 3 on none."
            parts: list[str] = []
            for n in sorted(ch_hist.keys(), reverse=True):
                count = ch_hist[n]
                if n == n_ch_run:
                    parts.append(f"**{count}** chip(s) returned ok on all {n_ch_run} channels")
                elif n == 0:
                    parts.append(f"{count} on none")
                else:
                    parts.append(f"{count} on {n}")
            out.append("Cross-channel coverage: " + "; ".join(parts) + ".")
            out.append("")
        mm = stats["mfr_mismatches"]
        if mm:
            preview = ", ".join(
                f"`{m['mpn']}` ({m['channel']}: {m['expected']} → {m['returned']})"
                for m in mm[:5]
            )
            tail = f", and {len(mm) - 5} more" if len(mm) > 5 else ""
            out.append(
                f"**Manufacturer-name mismatches surfaced:** {len(mm)} — "
                f"{preview}{tail}."
            )
            out.append("")
        else:
            out.append("No manufacturer-name mismatches in the latest run.")
            out.append("")
    out.append(END_MARKER)
    return "\n".join(out)


def main() -> int:
    if not README.exists():
        print(f"ERROR: {README} not found", file=sys.stderr)
        return 1
    text = README.read_text(encoding="utf-8")
    begin_match = BEGIN_RE.search(text)
    end_pos = text.find(END_MARKER)
    if not begin_match or end_pos < 0 or end_pos < begin_match.end():
        print(
            f"ERROR: AUTO:status markers not found (or out of order) in {README}",
            file=sys.stderr,
        )
        return 1
    batch_dir = find_latest_batch()
    stats = parse_batch(batch_dir) if batch_dir else {}
    new_block = render_block(datetime.now().strftime("%Y-%m-%d"), batch_dir, stats)
    new_text = text[: begin_match.start()] + new_block + text[end_pos + len(END_MARKER):]
    if new_text == text:
        print(f"  {README.relative_to(PROJECT_ROOT)}: no change")
    else:
        README.write_text(new_text, encoding="utf-8")
        print(f"  {README.relative_to(PROJECT_ROOT)}: updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
