"""Regenerate the auto-managed status sections of the scraper docs.

Two destinations:

1. `scraper/README.md` — between
       <!-- BEGIN AUTO:status ... -->
       <!-- END AUTO:status -->

2. `scraper/doc/source_technical_reference.md` — between
       <!-- BEGIN AUTO:source_status ... -->
       <!-- END AUTO:source_status -->

Both blocks are rendered from the most recent `test/scraper/BatchTest_<ts>/`
folder, with the hand-maintained `CHANNEL_STATUS` table at the top of this file
as the source of truth for which sources exist and what method each uses.

Idempotent. Side-effect-free outside the two target files. Exit code 0 on
success / no-op; 1 only if both files are missing their markers.

v3 schema (2026-05-19+): reads `batch_index.csv`'s `source` column (bilingual,
e.g. `LCSC_立创商城`) — split on first `_` to recover the short enum. Computes
cross-source coverage by deduping `batch_index.csv` rows on `(input_mpn, source)`
(no `batch_compare.csv` since v3).

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
SOURCE_TECH_REF = PROJECT_ROOT / "scraper" / "doc" / "source_technical_reference.md"
SCRAPER_TEST_ROOT = PROJECT_ROOT / "test" / "scraper"

# Marker pairs per target file
TARGETS = {
    README: {
        "begin_re": re.compile(r"<!-- BEGIN AUTO:status[^>]*-->", re.DOTALL),
        "end_marker": "<!-- END AUTO:status -->",
        "renderer": "render_readme_block",
    },
    SOURCE_TECH_REF: {
        "begin_re": re.compile(r"<!-- BEGIN AUTO:source_status[^>]*-->", re.DOTALL),
        "end_marker": "<!-- END AUTO:source_status -->",
        "renderer": "render_source_ref_block",
    },
}

# Hand-maintained channel list — edit when a blocker is resolved or a new
# channel goes live. The order here is also the display order in the tables.
# Tuple format: (short_code, display_label, method_description, status, script_basename)
CHANNEL_STATUS = [
    ("LCSC", "**LCSC** (立创商城, szlcsc.com)",
     "Playwright Chromium `--headless=new` + `__NEXT_DATA__` + DOM right-panel",
     "ok", "scrape_lcsc_v3.py"),
    ("DIGIKEY", "**Digikey** (得捷电子, digikey.cn)",
     "Playwright stealth Chromium + `__NEXT_DATA__` envelope",
     "ok", "scrape_digikey.py"),
    ("HQEW", "**HQEW** (华强电子网, hqew.com)",
     "Playwright Chromium + supplier-table DOM scrape (top-5 per chip)",
     "ok", "scrape_hqew.py"),
    ("FUTURE", "**Future** (Future Electronics, futureelectronics.com)",
     "Playwright **Firefox** (Akamai HTTP/2 bypass) + cookie-banner dismiss",
     "ok", "scrape_future.py"),
    ("RSONLINE", "**RSONLINE** (RS 欧时, rsonline.cn)",
     "curl_cffi + Next.js `__NEXT_DATA__` + Adobe data-layer `stockinfo`",
     "ok", "scrape_rsonline.py"),
    ("ONEYAC", "**ONEYAC** (唯样商城, oneyac.com)",
     "Playwright Firefox + main-product card extraction (not recommended-carousel)",
     "ok", "scrape_oneyac.py"),
    ("ICKEY", "**ICKEY** (云汉芯城, ickey.cn)",
     "Playwright Chromium + doT.js template hydration wait (marketplace aggregator)",
     "ok", "scrape_ickey.py"),
    ("ROCHESTER", "**Rochester** (Rochester Electronics, rocelec.com)",
     "Playwright Firefox + LWC hydration + exact-MPN guard (EOL-only)",
     "ok", "scrape_rochester.py"),
    ("BOM2BUY", "**bom2buy** (买芯片网, bom2buy.com)",
     "Playwright + Opera user-data-dir reuse (requires user-managed IconCaptcha session; Opera must be fully closed when scraping)",
     "ok", "scrape_bom2buy.py"),
    ("MOUSER", "Mouser (贸泽, mouser.cn / .com)",
     "Blocked by Akamai BotManager `bm-verify` — use api/scripts/api_mouser.py instead",
     "blocked", "scrape_mouser_v2.py"),
    ("ARROW", "Arrow (艾睿, arrow.com)",
     "Blocked by Akamai BotManager `_abck` — use api/scripts/api_arrow.py (key pending)",
     "blocked", "scrape_arrow_v2.py"),
]

# Sources evaluated and permanently dropped — not scraped, kept here for the
# benefit of anyone reading the doc and wondering "did we try X?".
DROPPED_SOURCES = [
    # bom2buy is now working via Playwright + Opera profile reuse — moved to CHANNEL_STATUS above.
    ("e络盟 Element14 (cn.element14.com)",
     "Akamai BMP 403 (same family as Mouser/Arrow). Use api/scripts/api_element14.py (key pending)."),
    ("Verical (verical.com)",
     "Arrow legacy site, half-broken (`系统错误` popup + WAF on repeat probes). User chose to drop."),
    ("Chip1Stop (chip1stop.com)",
     "301-redirects to arrow.com after acquisition. No separate scraping path."),
]

STATUS_ICON = {"ok": "✅", "pending": "⏳", "blocked": "❌"}

# Channel short codes (must match the `CHANNELS` dict in batch_scraper_test.py
# and the bilingual `SOURCE_LABEL` prefix). Used for ordering the per-source
# pass-rate table.
WORKING_CHANNELS = ["LCSC", "DIGIKEY", "HQEW", "FUTURE", "RSONLINE", "ONEYAC", "ICKEY", "ROCHESTER", "BOM2BUY"]

# Display form for the per-channel results table (initialisms stay uppercase,
# proper nouns are capitalized). Unknown channels fall back to .title().
CHANNEL_DISPLAY = {
    "LCSC": "LCSC",
    "DIGIKEY": "Digikey",
    "HQEW": "HQEW",
    "FUTURE": "Future",
    "RSONLINE": "RS Online",
    "ONEYAC": "Oneyac",
    "ICKEY": "ICKEY",
    "ROCHESTER": "Rochester",
    "BOM2BUY": "Bom2buy",
    "MOUSER": "Mouser",
    "ARROW": "Arrow",
}

# Bilingual display label written into the CSV — keep in sync with SOURCE_LABEL
# in batch_scraper_test.py. Used for the per-source pass-rate table caption.
SOURCE_LABEL_DISPLAY = {
    "LCSC": "LCSC_立创商城",
    "DIGIKEY": "DIGIKEY_得捷电子",
    "HQEW": "HQEW_华强电子网",
    "FUTURE": "FUTURE_Future_Electronics",
    "RSONLINE": "RSONLINE_RS欧时",
    "ONEYAC": "ONEYAC_唯样商城",
    "ICKEY": "ICKEY_云汉芯城",
    "ROCHESTER": "ROCHESTER_Rochester_Electronics",
    "BOM2BUY": "BOM2BUY_买芯片网",
}


def find_latest_batch() -> Path | None:
    """Pick the most recent FULL multi-source batch.

    Single-source / per-channel pilot batches (e.g. `BatchTest_*_bom2buy/`) are
    skipped because rendering the status snapshot from them would show 0/0/0
    for every other source, which is misleading. We require >= 3 distinct
    sources in `batch_index.csv` to consider a folder a "full batch".
    """
    if not SCRAPER_TEST_ROOT.exists():
        return None
    candidates = sorted(SCRAPER_TEST_ROOT.glob("BatchTest_*"), reverse=True)
    for b in candidates:
        idx = b / "batch_index.csv"
        if not idx.exists():
            continue
        try:
            with open(idx, encoding="utf-8-sig") as f:
                seen = set()
                for r in csv.DictReader(f):
                    src = r.get("source") or r.get("channel") or ""
                    seen.add(_short_source(src))
                    if len(seen) >= 3:
                        return b
        except OSError:
            continue
    # No multi-source batch found — fall back to most recent regardless.
    return candidates[0] if candidates else None


def _short_source(s: str) -> str:
    """v3 `source` cells are bilingual (`LCSC_立创商城`); recover the short
    enum prefix (`LCSC`). v1 cells were already short — pass through.
    """
    if not s:
        return s
    # Some short codes contain digits but no underscore; split on first `_`
    return s.split("_", 1)[0]


def parse_batch(batch_dir: Path) -> dict:
    """Return per-channel counts + cross-channel coverage stats + mfr mismatches.
    Empty dict on any read failure (we'd rather render a degraded snapshot than
    crash).

    v3 schema: reads `batch_index.csv` only — the warehouse-exploded form. Cells
    are deduped on `(input_mpn, source)` before counting, since one cell can
    span multiple warehouse rows.
    """
    idx_path = batch_dir / "batch_index.csv"
    if not idx_path.exists():
        return {}
    try:
        with open(idx_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return {}

    # Dedupe to one entry per (input_mpn, source) — warehouse rows of the same
    # cell share status / mfr_match / returned_mfr.
    cell_rows: dict[tuple[str, str], dict] = {}
    for r in rows:
        # Accept both v3 (`source`) and v1 (`channel`) for forward/backward compat.
        src_raw = r.get("source") or r.get("channel", "")
        src = _short_source(src_raw)
        key = (r.get("input_mpn", ""), src)
        cell_rows.setdefault(key, r)

    per_ch: dict[str, dict] = {}
    for (mpn, src), r in cell_rows.items():
        d = per_ch.setdefault(
            src,
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

    # Cross-source coverage histogram: per chip, count sources that returned ok.
    by_chip: dict[str, set[str]] = {}
    for (mpn, src), r in cell_rows.items():
        if r.get("status") == "ok":
            by_chip.setdefault(mpn, set()).add(src)
    all_chips = {mpn for (mpn, _src) in cell_rows.keys()}
    coverage_hist: dict[int, int] = {}
    for chip in all_chips:
        n_ok = len(by_chip.get(chip, set()))
        coverage_hist[n_ok] = coverage_hist.get(n_ok, 0) + 1

    # Manufacturer-name mismatches: rows where status=ok and mfr_match=False.
    mfr_mismatches: list[dict] = []
    for (mpn, src), r in cell_rows.items():
        if r.get("status") != "ok":
            continue
        if (r.get("mfr_match", "") or "").lower() != "false":
            continue
        returned = r.get("returned_mfr", "")
        if not returned:
            continue
        mfr_mismatches.append({
            "mpn": mpn,
            "channel": src,
            "expected": r.get("expected_mfr", ""),
            "returned": returned,
        })

    n_sources_run = len(per_ch)
    return {
        "n_chips": len(all_chips),
        "n_sources_run": n_sources_run,
        "per_channel": per_ch,
        "coverage_hist": coverage_hist,
        "mfr_mismatches": mfr_mismatches,
    }


def render_readme_block(today: str, batch_dir: Path | None, stats: dict) -> str:
    """Block for scraper/README.md — channel table + latest-batch snapshot."""
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
    for _short, ch, method, st, _script in CHANNEL_STATUS:
        out.append(f"| {ch} | {method} | {STATUS_ICON.get(st, '?')} |")
    out.append("")
    if not batch_dir or not stats or not stats.get("per_channel"):
        out.append("_No batch runs yet in `test/scraper/`._")
        out.append("")
    else:
        rel = batch_dir.relative_to(PROJECT_ROOT).as_posix()
        n_chips = stats["n_chips"]
        n_ch_run = stats["n_sources_run"]
        total_cells = sum(d["total"] for d in stats["per_channel"].values())
        out.append(
            f"**Latest batch run:** `{rel}/` — {n_chips} MPNs × "
            f"{n_ch_run} source(s) = {total_cells} cells."
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
            parts: list[str] = []
            for n in sorted(ch_hist.keys(), reverse=True):
                count = ch_hist[n]
                if n == n_ch_run:
                    parts.append(f"**{count}** chip(s) returned ok on all {n_ch_run} sources")
                elif n == 0:
                    parts.append(f"{count} on none")
                else:
                    parts.append(f"{count} on {n}")
            out.append("Cross-source coverage: " + "; ".join(parts) + ".")
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
    out.append("<!-- END AUTO:status -->")
    return "\n".join(out)


def render_source_ref_block(today: str, batch_dir: Path | None, stats: dict) -> str:
    """Block for scraper/doc/source_technical_reference.md — all-source table
    incl. dropped, with current pass-rate where available.
    """
    out: list[str] = []
    out.append(
        '<!-- BEGIN AUTO:source_status — managed by scraper/scripts/_update_readme_status.py. '
        'Hand edits between these markers will be overwritten on the next batch run. -->'
    )
    out.append("")
    out.append(f"## Current status snapshot ({today})")
    out.append("")
    if batch_dir and stats and stats.get("per_channel"):
        rel = batch_dir.relative_to(PROJECT_ROOT).as_posix()
        n_chips = stats["n_chips"]
        n_ch_run = stats["n_sources_run"]
        total_cells = sum(d["total"] for d in stats["per_channel"].values())
        out.append(
            f"**Latest batch:** `{rel}/` — {n_chips} MPNs × {n_ch_run} sources = {total_cells} cells."
        )
        out.append("")
        out.append("### Working sources")
        out.append("")
        out.append("| Source | Script | OK | No results | Blocked | Failed | OK % |")
        out.append("|---|---|---|---|---|---|---|")
        for short, ch, _method, st, script in CHANNEL_STATUS:
            if st != "ok":
                continue
            d = stats["per_channel"].get(short, {"ok": 0, "no_results": 0,
                                                  "blocked": 0, "failed": 0, "total": 0})
            pct = 100.0 * d["ok"] / d["total"] if d["total"] else 0.0
            label = SOURCE_LABEL_DISPLAY.get(short, short)
            out.append(
                f"| **{label}** | `{script}` | {d['ok']} | {d['no_results']} | "
                f"{d['blocked']} | {d['failed']} | {pct:.1f} % |"
            )
        out.append("")
    else:
        out.append("_No batch runs yet in `test/scraper/`._")
        out.append("")
        out.append("### Working sources (hand-maintained — no batch data yet)")
        out.append("")
        out.append("| Source | Script |")
        out.append("|---|---|")
        for short, _ch, _method, st, script in CHANNEL_STATUS:
            if st != "ok":
                continue
            label = SOURCE_LABEL_DISPLAY.get(short, short)
            out.append(f"| **{label}** | `{script}` |")
        out.append("")

    # Blocked sources — same list every time
    out.append("### Blocked sources")
    out.append("")
    out.append("| Source | Reason | Workaround |")
    out.append("|---|---|---|")
    for _short, ch, method, st, _script in CHANNEL_STATUS:
        if st != "blocked":
            continue
        out.append(f"| {ch} | {method} | API track |")
    out.append("")

    # Dropped sources — hand-maintained list at top of file
    out.append("### Evaluated and dropped")
    out.append("")
    out.append("| Source | Why dropped |")
    out.append("|---|---|")
    for name, reason in DROPPED_SOURCES:
        out.append(f"| {name} | {reason} |")
    out.append("")

    out.append(
        "_See the per-source sections below for technical details. This table "
        "is regenerated by `scraper/scripts/_update_readme_status.py` after "
        "every batch run; hand edits between the AUTO markers will be lost._"
    )
    out.append("")
    out.append("<!-- END AUTO:source_status -->")
    return "\n".join(out)


def _apply_block(path: Path, begin_re: re.Pattern, end_marker: str,
                 new_block: str) -> str:
    text = path.read_text(encoding="utf-8")
    begin_match = begin_re.search(text)
    end_pos = text.find(end_marker)
    if not begin_match or end_pos < 0 or end_pos < begin_match.end():
        return ""   # marker missing — caller reports
    new_text = text[: begin_match.start()] + new_block + text[end_pos + len(end_marker):]
    return new_text


def main() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    batch_dir = find_latest_batch()
    stats = parse_batch(batch_dir) if batch_dir else {}

    renderers = {
        "render_readme_block": render_readme_block,
        "render_source_ref_block": render_source_ref_block,
    }

    any_updated = False
    any_failure = False
    for path, cfg in TARGETS.items():
        if not path.exists():
            print(f"  {path.relative_to(PROJECT_ROOT)}: file not found, skipping",
                  file=sys.stderr)
            any_failure = True
            continue
        renderer = renderers[cfg["renderer"]]
        new_block = renderer(today, batch_dir, stats)
        new_text = _apply_block(path, cfg["begin_re"], cfg["end_marker"], new_block)
        if not new_text:
            print(
                f"  {path.relative_to(PROJECT_ROOT)}: AUTO markers missing or "
                f"out of order — skipping",
                file=sys.stderr,
            )
            any_failure = True
            continue
        old_text = path.read_text(encoding="utf-8")
        if new_text == old_text:
            print(f"  {path.relative_to(PROJECT_ROOT)}: no change")
        else:
            path.write_text(new_text, encoding="utf-8")
            print(f"  {path.relative_to(PROJECT_ROOT)}: updated")
            any_updated = True
    # Exit code: 0 if anything succeeded, 1 only when EVERY target was missing.
    return 0 if (any_updated or not any_failure) else 1


if __name__ == "__main__":
    sys.exit(main())
