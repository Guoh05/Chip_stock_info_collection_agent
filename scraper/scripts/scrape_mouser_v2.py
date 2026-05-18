"""Mouser scraper v2 — curl_cffi-first cascade per project_scrape_state.md plan.

Cascade:
  1. curl_cffi (chrome131) static fetch of canonical /ProductDetail/<mfr>/<MPN>
  2. curl_cffi with homepage warm-up (seed ak_bmsc cookie) + retry product page
  3. curl_cffi with longer wait then refetch (Akamai bm-verify meta-refresh)
  4. (Optional) Playwright stealth with persistent profile — known to be blocked
     by DataDome/Akamai BMP per memory. Off by default; pass --playwright to try.

Every attempt is logged in record["attempts"]; final method written to
record["method"]. Output schema matches the web-scraper skill conventions:
method, data_quality, paywall, attempts.

Usage:
  .venv/Scripts/python.exe scripts/scrape_mouser_v2.py STM32G030F6P6 STMicroelectronics
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
CHANNEL = "MOUSER"
BASE = "https://www.mouser.cn"
BASE_COM = "https://www.mouser.com"


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


def looks_like_real_page(html: str) -> bool:
    """Heuristic: a real Mouser product page has rich content, no bm-verify."""
    if not html or len(html) < 20_000:
        return False
    if "bm-verify" in html:
        return False
    if "captcha-delivery.com" in html:
        return False
    if 'id="cmsg"' in html:
        return False
    # Real product page has Schema.org or pricing markers
    return any(
        marker in html
        for marker in (
            'application/ld+json',
            'spnManufacturerPartNumber',
            'pdp-pricing',
            'PartNumberOrder',
        )
    )


def follow_bm_verify(session, html: str, current_url: str, timeout: int = 30):
    """If page contains an Akamai bm-verify meta refresh, wait and follow it."""
    m = re.search(
        r"meta http-equiv=.refresh. content=.(\d+);\s*URL=.([^'\">]+)",
        html, re.IGNORECASE,
    )
    if not m:
        return None
    delay = int(m.group(1))
    next_url = m.group(2).replace("&amp;", "&")
    if not next_url.startswith("http"):
        next_url = BASE + next_url
    time.sleep(delay + 0.5)
    try:
        r = session.get(next_url, timeout=timeout, allow_redirects=True,
                        headers={"Referer": current_url})
        return r
    except Exception:
        return None


def attempt_curl_cffi(direct_url: str, label: str, profile: str = "chrome131",
                      warmup_url: str | None = None, lang: str = "zh") -> dict:
    headers_zh = {
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    headers_en = {
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    s = cf_requests.Session(impersonate=profile)
    s.headers.update(headers_zh if lang == "zh" else headers_en)
    attempt = {"method": label, "profile": profile, "url": direct_url}

    try:
        if warmup_url:
            w = s.get(warmup_url, timeout=20, allow_redirects=True)
            attempt["warmup_status"] = w.status_code
            attempt["warmup_cookies"] = list(s.cookies.keys())
            time.sleep(1)

        r = s.get(direct_url, timeout=30, allow_redirects=True,
                  headers={"Referer": warmup_url} if warmup_url else None)
        attempt["status"] = r.status_code
        attempt["final_url"] = r.url
        attempt["len"] = len(r.text)

        if looks_like_real_page(r.text):
            attempt["outcome"] = "real_page"
            attempt["html"] = r.text
            return attempt

        if "bm-verify" in r.text:
            attempt["outcome"] = "bm_verify_challenge"
            r2 = follow_bm_verify(s, r.text, direct_url)
            if r2 is not None:
                attempt["followup_status"] = r2.status_code
                attempt["followup_len"] = len(r2.text)
                if looks_like_real_page(r2.text):
                    attempt["outcome"] = "real_page_after_bmverify"
                    attempt["html"] = r2.text
                    return attempt
                if "bm-verify" in r2.text:
                    attempt["outcome"] = "bm_verify_loop"
        elif "captcha-delivery.com" in r.text:
            attempt["outcome"] = "datadome_captcha"
        else:
            attempt["outcome"] = "short_or_unknown"
            attempt["html_head"] = r.text[:500]
    except Exception as exc:
        attempt["outcome"] = "exception"
        attempt["error"] = str(exc)

    return attempt


def extract_from_html(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out = {
        "page_title": (soup.title.get_text(strip=True) if soup.title else None),
        "mouser_part_number": None,
        "manufacturer_part_number": None,
        "manufacturer": None,
        "description_en": None,
        "stock": None,
        "datasheet_url": None,
        "prices": [],
        "parameters": [],
        "image_urls": [],
    }

    el = soup.select_one("#spnMouserPartNumFormattedForProdInfo")
    if el:
        out["mouser_part_number"] = el.get_text(strip=True)
    el = soup.select_one(
        "#spnManufacturerPartNumber, h1#spnManufacturerPartNumberFormattedForProdInfo"
    )
    if el:
        out["manufacturer_part_number"] = el.get_text(strip=True)
    el = soup.select_one("a#lnkManufacturerName, span#spnManufacturerName")
    if el:
        out["manufacturer"] = el.get_text(strip=True)
    el = soup.select_one("span#spnDescription, meta[name='description']")
    if el:
        out["description_en"] = (
            el.get("content") if el.name == "meta" else el.get_text(strip=True)
        )

    # JSON-LD blocks
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
                out["description_en"] = (
                    item.get("description") or out["description_en"]
                )
                offers = item.get("offers")
                if offers:
                    for off in offers if isinstance(offers, list) else [offers]:
                        if not isinstance(off, dict):
                            continue
                        price = off.get("price") or off.get("lowPrice")
                        if price is not None:
                            out["prices"].append({
                                "unit_price": price,
                                "currency": off.get("priceCurrency"),
                            })

    # Price tier table
    for table in soup.find_all("table"):
        cls = " ".join(table.get("class") or []).lower()
        if "price" in cls or "pricing" in cls:
            for row in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
                if len(cells) >= 2:
                    qty_m = re.search(r"(\d[\d,]*)", cells[0])
                    if qty_m:
                        out["prices"].append({
                            "min_qty": int(qty_m.group(1).replace(",", "")),
                            "unit_price_text": cells[1],
                        })

    # Datasheet
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "datasheet" in href.lower() and href.lower().endswith(".pdf"):
            out["datasheet_url"] = href if href.startswith("http") else f"{BASE}{href}"
            break

    return out


def assess_quality(extracted: dict) -> str:
    has_part = bool(extracted.get("manufacturer_part_number"))
    has_price = bool(extracted.get("prices"))
    has_stock = extracted.get("stock") is not None
    if has_part and has_price and has_stock:
        return "high"
    if has_part and (has_price or has_stock):
        return "medium"
    if has_part:
        return "low"
    return "none"


def scrape(part: str, mfr: str, out_dir: Path) -> dict:
    direct_cn = f"{BASE}/ProductDetail/{mfr}/{part}"
    direct_com = f"{BASE_COM}/ProductDetail/{mfr}/{part}"
    search_cn = f"{BASE}/c/?q={part}"

    record: dict = {
        "query": part,
        "manufacturer_query": mfr,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "mouser.cn / mouser.com",
        "direct_url_cn": direct_cn,
        "direct_url_com": direct_com,
        "search_url_cn": search_cn,
        "output_dir": str(out_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    cascade = [
        ("curl_cffi_chrome131_cn", direct_cn, "chrome131", None, "zh"),
        ("curl_cffi_chrome131_cn_warmup", direct_cn, "chrome131", BASE + "/", "zh"),
        ("curl_cffi_chrome146_cn_warmup", direct_cn, "chrome146", BASE + "/", "zh"),
        ("curl_cffi_chrome131_com", direct_com, "chrome131", None, "en"),
        ("curl_cffi_chrome131_com_warmup", direct_com, "chrome131", BASE_COM + "/", "en"),
        ("curl_cffi_safari260_cn", direct_cn, "safari260", BASE + "/", "zh"),
    ]

    success_html = None
    for label, url, profile, warmup, lang in cascade:
        print(f"[mouser] attempt {label} ({profile}) -> {url}")
        att = attempt_curl_cffi(url, label, profile, warmup, lang)
        # Keep html out of the recorded attempt list (too large)
        html = att.pop("html", None)
        record["attempts"].append(att)
        print(f"  -> outcome={att.get('outcome')} status={att.get('status')} len={att.get('len')}")
        if html and att.get("outcome") in ("real_page", "real_page_after_bmverify"):
            success_html = html
            record["method"] = label
            break
        # Light backoff between attempts to avoid getting fingerprinted as a burst
        time.sleep(2)

    if not success_html:
        # Save bm-verify body for forensic inspection
        last_attempts = [a for a in record["attempts"] if a.get("html_head") or a.get("outcome", "").startswith("bm_verify")]
        (out_dir / f"{part}_attempt_summary.json").write_text(
            json.dumps(record["attempts"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record["status"] = "blocked"
        record["blocker"] = "akamai_bot_manager_bm_verify"
        record["data_quality"] = "none"
        return record

    # Save artefacts
    (out_dir / f"{part}_product.html").write_text(success_html, encoding="utf-8")
    extracted = extract_from_html(success_html)
    record["status"] = "ok"
    record["extracted"] = extracted
    record["data_quality"] = assess_quality(extracted)
    return record


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "STM32G030F6P6"
    mfr = argv[2] if len(argv) > 2 else "STMicroelectronics"
    run_dir = make_run_dir(part)
    print(f"=== MOUSER scrape v2: {part} (mfr={mfr}) ===")
    print(f"output folder: {run_dir}")

    rec = scrape(part, mfr, run_dir)
    out = run_dir / f"{part}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = write_summary(rec, run_dir, part)

    print()
    print(f"Wrote {out}")
    print(f"Wrote {summary}")
    print(f"status: {rec.get('status')}  method: {rec.get('method')}  quality: {rec.get('data_quality')}")
    if rec.get("status") == "blocked":
        print(f"blocker: {rec.get('blocker')}")
        print("Attempts:")
        for a in rec["attempts"]:
            print(f"  - {a.get('method')}: {a.get('outcome')} (status={a.get('status')}, len={a.get('len')})")
    ex = rec.get("extracted") or {}
    for k in ("mouser_part_number", "manufacturer_part_number", "manufacturer",
              "description_en", "stock", "datasheet_url"):
        if ex.get(k) is not None:
            print(f"  {k}: {ex.get(k)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
