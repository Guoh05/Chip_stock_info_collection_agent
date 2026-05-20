"""One-shot evaluation of the Firecrawl /v2/scrape REST API.

Two questions answered:
1. Can Firecrawl reach pages we can't?  →  Phase 1 probes bom2buy.com (CAPTCHA-gated).
2. Is its data better than our scraper on sources we already do?
   →  Phase 2 scrapes the same detail URLs our scrapers landed on for 5 sources
       (ICKEY / Rochester / ONEYAC / RSONLINE / Future) × 2 MPNs each, both as
       plain markdown and as canonical-schema JSON extraction.

Outputs land in test/scraper_test/Firecrawl_Eval_<ts>/.
Plan: ~/.claude/plans/cozy-singing-hennessy.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
ENV_PATH = PROJECT_ROOT / "api" / ".env"
API_URL = "https://api.firecrawl.dev/v2/scrape"
SCRAPER_BATCH = TEST_ROOT / "BatchTest_20260520_07_40_03"

CREDIT_BUDGET_CAP = 70  # abort if observed credits used exceed this

CANONICAL_SCHEMA = {
    "type": "object",
    "properties": {
        "manufacturer_part_number": {"type": "string"},
        "manufacturer": {"type": "string"},
        "stock_now_qty": {"type": ["integer", "null"]},
        "stock_now_ship_text": {"type": ["string", "null"]},
        "stock_future_qty": {"type": ["integer", "null"]},
        "stock_future_ship_text": {"type": ["string", "null"]},
        "stock_breakdown": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "warehouse": {"type": ["string", "null"]},
                    "quantity": {"type": ["integer", "null"]},
                    "ship_text": {"type": ["string", "null"]},
                    "moq": {"type": ["integer", "null"]},
                },
            },
        },
        "prices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "min_qty": {"type": "integer"},
                    "unit_price": {"type": "number"},
                    "currency": {"type": "string"},
                },
            },
        },
        "datasheet_url": {"type": ["string", "null"]},
        "package": {"type": ["string", "null"]},
        "lifecycle_status": {"type": ["string", "null"]},
        "min_order_qty": {"type": ["integer", "null"]},
    },
}

BOM2BUY_TARGETS = [
    ("search_STM32",      "https://www.bom2buy.com/search?keyword=STM32G030F6P6"),
    ("search_BT168",      "https://www.bom2buy.com/search?keyword=BT168GW%2C115"),
    ("search_ATXMEGA",    "https://www.bom2buy.com/search?keyword=ATXMEGA32E5-ANR"),
    ("homepage",          "https://www.bom2buy.com/"),
    # 5th probe (a product detail URL) is added dynamically after the search probes
    # if any of them returned anchor URLs we can follow.
]

COMPARISON_TARGETS = [
    # (source, input_mpn, detail_url, scraper_json_relative_path)
    ("ICKEY",     "STM32G030F6P6",      "https://www.ickey.cn/detail/1000201010915684/STM32G030F6P6.html",
     "Test_STM32G030F6P6_ICKEY/STM32G030F6P6.json"),
    ("ICKEY",     "CY8C4025AZI-S413T",  "https://www.ickey.cn/detail/1000201010869993/CY8C4025AZI-S413T.html",
     "Test_CY8C4025AZI-S413T_ICKEY/CY8C4025AZI-S413T.json"),
    ("ROCHESTER", "IRLML5103TRPBF",     "https://www.rocelec.com/part/01t4w00000PPCKKAA5-IRLML5103TRPBF",
     "Test_IRLML5103TRPBF_ROCHESTER/IRLML5103TRPBF.json"),
    ("ROCHESTER", "L78L33ABUTR",        "https://www.rocelec.com/part/01tRl000003cXEOIA2-L78L33ABUTR",
     "Test_L78L33ABUTR_ROCHESTER/L78L33ABUTR.json"),
    ("ONEYAC",    "ATXMEGA32E5-ANR",    "https://www.oneyac.com/product/15981551.html",
     "Test_ATXMEGA32E5-ANR_ONEYAC/ATXMEGA32E5-ANR.json"),
    ("ONEYAC",    "BT168GW,115",        "https://www.oneyac.com/product/30800157.html",
     "Test_BT168GW_115_ONEYAC/BT168GW_115.json"),
    ("RSONLINE",  "STM32G030F6P6",      "https://www.rsonline.cn/web/p/microcontrollers/2396333",
     "Test_STM32G030F6P6_RSONLINE/STM32G030F6P6.json"),
    ("RSONLINE",  "CY8C4025AZI-S413T",  "https://www.rsonline.cn/web/p/microcontrollers/2733295",
     "Test_CY8C4025AZI-S413T_RSONLINE/CY8C4025AZI-S413T.json"),
    ("FUTURE",    "CY8C4025AZI-S413T",
     "https://www.futureelectronics.com/p/semiconductors--microcontrollers--32-bit/cy8c4025azi-s413t-infineon-1127137",
     "Test_CY8C4025AZI-S413T_FUTURE/CY8C4025AZI-S413T/CY8C4025AZI-S413T.json"),
    ("FUTURE",    "STM32G030F6P6",
     "https://www.futureelectronics.com/p/semiconductors--microcontrollers--32-bit/stm32g030f6p6-stmicroelectronics-8137468",
     "Test_STM32G030F6P6_FUTURE/STM32G030F6P6/STM32G030F6P6.json"),
]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


class CreditTracker:
    def __init__(self, cap: int = CREDIT_BUDGET_CAP):
        self.used = 0
        self.calls = 0
        self.failures = 0
        self.cap = cap

    def record(self, response_meta: dict) -> int:
        self.calls += 1
        delta = 0
        for k in ("creditsUsed", "credits_used", "creditCharged"):
            if k in (response_meta or {}):
                try:
                    delta = int(response_meta[k])
                    break
                except Exception:
                    pass
        self.used += delta
        return delta

    def fail(self):
        self.calls += 1
        self.failures += 1

    def over_cap(self) -> bool:
        return self.used > self.cap


def firecrawl_scrape(url: str, mode: str, api_key: str, schema: dict | None = None,
                     timeout: int = 120) -> tuple[int, dict, float]:
    """POST to /v2/scrape and return (status_code, body, elapsed_sec)."""
    body: dict = {"url": url, "onlyMainContent": False}
    if mode == "markdown":
        body["formats"] = ["markdown", "links"]
    elif mode == "json":
        # v2 syntax: object-form format
        body["formats"] = [{"type": "json", "schema": schema}]
    else:
        raise ValueError(f"unknown mode: {mode}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.time()
    try:
        r = requests.post(API_URL, json=body, headers=headers, timeout=timeout)
        elapsed = time.time() - t0
        try:
            j = r.json()
        except Exception:
            j = {"_raw_text": r.text[:5000]}
        return r.status_code, j, elapsed
    except Exception as e:
        return 0, {"_error": str(e), "_error_type": type(e).__name__}, time.time() - t0


def save_response(out_dir: Path, name: str, status: int, body: dict, elapsed: float,
                  url: str, mode: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / f"{_safe(name)}__{mode}.json"
    payload = {
        "url": url,
        "mode": mode,
        "http_status": status,
        "elapsed_sec": round(elapsed, 2),
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "response": body,
    }
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Also save the markdown out separately for easy reading
    md = ((body or {}).get("data") or {}).get("markdown") or ""
    if md:
        (out_dir / f"{_safe(name)}__{mode}.md").write_text(md, encoding="utf-8")
    return fp


def looks_like_captcha(body: dict) -> bool:
    md = ((body or {}).get("data") or {}).get("markdown") or ""
    text = md.lower()
    return any(s in text for s in ("captcha", "请完成", "verify you are human",
                                    "robot verification", "图形验证"))


def has_product_signals(body: dict) -> bool:
    md = ((body or {}).get("data") or {}).get("markdown") or ""
    keywords = ("库存", "价格", "stock", "price", "datasheet", "数据手册",
                "manufacturer", "制造商", "封装", "package", "moq", "最小")
    text = md.lower()
    hits = sum(1 for k in keywords if k in text)
    return hits >= 3 and len(md) > 1000


def phase0_smoke(run_dir: Path, api_key: str, tracker: CreditTracker) -> bool:
    print("=" * 60)
    print("[PHASE 0] Smoke test: firecrawl.dev (markdown)")
    print("=" * 60)
    status, body, elapsed = firecrawl_scrape("https://firecrawl.dev", "markdown", api_key)
    save_response(run_dir / "_smoke", "firecrawl_dev", status, body, elapsed,
                  "https://firecrawl.dev", "markdown")
    if status != 200:
        print(f"  ✗ HTTP {status} after {elapsed:.1f}s — aborting")
        print(f"    body: {json.dumps(body, ensure_ascii=False)[:500]}")
        tracker.fail()
        return False
    md = ((body or {}).get("data") or {}).get("markdown") or ""
    meta = ((body or {}).get("data") or {}).get("metadata") or {}
    delta = tracker.record(meta)
    print(f"  ✓ 200 in {elapsed:.1f}s, markdown={len(md)} chars, +{delta} credit (used={tracker.used})")
    return len(md) > 100


def phase1_bom2buy(run_dir: Path, api_key: str, tracker: CreditTracker) -> dict:
    print("=" * 60)
    print("[PHASE 1] bom2buy.com feasibility (markdown × 4)")
    print("=" * 60)
    results = {}
    for name, url in BOM2BUY_TARGETS:
        if tracker.over_cap():
            print(f"  ! Credit cap exceeded ({tracker.used}); skipping rest of phase 1")
            break
        status, body, elapsed = firecrawl_scrape(url, "markdown", api_key)
        sub = run_dir / "phase1_bom2buy"
        save_response(sub, name, status, body, elapsed, url, "markdown")
        meta = ((body or {}).get("data") or {}).get("metadata") or {}
        delta = tracker.record(meta) if status == 200 else (tracker.fail() or 0)
        md_len = len(((body or {}).get("data") or {}).get("markdown") or "")
        cap = looks_like_captcha(body)
        sig = has_product_signals(body)
        verdict = ("CAPTCHA" if cap else "OK" if sig else "EMPTY/UNCLEAR") if status == 200 else f"HTTP{status}"
        print(f"  • {name:20} {url[:80]}")
        print(f"      → HTTP {status} | md={md_len} chars | +{delta} credit | {verdict}")
        results[name] = {
            "url": url, "http_status": status, "md_len": md_len,
            "captcha": cap, "product_signals": sig, "verdict": verdict,
            "elapsed_sec": elapsed, "credits": delta,
        }
        time.sleep(0.5)
    return results


def phase2a_markdown(run_dir: Path, api_key: str, tracker: CreditTracker,
                     targets: list) -> dict:
    print("=" * 60)
    print(f"[PHASE 2a] Markdown probe — {len(targets)} cells")
    print("=" * 60)
    results = {}
    for source, mpn, url, _scraper_path in targets:
        if tracker.over_cap():
            print(f"  ! Credit cap exceeded ({tracker.used}); skipping rest of phase 2a")
            break
        key = f"{source}_{mpn}"
        status, body, elapsed = firecrawl_scrape(url, "markdown", api_key)
        sub = run_dir / "phase2a_markdown" / source
        save_response(sub, mpn, status, body, elapsed, url, "markdown")
        meta = ((body or {}).get("data") or {}).get("metadata") or {}
        delta = tracker.record(meta) if status == 200 else (tracker.fail() or 0)
        md_len = len(((body or {}).get("data") or {}).get("markdown") or "")
        sig = has_product_signals(body)
        verdict = "OK" if (status == 200 and sig) else (f"HTTP{status}" if status != 200 else "WEAK")
        print(f"  • {source:10} {mpn:25}  HTTP {status} | md={md_len} | +{delta}cr | {verdict}")
        results[key] = {
            "source": source, "mpn": mpn, "url": url, "http_status": status,
            "md_len": md_len, "product_signals": sig, "verdict": verdict,
            "elapsed_sec": elapsed, "credits": delta,
        }
        time.sleep(0.5)
    return results


def phase2b_json(run_dir: Path, api_key: str, tracker: CreditTracker,
                 targets: list) -> dict:
    print("=" * 60)
    print(f"[PHASE 2b] JSON-extract — {len(targets)} cells")
    print("=" * 60)
    results = {}
    for source, mpn, url, _scraper_path in targets:
        if tracker.over_cap():
            print(f"  ! Credit cap exceeded ({tracker.used}); skipping rest of phase 2b")
            break
        key = f"{source}_{mpn}"
        status, body, elapsed = firecrawl_scrape(url, "json", api_key, schema=CANONICAL_SCHEMA)
        sub = run_dir / "phase2b_json" / source
        save_response(sub, mpn, status, body, elapsed, url, "json")
        meta = ((body or {}).get("data") or {}).get("metadata") or {}
        delta = tracker.record(meta) if status == 200 else (tracker.fail() or 0)
        extract = ((body or {}).get("data") or {}).get("json") or {}
        keys_filled = sum(1 for k, v in extract.items() if v not in (None, "", [], {}))
        verdict = "OK" if (status == 200 and keys_filled >= 4) else (f"HTTP{status}" if status != 200 else "EMPTY")
        print(f"  • {source:10} {mpn:25}  HTTP {status} | fields_filled={keys_filled}/13 | +{delta}cr | {verdict}")
        results[key] = {
            "source": source, "mpn": mpn, "url": url, "http_status": status,
            "extract": extract, "keys_filled": keys_filled, "verdict": verdict,
            "elapsed_sec": elapsed, "credits": delta,
        }
        time.sleep(0.5)
    return results


def load_scraper_record(rel_path: str) -> dict:
    fp = SCRAPER_BATCH / rel_path
    if not fp.exists():
        return {}
    try:
        return json.load(open(fp, encoding="utf-8"))
    except Exception:
        return {}


def _short(v, max_len=80):
    if v is None:
        return "—"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    return s if len(s) <= max_len else s[:max_len - 1] + "…"


def render_findings(run_dir: Path, p1: dict, p2a: dict, p2b: dict,
                    tracker: CreditTracker) -> Path:
    lines = []
    ts = datetime.now().isoformat(timespec="seconds")
    lines.append(f"# Firecrawl evaluation findings — {ts}")
    lines.append("")
    lines.append(f"**Run dir:** `{run_dir.relative_to(PROJECT_ROOT)}`")
    lines.append(f"**Plan:** `~/.claude/plans/cozy-singing-hennessy.md`")
    lines.append("")
    lines.append("## Credit usage")
    lines.append("")
    lines.append(f"- Total calls: {tracker.calls}")
    lines.append(f"- Total credits used (per response metadata): **{tracker.used}**")
    lines.append(f"- Failed calls (HTTP != 200): {tracker.failures}")
    lines.append(f"- Cap: {tracker.cap}")
    lines.append("")

    # Phase 1
    lines.append("## Phase 1 — bom2buy.com feasibility")
    lines.append("")
    lines.append("| Target | HTTP | md chars | captcha? | product signals? | verdict |")
    lines.append("|---|---|---|---|---|---|")
    for name, r in p1.items():
        lines.append(f"| {name} | {r['http_status']} | {r['md_len']} | "
                     f"{'yes' if r['captcha'] else 'no'} | "
                     f"{'yes' if r['product_signals'] else 'no'} | {r['verdict']} |")
    lines.append("")
    any_pass = any(r["verdict"] == "OK" for r in p1.values())
    lines.append(f"**Phase 1 verdict:** {'**PASS** — Firecrawl reached bom2buy product content.' if any_pass else '**FAIL** — Firecrawl did not bypass the captcha gate.'}")
    lines.append("")
    # First successful or first response — sample markdown excerpt
    sample = next((r for r in p1.values() if r["verdict"] == "OK"), None) or next(iter(p1.values()), None)
    if sample:
        url = sample["url"]
        fn = next((f for f in (run_dir / "phase1_bom2buy").glob("*__markdown.md")
                   if f.exists()), None)
        if fn and fn.exists():
            md = fn.read_text(encoding="utf-8", errors="ignore")
            excerpt = md[:1500]
            lines.append(f"**Sample markdown from `{url}` (first 1500 chars):**")
            lines.append("")
            lines.append("```markdown")
            lines.append(excerpt)
            lines.append("```")
            lines.append("")

    # Phase 2 — per-cell parity
    lines.append("## Phase 2 — quality parity vs existing scrapers")
    lines.append("")
    fields = ["manufacturer_part_number", "manufacturer", "stock_now_qty",
              "stock_now_ship_text", "stock_future_qty", "stock_breakdown",
              "prices", "datasheet_url", "package", "lifecycle_status",
              "min_order_qty"]
    summary_rows = []
    for source, mpn, url, scraper_path in COMPARISON_TARGETS:
        key = f"{source}_{mpn}"
        scraper_rec = load_scraper_record(scraper_path)
        sc_ex = (scraper_rec or {}).get("extracted") or {}
        fc_ex = (p2b.get(key) or {}).get("extract") or {}
        md_status = (p2a.get(key) or {}).get("verdict", "—")
        json_status = (p2b.get(key) or {}).get("verdict", "—")

        lines.append(f"### {source} × {mpn}")
        lines.append("")
        lines.append(f"- Detail URL: <{url}>")
        lines.append(f"- Scraper JSON: `BatchTest_20260520_07_40_03/{scraper_path}`")
        lines.append(f"- Phase 2a (markdown) verdict: **{md_status}**")
        lines.append(f"- Phase 2b (JSON) verdict: **{json_status}**")
        lines.append("")
        lines.append("| Field | Scraper | Firecrawl | Verdict |")
        lines.append("|---|---|---|---|")
        wins = {"scraper": 0, "firecrawl": 0, "match": 0, "both_empty": 0}
        for f in fields:
            sv = sc_ex.get(f)
            fv = fc_ex.get(f)
            # length-based comparison for lists
            if isinstance(sv, list) or isinstance(fv, list):
                sv_repr = f"len={len(sv) if isinstance(sv, list) else 0}"
                fv_repr = f"len={len(fv) if isinstance(fv, list) else 0}"
                s_len = len(sv) if isinstance(sv, list) else 0
                f_len = len(fv) if isinstance(fv, list) else 0
                if s_len == 0 and f_len == 0:
                    v = "both empty"; wins["both_empty"] += 1
                elif s_len == f_len:
                    v = "match (len)"; wins["match"] += 1
                elif f_len > s_len:
                    v = "firecrawl ↑"; wins["firecrawl"] += 1
                else:
                    v = "scraper ↑"; wins["scraper"] += 1
            else:
                sv_repr = _short(sv)
                fv_repr = _short(fv)
                if (sv in (None, "")) and (fv in (None, "")):
                    v = "both empty"; wins["both_empty"] += 1
                elif sv in (None, ""):
                    v = "firecrawl wins"; wins["firecrawl"] += 1
                elif fv in (None, ""):
                    v = "scraper wins"; wins["scraper"] += 1
                elif str(sv) == str(fv):
                    v = "match"; wins["match"] += 1
                else:
                    v = "differ"
            lines.append(f"| {f} | {sv_repr} | {fv_repr} | {v} |")
        lines.append("")
        lines.append(f"**Tally:** scraper wins {wins['scraper']}, firecrawl wins {wins['firecrawl']}, match {wins['match']}, both empty {wins['both_empty']}")
        lines.append("")
        summary_rows.append((source, mpn, wins))

    # Aggregate summary
    lines.append("## Overall scoreboard (Phase 2)")
    lines.append("")
    lines.append("| Source | MPN | Scraper wins | Firecrawl wins | Match | Both empty |")
    lines.append("|---|---|---|---|---|---|")
    tot = {"scraper": 0, "firecrawl": 0, "match": 0, "both_empty": 0}
    for source, mpn, w in summary_rows:
        lines.append(f"| {source} | {mpn} | {w['scraper']} | {w['firecrawl']} | {w['match']} | {w['both_empty']} |")
        for k in tot:
            tot[k] += w[k]
    lines.append(f"| **TOTAL** | — | **{tot['scraper']}** | **{tot['firecrawl']}** | **{tot['match']}** | **{tot['both_empty']}** |")
    lines.append("")

    # Recommendation
    lines.append("## Recommendation")
    lines.append("")
    fc_share = tot["firecrawl"] / max(tot["firecrawl"] + tot["scraper"] + tot["match"], 1)
    p1_pass = any_pass
    if p1_pass and fc_share > 0.5:
        rec = ("**Integrate.** Firecrawl reached bom2buy AND wins the majority of comparable fields. "
               "Next step: install the `firecrawl_skill.md` artifacts under `.claude/skills/firecrawl/`, "
               "write `scrape_firecrawl_<source>.py` and plumb it into `batch_scraper_test.py` as a 9th channel.")
    elif p1_pass and fc_share <= 0.5:
        rec = ("**Selective adoption.** Firecrawl unlocked bom2buy but did not consistently beat our own scrapers "
               "on the working 5. Use it as the bom2buy driver only; do not replace the working 8.")
    elif (not p1_pass) and fc_share > 0.5:
        rec = ("**Selective adoption / parity replacement.** bom2buy still blocked, but Firecrawl produces "
               "richer data on the working 5. Could replace specific weak scrapers (Rochester / RSONLINE) but "
               "not worth a wholesale migration. Re-test once Firecrawl announces CAPTCHA support.")
    else:
        rec = ("**Shelve for now.** bom2buy still blocked AND no clear quality win on the working sources. "
               "Keep the 1000-call free tier in reserve for ad-hoc tasks.")
    lines.append(rec)
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated by `scraper/scripts/firecrawl_eval.py` at {ts}._")

    fp = run_dir / "findings.md"
    fp.write_text("\n".join(lines), encoding="utf-8")
    return fp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["all", "0", "1", "2a", "2b", "report"],
                    default="all", help="Run a specific phase only")
    ap.add_argument("--run-dir", help="Reuse an existing run dir for --phase report")
    args = ap.parse_args()

    load_dotenv(ENV_PATH)
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        print("ERROR: FIRECRAWL_API_KEY not in api/.env")
        sys.exit(2)

    if args.run_dir:
        run_dir = Path(args.run_dir)
        if not run_dir.is_absolute():
            run_dir = TEST_ROOT / run_dir
    else:
        ts = datetime.now().strftime("%Y%m%d_%H_%M_%S")
        run_dir = TEST_ROOT / f"Firecrawl_Eval_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Run dir: {run_dir.relative_to(PROJECT_ROOT)}")

    tracker = CreditTracker()
    p1, p2a, p2b = {}, {}, {}

    if args.phase in ("all", "0"):
        ok = phase0_smoke(run_dir, api_key, tracker)
        if not ok and args.phase == "all":
            print("Phase 0 failed; aborting.")
            sys.exit(1)

    if args.phase in ("all", "1"):
        p1 = phase1_bom2buy(run_dir, api_key, tracker)
        (run_dir / "phase1_bom2buy_summary.json").write_text(
            json.dumps(p1, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.phase in ("all", "2a"):
        p2a = phase2a_markdown(run_dir, api_key, tracker, COMPARISON_TARGETS)
        (run_dir / "phase2a_markdown_summary.json").write_text(
            json.dumps(p2a, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.phase in ("all", "2b"):
        p2b = phase2b_json(run_dir, api_key, tracker, COMPARISON_TARGETS)
        # Strip the huge response from per-row dicts before saving the summary
        slim = {k: {kk: vv for kk, vv in v.items() if kk != "extract"} | {"keys_filled": v.get("keys_filled", 0)}
                for k, v in p2b.items()}
        (run_dir / "phase2b_json_summary.json").write_text(
            json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        # Save the full extracts to a separate file for the report renderer
        (run_dir / "phase2b_extracts.json").write_text(
            json.dumps({k: v.get("extract", {}) for k, v in p2b.items()},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    # If we ran a single non-all phase, reload prior summaries from disk so the
    # final report can be regenerated.
    if args.phase == "report":
        try:
            p1 = json.loads((run_dir / "phase1_bom2buy_summary.json").read_text(encoding="utf-8"))
        except Exception:
            p1 = {}
        try:
            p2a = json.loads((run_dir / "phase2a_markdown_summary.json").read_text(encoding="utf-8"))
        except Exception:
            p2a = {}
        try:
            slim = json.loads((run_dir / "phase2b_json_summary.json").read_text(encoding="utf-8"))
            extracts = json.loads((run_dir / "phase2b_extracts.json").read_text(encoding="utf-8"))
            p2b = {k: dict(v, extract=extracts.get(k, {})) for k, v in slim.items()}
        except Exception:
            p2b = {}

    if args.phase in ("all", "report"):
        fp = render_findings(run_dir, p1, p2a, p2b, tracker)
        print()
        print(f"Findings written: {fp.relative_to(PROJECT_ROOT)}")

    print()
    print(f"Total calls: {tracker.calls}  credits used (per metadata): {tracker.used}  failed: {tracker.failures}")


if __name__ == "__main__":
    main()
