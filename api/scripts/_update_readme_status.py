"""Regenerate the auto-managed status section of api/README.md.

Reads the most recent test/api_test/BatchTest_<ts>/ folder and replaces the
text between
    <!-- BEGIN AUTO:status ... -->
and
    <!-- END AUTO:status -->
in api/README.md with a fresh snapshot. The rest of the README is hand-written
and left untouched.

Idempotent. Side-effect-free outside the README. Exit code 0 on success or
no-op; 1 only if the README is missing the markers (caller can decide whether
to treat that as fatal).

Usage:
    .venv/Scripts/python.exe api/scripts/_update_readme_status.py
"""

from __future__ import annotations

import csv
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
README = PROJECT_ROOT / "api" / "README.md"
API_TEST_ROOT = PROJECT_ROOT / "test" / "api_test"
BEGIN_RE = re.compile(r"<!-- BEGIN AUTO:status[^>]*-->", re.DOTALL)
END_MARKER = "<!-- END AUTO:status -->"

# Vendor state changes rarely; hand-maintained dict (edit when a new vendor goes live).
VENDOR_STATUS = [
    ("**Mouser** Search API v1",
     "POST api.mouser.com/api/v1/search/partnumber (fallback /search/keyword)",
     "API key in querystring",
     "ok"),
    ("**Digikey** Product Information API v4",
     "POST api.digikey.com/products/v4/search/keyword",
     "OAuth2 client_credentials → bearer",
     "ok"),
    ("Octopart / Nexar",
     "not started",
     "OAuth2 (keys not yet acquired)",
     "pending"),
    ("Element14 / Farnell",
     "not started",
     "API key (key not yet acquired)",
     "pending"),
]
STATUS_ICON = {"ok": "✅", "pending": "⏳", "blocked": "❌"}


def find_latest_batch() -> Path | None:
    if not API_TEST_ROOT.exists():
        return None
    batches = sorted(API_TEST_ROOT.glob("BatchTest_*"))
    return batches[-1] if batches else None


def parse_batch(batch_dir: Path) -> dict:
    """Return per-channel counts + cross-channel agreement stats. Empty dict
    on any read failure (we'd rather render a degraded snapshot than crash)."""
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
            r["channel"], {"ok": 0, "no_results": 0, "failed": 0, "total": 0}
        )
        d["total"] += 1
        if r["status"] == "ok":
            d["ok"] += 1
        elif r["status"] == "no_results":
            d["no_results"] += 1
        else:
            d["failed"] += 1
    both_ok = stock_both = only_mouser = only_digikey = neither = 0
    mfr_mismatches: list[dict] = []
    comparisons: list[dict] = []
    if cmp_path.exists():
        try:
            with open(cmp_path, encoding="utf-8-sig") as f:
                comparisons = list(csv.DictReader(f))
        except OSError:
            comparisons = []
        for c in comparisons:
            if c.get("mouser_status") == "ok" and c.get("digikey_status") == "ok":
                both_ok += 1
                d = c.get("stock_now_disagreement", "")
                if d == "both_have_stock":
                    stock_both += 1
                elif d == "only_mouser":
                    only_mouser += 1
                elif d == "only_digikey":
                    only_digikey += 1
                else:
                    neither += 1
            for ch in ("mouser", "digikey"):
                if c.get(f"{ch}_status") == "ok" and (
                    c.get(f"mfr_match_{ch}", "") or ""
                ).lower() == "false":
                    mfr_mismatches.append(
                        {
                            "mpn": c.get("input_mpn", ""),
                            "channel": ch.upper(),
                            "expected": c.get("expected_mfr", ""),
                            "returned": c.get(f"{ch}_returned_mfr", ""),
                        }
                    )
    return {
        "n_chips": len(comparisons) or len({r["input_mpn"] for r in rows}),
        "per_channel": per_ch,
        "both_ok": both_ok,
        "stock_both": stock_both,
        "only_mouser": only_mouser,
        "only_digikey": only_digikey,
        "neither": neither,
        "mfr_mismatches": mfr_mismatches,
    }


def render_block(today: str, batch_dir: Path | None, stats: dict) -> str:
    """Render everything between (and including) the BEGIN/END markers."""
    out: list[str] = []
    out.append(
        '<!-- BEGIN AUTO:status — managed by api/scripts/_update_readme_status.py '
        '(see "Auto-updating this README" at bottom) -->'
    )
    out.append("")
    out.append(f"## Status snapshot ({today})")
    out.append("")
    out.append("| Vendor | Endpoint | Auth | Working? |")
    out.append("|---|---|---|---|")
    for v, ep, auth, st in VENDOR_STATUS:
        ep_cell = f"`{ep}`" if ep.lower().startswith(("post ", "get ", "http")) else ep
        out.append(f"| {v} | {ep_cell} | {auth} | {STATUS_ICON.get(st, '?')} |")
    out.append("")
    if not batch_dir or not stats or not stats.get("per_channel"):
        out.append("_No batch runs yet in `test/api_test/`._")
        out.append("")
    else:
        rel = batch_dir.relative_to(PROJECT_ROOT).as_posix()
        n_chips = stats["n_chips"]
        total_calls = sum(d["total"] for d in stats["per_channel"].values())
        out.append(
            f"**Latest batch run:** `{rel}/` — {n_chips} MPNs × "
            f"{len(stats['per_channel'])} channel(s) = {total_calls} calls."
        )
        out.append("")
        out.append("| Channel | OK | No results | Failed | OK % |")
        out.append("|---|---|---|---|---|")
        preferred = ["MOUSER", "DIGIKEY"]
        ordered = [c for c in preferred if c in stats["per_channel"]] + sorted(
            c for c in stats["per_channel"] if c not in preferred
        )
        for ch in ordered:
            d = stats["per_channel"][ch]
            pct = 100.0 * d["ok"] / d["total"] if d["total"] else 0.0
            out.append(
                f"| {ch.title()} | {d['ok']} | {d['no_results']} | "
                f"{d['failed']} | {pct:.1f} % |"
            )
        out.append("")
        if stats["both_ok"]:
            out.append(
                f"Both channels returned a usable result for **{stats['both_ok']}** of "
                f"the {n_chips} chips. Of those: {stats['stock_both']} have stock at "
                f"both, {stats['only_digikey']} only at Digikey, "
                f"{stats['only_mouser']} only at Mouser, "
                f"{stats['neither']} factory-order at both."
            )
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
