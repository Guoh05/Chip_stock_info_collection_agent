"""Compare stockpool_qty between latest API and Scraper batch runs for LCSC + Digikey.

Rule: same input_mpn, status=ok in BOTH sides; for each (mpn, channel) pair,
the scraper emits one row, the API may emit several (one per warehouse). It's
a match if ANY API warehouse stockpool_qty equals the scraper's stockpool_qty.

Treats empty/null stockpool_qty as `None` (unbounded / lead-time only).
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

API_DIR = Path("test/api/BatchTest_20260519_17_54_29")
SCR_DIR = Path("test/scraper/BatchTest_20260519_14_15_45")
OUT = Path("test/comparison/api_vs_scraper_qty_20260519.md")

CHANNEL_MAP = {
    "LCSC_立创商城": "LCSC",
    "DIGIKEY_得捷电子": "DIGIKEY",
}

# Tolerance for "near match" — treat as close-but-not-exact if relative diff
# of best-aligned (API, SCR) pair is within this. Snapshots were 3.5 h apart,
# so high-volume parts can drift by hundreds.
NEAR_REL_TOL = 0.01     # 1%
NEAR_ABS_TOL = 50       # or absolute diff <= 50 units, whichever is looser


def parse_qty(s: str):
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def load(csv_path: Path):
    rows_per_key = defaultdict(list)
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "ok":
                continue
            ch = CHANNEL_MAP.get(r.get("source", ""))
            if not ch:
                continue
            mpn = r["input_mpn"].strip()
            rows_per_key[(mpn, ch)].append(
                {
                    "qty": parse_qty(r.get("stockpool_qty", "")),
                    "warehouse": (r.get("warehouse") or "").strip(),
                    "ship_text": (r.get("ship_text") or "").strip(),
                }
            )
    return rows_per_key


def classify(api_rows, scr_rows):
    api_qtys_all = [r["qty"] for r in api_rows]
    scr_qtys_all = [r["qty"] for r in scr_rows]
    api_qtys = [q for q in api_qtys_all if q is not None]
    scr_qtys = [q for q in scr_qtys_all if q is not None]

    if not scr_qtys and not api_qtys:
        return "both_null", None
    if not scr_qtys or not api_qtys:
        return "one_side_null", None

    # Exact: any API qty equals any scraper qty.
    if set(api_qtys) & set(scr_qtys):
        return "match", 0

    # Near: smallest diff between any API qty and any scraper qty.
    best_diff = min(abs(a - s) for a in api_qtys for s in scr_qtys)
    best_pair = min(
        ((a, s) for a in api_qtys for s in scr_qtys),
        key=lambda p: abs(p[0] - p[1]),
    )
    a, s = best_pair
    base = max(abs(a), abs(s), 1)
    if best_diff <= NEAR_ABS_TOL or best_diff / base <= NEAR_REL_TOL:
        return "near", best_diff
    return "mismatch", best_diff


api = load(API_DIR / "batch_index.csv")
scr = load(SCR_DIR / "batch_index.csv")
joint_keys = sorted(set(api) & set(scr))

per_channel = defaultdict(lambda: defaultdict(list))   # ch -> verdict -> rows
for (mpn, ch) in joint_keys:
    verdict, diff = classify(api[(mpn, ch)], scr[(mpn, ch)])
    per_channel[ch][verdict].append(
        {
            "mpn": mpn,
            "diff": diff,
            "api": [(r["warehouse"], r["qty"]) for r in api[(mpn, ch)]],
            "scr": [(r["warehouse"] or "(blank)", r["qty"]) for r in scr[(mpn, ch)]],
        }
    )

OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", encoding="utf-8") as f:
    def emit(line=""):
        print(line)
        f.write(line + "\n")

    emit(f"# API vs Scraper — stockpool_qty comparison\n")
    emit(f"- API batch : `{API_DIR}` (run 17:54)")
    emit(f"- Scrap batch: `{SCR_DIR}` (run 14:15)")
    emit(f"- Snapshot gap: ~3.5 h (real stock can drift between runs)")
    emit(f"- Joint (mpn,channel) pairs with status=ok on both sides: **{len(joint_keys)}**")
    emit(f"- `match` = some API warehouse qty == scraper qty")
    emit(f"- `near`  = closest pair within {NEAR_ABS_TOL} units OR within {NEAR_REL_TOL:.0%}")
    emit()

    for ch in ("LCSC", "DIGIKEY"):
        buckets = per_channel[ch]
        total = sum(len(v) for v in buckets.values())
        emit(f"## {ch}  ({total} joint MPNs)\n")
        for v in ("match", "near", "mismatch", "one_side_null", "both_null"):
            emit(f"- {v:14s}: {len(buckets.get(v, []))}")
        emit()

        for label in ("near", "mismatch", "one_side_null"):
            rows = buckets.get(label, [])
            if not rows:
                continue
            emit(f"### {ch} — {label}\n")
            emit("| MPN | diff | API (warehouse=qty) | Scraper (warehouse=qty) |")
            emit("|---|---:|---|---|")
            for row in rows:
                api_str = "; ".join(f"{w or '(blank)'}={q}" for w, q in row["api"])
                scr_str = "; ".join(f"{w}={q}" for w, q in row["scr"])
                d = row["diff"] if row["diff"] is not None else ""
                emit(f"| `{row['mpn']}` | {d} | {api_str} | {scr_str} |")
            emit()

print(f"\n[written] {OUT}", file=sys.stderr)
