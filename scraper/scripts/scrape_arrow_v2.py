"""Arrow scraper v2 — curl_cffi-first cascade per project_scrape_state.md plan.

Cascade:
  1. curl_cffi (chrome131) static fetch of canonical product URL
  2. curl_cffi homepage warm-up + retry product URL
  3. curl_cffi try /en/ variant + retry
  4. curl_cffi try search URL

Arrow is fronted by Akamai BotManager: product detail paths return 403 with the
Akamai sensor script. Root SPA paths return a 404 + SPA shell that does not
contain product data. Bypassing this requires running JS to compute the _abck
sensor token, which curl_cffi cannot do.

Per memory: Playwright is also blocked (HTTP/2 fingerprint rejection on
arrow.com, and china.arrow.com/arrow.com.cn fail at TLS). Off by default.

Output schema follows the web-scraper skill: method, data_quality, paywall,
attempts.

Usage:
  .venv/Scripts/python.exe scripts/scrape_arrow_v2.py STM32G030F6P6 stmicroelectronics
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
CHANNEL = "ARROW"
BASE = "https://www.arrow.com"


def make_run_dir(part: str, channel: str = CHANNEL) -> Path:
    now = datetime.now()
    safe_part = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    safe_channel = re.sub(r"[^A-Za-z0-9_-]", "_", channel).upper()
    name = (
        f"Test_{safe_part}_{safe_channel}_"
        f"{now.strftime('%Y%m%d')}_{now.strftime('%H_%M_%S')}"
    )
    run_dir = TEST_ROOT / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def is_real_arrow_product_page(html: str) -> bool:
    if not html or len(html) < 30_000:
        return False
    # Akamai sensor pages are tiny
    if "<title>Access Denied</title>" in html:
        return False
    # Real product page contains JSON-LD Product or specific data markers
    return any(m in html for m in (
        '"@type":"Product"',
        '"@type": "Product"',
        'data-component="product-detail"',
        'productPartNumberDisplay',
        'inventoryLineItem',
    ))


def attempt(url: str, label: str, profile: str, warmup_urls: list[str] | None = None,
            lang: str = "zh") -> dict:
    headers = {
        "Accept-Language": (
            "zh-CN,zh;q=0.9,en;q=0.8" if lang == "zh"
            else "en-US,en;q=0.9"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    s = cf_requests.Session(impersonate=profile)
    s.headers.update(headers)
    rec = {"method": label, "profile": profile, "url": url}
    try:
        if warmup_urls:
            warmup_log = []
            for wu in warmup_urls:
                w = s.get(wu, timeout=20, allow_redirects=True)
                warmup_log.append({"url": wu, "status": w.status_code, "len": len(w.text)})
                time.sleep(1)
            rec["warmups"] = warmup_log
            rec["cookies_after_warmup"] = list(s.cookies.keys())
        r = s.get(url, timeout=30, allow_redirects=True,
                  headers={"Referer": warmup_urls[-1]} if warmup_urls else None)
        rec["status"] = r.status_code
        rec["final_url"] = r.url
        rec["len"] = len(r.text)
        if r.status_code == 403:
            rec["outcome"] = "akamai_403"
        elif is_real_arrow_product_page(r.text):
            rec["outcome"] = "real_page"
            rec["html"] = r.text
        elif r.status_code == 404:
            rec["outcome"] = "404_spa_shell"
        else:
            rec["outcome"] = "unknown_response"
            rec["body_head"] = r.text[:300]
    except Exception as exc:
        rec["outcome"] = "exception"
        rec["error"] = str(exc)
    return rec


def extract_from_html(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out = {
        "page_title": (soup.title.get_text(strip=True) if soup.title else None),
        "manufacturer_part_number": None,
        "manufacturer": None,
        "description_en": None,
        "stock": None,
        "datasheet_url": None,
        "prices": [],
        "parameters": [],
    }
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else data
        for item in items if isinstance(items, list) else [items]:
            if not isinstance(item, dict):
                continue
            t = item.get("@type", "")
            if "Product" in (t if isinstance(t, list) else [t]):
                out["manufacturer_part_number"] = (
                    item.get("mpn") or item.get("model")
                    or out["manufacturer_part_number"]
                )
                brand = item.get("brand")
                if isinstance(brand, dict):
                    out["manufacturer"] = brand.get("name") or out["manufacturer"]
                out["description_en"] = item.get("description") or out["description_en"]
                offers = item.get("offers")
                if offers:
                    for off in offers if isinstance(offers, list) else [offers]:
                        if isinstance(off, dict) and off.get("price") is not None:
                            out["prices"].append({
                                "unit_price": off.get("price"),
                                "currency": off.get("priceCurrency"),
                            })
    return out


def assess_quality(ex: dict) -> str:
    if ex.get("manufacturer_part_number") and ex.get("prices") and ex.get("stock") is not None:
        return "high"
    if ex.get("manufacturer_part_number") and (ex.get("prices") or ex.get("stock") is not None):
        return "medium"
    if ex.get("manufacturer_part_number"):
        return "low"
    return "none"


def scrape(part: str, mfr: str, out_dir: Path) -> dict:
    direct_zh = f"{BASE}/zh/products/{part.lower()}/{mfr.lower()}"
    direct_en = f"{BASE}/en/products/{part.lower()}/{mfr.lower()}"
    search_zh = f"{BASE}/zh/products/search?q={part}"
    search_en = f"{BASE}/en/products/search?q={part}"

    record: dict = {
        "query": part,
        "manufacturer_query": mfr,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "arrow.com",
        "direct_url_zh": direct_zh,
        "direct_url_en": direct_en,
        "output_dir": str(out_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    cascade = [
        ("curl_cffi_chrome131_direct_zh", direct_zh, "chrome131", None, "zh"),
        ("curl_cffi_chrome131_direct_zh_warmup", direct_zh, "chrome131",
         [BASE + "/zh/"], "zh"),
        ("curl_cffi_chrome131_direct_en_warmup", direct_en, "chrome131",
         [BASE + "/en/"], "en"),
        ("curl_cffi_chrome131_search_zh", search_zh, "chrome131", [BASE + "/zh/"], "zh"),
        ("curl_cffi_chrome131_search_en", search_en, "chrome131", [BASE + "/en/"], "en"),
        ("curl_cffi_safari260_direct_zh", direct_zh, "safari260", [BASE + "/zh/"], "zh"),
    ]

    success_html = None
    for label, url, profile, warmups, lang in cascade:
        print(f"[arrow] attempt {label} -> {url}")
        att = attempt(url, label, profile, warmups, lang)
        html = att.pop("html", None)
        record["attempts"].append(att)
        print(f"  -> outcome={att.get('outcome')} status={att.get('status')} len={att.get('len')}")
        if html and att["outcome"] == "real_page":
            success_html = html
            record["method"] = label
            break
        time.sleep(2)

    if not success_html:
        record["status"] = "blocked"
        record["blocker"] = "akamai_botmanager_abck_sensor"
        return record

    (out_dir / f"{part}_product.html").write_text(success_html, encoding="utf-8")
    extracted = extract_from_html(success_html)
    record["status"] = "ok"
    record["extracted"] = extracted
    record["data_quality"] = assess_quality(extracted)
    return record


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "STM32G030F6P6"
    mfr = argv[2] if len(argv) > 2 else "stmicroelectronics"
    run_dir = make_run_dir(part)
    print(f"=== ARROW scrape v2: {part} (mfr={mfr}) ===")
    print(f"output folder: {run_dir}")

    rec = scrape(part, mfr, run_dir)
    out = run_dir / f"{part}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = write_summary(rec, run_dir, part)

    print(f"\nWrote {out}")
    print(f"Wrote {summary}")
    print(f"status: {rec.get('status')}  method: {rec.get('method')}  quality: {rec.get('data_quality')}")
    if rec.get("status") == "blocked":
        print(f"blocker: {rec.get('blocker')}")
        for a in rec["attempts"]:
            print(f"  - {a.get('method')}: {a.get('outcome')} (status={a.get('status')}, len={a.get('len')})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
