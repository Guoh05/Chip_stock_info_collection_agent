"""DigiKey (digikey.cn) product scraper.

Strategy (cascade per web-scraper skill):
  1. curl_cffi (chrome131) — KNOWN to fail (Cloudflare 'Just a moment...').
  2. Playwright stealth — passes Cloudflare basic JS challenge automatically.
     - GET search URL: /zh/products/result?keywords=<MPN>
     - If redirected to /zh/products/detail/<mfr>/<MPN>/<id>, parse product page.
     - Otherwise pick first product link in the search results.
     - Parse the Next.js __NEXT_DATA__ blob from the product page for clean JSON.
     - Fall back to DOM scraping if __NEXT_DATA__ is missing.

Output schema (web-scraper skill):
  - method: which stage succeeded
  - data_quality: high|medium|low|none
  - paywall: none
  - attempts: per-method log

Folder convention: test/scraper_test/Test_<MPN>_DIGIKEY_<YYYYMMDD>_<HH>_<MM>_<SS>/
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
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
CHANNEL = "DIGIKEY"
BASE = "https://www.digikey.cn"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


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


CF_CHALLENGE_TITLES = (
    "Just a moment",
    "请稍候",  # zh-CN
    "稍候",
    "Un momento",
    "Veuillez patienter",
)


def is_cf_challenge_title(title: str) -> bool:
    t = title or ""
    return any(s in t for s in CF_CHALLENGE_TITLES)


def looks_like_cf_challenge_html(html: str) -> bool:
    """Robust check: CF challenge HTML is small AND has the CF turnstile script."""
    if not html:
        return True
    if len(html) < 50_000:
        # Small page: check for CF challenge markers
        head = html[:3000]
        if any(m in head for m in CF_CHALLENGE_TITLES):
            return True
        if "challenges.cloudflare.com" in html and "cf_chl" in html:
            return True
    return False


def attempt_curl_cffi(url: str, profile: str = "chrome131") -> dict:
    """Cheap first attempt — KNOWN to fail on Digikey, kept for the cascade log."""
    rec = {"method": "curl_cffi", "profile": profile, "url": url}
    try:
        s = cf_requests.Session(impersonate=profile)
        s.headers.update({
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        r = s.get(url, timeout=20, allow_redirects=True)
        rec["status"] = r.status_code
        rec["len"] = len(r.text)
        if looks_like_cf_challenge_html(r.text):
            rec["outcome"] = "cloudflare_challenge"
        elif r.status_code >= 400:
            rec["outcome"] = "http_error"
        else:
            rec["outcome"] = "unexpected_pass"
            rec["html"] = r.text
    except Exception as exc:
        rec["outcome"] = "exception"
        rec["error"] = str(exc)
    return rec


def attempt_playwright(part: str, out_dir: Path, file_prefix: str | None = None) -> dict:
    """Use Playwright stealth to navigate Digikey and capture data.

    `file_prefix` is the sanitized MPN used to name artefacts on disk; defaults
    to `part` for backwards compatibility, but must be sanitized when the
    original MPN contains path-illegal chars like '/' or ','.
    """
    rec: dict = {"method": "playwright_stealth", "url": None}
    search_url = f"{BASE}/zh/products/result?keywords={part}"
    pfx = file_prefix or part

    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
        ])
        ctx = browser.new_context(
            user_agent=UA,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        page = ctx.new_page()
        try:
            print(f"[digikey] goto {search_url}")
            resp = page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
            rec["initial_status"] = resp.status if resp else None
            rec["initial_url"] = page.url

            # Allow Cloudflare interstitial to resolve and DOM to settle.
            # CF in zh-CN shows "请稍候..." in the title.
            for _ in range(15):
                page.wait_for_timeout(2_000)
                title = page.title()
                if title and not is_cf_challenge_title(title) and len(page.content()) > 50_000:
                    break

            page.wait_for_timeout(2_000)
            title = page.title()
            final_url = page.url
            html = page.content()
            rec["final_url"] = final_url
            rec["title"] = title
            rec["len"] = len(html)

            if looks_like_cf_challenge_html(html) or is_cf_challenge_title(title):
                rec["outcome"] = "cloudflare_persisted"
                (out_dir / f"{pfx}_cf_challenge.html").write_text(html, encoding="utf-8")
                browser.close()
                return rec

            # Save search page
            (out_dir / f"{pfx}_search.html").write_text(html, encoding="utf-8")
            try:
                page.screenshot(path=str(out_dir / f"{pfx}_search.png"), full_page=True)
            except Exception:
                pass

            product_url = None
            if "/products/detail/" in final_url:
                product_url = final_url
                rec["search_redirect_to_detail"] = True
            else:
                # Find first product detail link in results
                hrefs = page.eval_on_selector_all(
                    "a[href*='/products/detail/']",
                    "els => els.map(e => e.getAttribute('href'))",
                )
                seen = set()
                for h in hrefs or []:
                    if h and h not in seen:
                        seen.add(h)
                        if part.lower() in h.lower():
                            product_url = h if h.startswith("http") else f"{BASE}{h}"
                            break
                if not product_url and hrefs:
                    h = hrefs[0]
                    product_url = h if h.startswith("http") else f"{BASE}{h}"

            rec["product_url"] = product_url

            if not product_url:
                rec["outcome"] = "no_product_link"
                browser.close()
                return rec

            print(f"[digikey] goto product {product_url}")
            resp2 = page.goto(product_url, wait_until="domcontentloaded", timeout=60_000)
            rec["product_status"] = resp2.status if resp2 else None
            # Wait for Next.js hydration / CF clear. CF on a fresh product
            # navigation can take 20–30 s; poll up to 30 iterations.
            product_cf_cleared = False
            for _ in range(30):
                page.wait_for_timeout(2_000)
                t = page.title()
                html_len = len(page.content())
                if t and not is_cf_challenge_title(t) and html_len > 50_000:
                    product_cf_cleared = True
                    break
            page.wait_for_timeout(2_000)
            product_html = page.content()
            rec["product_final_url"] = page.url
            rec["product_len"] = len(product_html)
            rec["product_cf_cleared"] = product_cf_cleared
            (out_dir / f"{pfx}_product.html").write_text(product_html, encoding="utf-8")
            try:
                page.screenshot(path=str(out_dir / f"{pfx}_product.png"), full_page=True)
            except Exception:
                pass

            if not product_cf_cleared:
                rec["outcome"] = "cloudflare_persisted_on_product"
                browser.close()
                return rec

            # Capture __NEXT_DATA__ from page context
            next_data = page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    if (!el) return null;
                    try { return JSON.parse(el.textContent); } catch (e) { return null; }
                }
            """)
            if next_data:
                (out_dir / f"{pfx}_next_data.json").write_text(
                    json.dumps(next_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                rec["has_next_data"] = True
            rec["product_html"] = product_html
            rec["next_data"] = next_data
            rec["outcome"] = "ok"
        except Exception as exc:
            rec["outcome"] = "exception"
            rec["error"] = str(exc)
        finally:
            browser.close()
    return rec


def extract_from_next_data(nd: dict) -> dict | None:
    """Pull the Digikey `envelope.data` object from __NEXT_DATA__."""
    try:
        return nd["props"]["pageProps"]["envelope"]["data"]
    except (KeyError, TypeError):
        return None


def _parse_stock_str(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    try:
        return int(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def normalize_next(data: dict) -> dict:
    """Map Digikey envelope.data to stable schema."""
    po = data.get("productOverview") or {}
    pq = data.get("priceQuantity") or {}
    qt = data.get("quantityTable") or []
    attrs_root = data.get("productAttributes") or {}
    attrs = attrs_root.get("attributes") or []
    cats = attrs_root.get("categories") or []

    # Digikey part numbers
    dk_numbers = po.get("digikeyProductNumbers") or {}
    dk_values = dk_numbers.get("value") or []
    digikey_pn = (
        dk_values[0].get("value") if dk_values and isinstance(dk_values[0], dict)
        else po.get("rolledUpProductNumber")
    )

    qty_available = _parse_stock_str(pq.get("qtyAvailable"))
    lead_time = po.get("standardLeadTime")
    is_back_order_allowed = data.get("isBackOrderAllowed")

    # Stock breakdown — only emit fields the page actually shows.
    # Audited 2026-05-19 against digikey.cn product page:
    #   • "DigiKey 美国仓"        — NOT on page (only a CMS tooltip string
    #                               "预计美国仓库发货日期" — not a warehouse name)
    #   • "下单后立即发货"          — NOT on page (only meta-description SEO
    #                               text "立即购买，当天发货")
    #   • "工厂期货"               — NOT on page (the word 工厂 appears only
    #                               in unrelated nav menu category links)
    #   • "原厂标准交货期 N 周"     — REAL (visible in the product info table)
    #
    # Per the "no fabricated labels" rule we now emit:
    #   • A single 现货 row when qty_available > 0, warehouse left blank
    #     (Digikey does not publish a per-warehouse name on the .cn page).
    #   • No 期货 / lead-time row — the row label and the implied "qty=unbounded"
    #     semantics are scraper interpretation, not page truth. The literal
    #     "原厂标准交货期 N 周" string is still preserved in `site_*`/`lead_time`
    #     fields below for downstream that knows what it means; just not
    #     dressed up as a warehouse row here.
    stock_breakdown: list[dict] = []
    if qty_available:
        stock_breakdown.append({
            "label": "现货",
            "warehouse": None,         # page does not name a warehouse
            "quantity": qty_available,
            "ship_text": None,          # page does not name a ship SLA
        })

    out: dict = {
        "digikey_part_number": digikey_pn,
        "digikey_product_id": po.get("rolledUpProductId"),
        "manufacturer_part_number": po.get("manufacturerProductNumber") or po.get("title"),
        "manufacturer": po.get("manufacturer"),
        "manufacturer_url": po.get("manufacturerUrl"),
        "description_en": po.get("description"),
        "detailed_description_cn": po.get("detailedDescription"),
        "datasheet_url": po.get("datasheetUrl"),
        "is_normally_stocking": po.get("isNormallyStocking"),
        "is_back_order_allowed": is_back_order_allowed,
        "lead_time": lead_time,
        "stock_total": qty_available,
        "stock_text": pq.get("qtyAvailable"),
        # Stock breakdown rows — same schema as LCSC for cross-channel uniformity
        "stock_now_qty": qty_available,
        # ship_text fields left blank — see stock_breakdown rationale above.
        # The factory lead time itself is still preserved in `lead_time`
        # above (the raw `standardLeadTime` from Digikey's API).
        "stock_now_ship_text": None,
        "stock_future_qty": None,
        "stock_future_ship_text": None,
        "stock_breakdown": stock_breakdown,
        "has_lead_time": pq.get("hasLeadTime"),
        "min_order_qty": (pq.get("pricing") or [{}])[0].get("minOrderQuantity") if pq.get("pricing") else None,
        "min_order_multiplier": data.get("minimumMultiplier"),
        "packaging": (pq.get("pricing") or [{}])[0].get("packaging") if pq.get("pricing") else None,
        "package": None,
        "lifecycle_status": None,
        "categories": [{"id": c.get("id"), "label": c.get("label"), "url": c.get("url")} for c in cats],
        "prices": [],
        "prices_float": [],
        "parameters": [],
    }

    # Price tiers — preferred: priceQuantity.pricing[0].mergedPricingTiers (string format with currency)
    pricing_list = pq.get("pricing") or []
    if pricing_list:
        for tier in pricing_list[0].get("mergedPricingTiers") or []:
            out["prices"].append({
                "min_qty": _parse_stock_str(tier.get("brkQty")),
                "unit_price": tier.get("unitPrice"),
                "ext_price": tier.get("extPrice"),
            })

    # Float-format tiers from quantityTable (more granular per-packaging)
    for tier in qt or []:
        if not isinstance(tier, dict):
            continue
        out["prices_float"].append({
            "min_qty": tier.get("breakQty"),
            "unit_price": tier.get("unitPrice"),
            "packaging": tier.get("packaging"),
            "digikey_part_number": tier.get("digikeyProductNumber"),
        })

    # Parameters (flatten 'label': 'values[].value')
    for a in attrs:
        if not isinstance(a, dict):
            continue
        label = a.get("label")
        vals = a.get("values") or []
        if isinstance(vals, list):
            value_strs = []
            for v in vals:
                if isinstance(v, dict):
                    val = v.get("value")
                    if val is not None:
                        value_strs.append(str(val))
                elif v is not None:
                    value_strs.append(str(v))
            value = " / ".join(value_strs) if value_strs else None
        else:
            value = str(vals)
        if label and value:
            out["parameters"].append({"name": label, "value": value})
            # Common-field promotions
            llow = label.lower()
            if not out["package"] and ("封装" in label or "package" in llow or "供应商器件封装" in label):
                out["package"] = value
            if not out["lifecycle_status"] and ("零件状态" in label or "part status" in llow or "lifecycle" in llow):
                out["lifecycle_status"] = value

    return out


def extract_from_html(html: str) -> dict:
    """Fallback DOM extractor for Digikey product pages."""
    soup = BeautifulSoup(html, "lxml")
    out = {
        "page_title": soup.title.get_text(strip=True) if soup.title else None,
        "manufacturer_part_number": None,
        "manufacturer": None,
        "description_en": None,
        "stock_total": None,
        "datasheet_url": None,
        "prices": [],
        "parameters": [],
    }
    # Product description meta
    for tag in soup.select('[data-testid*="manufacturer-product-number"]'):
        t = tag.get_text(strip=True)
        if t:
            out["manufacturer_part_number"] = t
            break
    for tag in soup.select('[data-testid*="manufacturer-name"], a[data-testid*="manufacturer"]'):
        t = tag.get_text(strip=True)
        if t:
            out["manufacturer"] = t
            break
    # Datasheet
    for a in soup.find_all("a", href=True):
        if "datasheet" in (a.get_text() or "").lower() or "datasheet" in a["href"].lower():
            if a["href"].lower().endswith(".pdf"):
                out["datasheet_url"] = a["href"]
                break
    return out


def assess_quality(ex: dict) -> str:
    has_part = bool(ex.get("manufacturer_part_number"))
    has_price = bool(ex.get("prices"))
    has_stock = ex.get("stock_total") is not None
    has_params = bool(ex.get("parameters"))
    if has_part and has_price and has_stock and has_params:
        return "high"
    if has_part and (has_price or has_stock):
        return "medium"
    if has_part:
        return "low"
    return "none"


def scrape(part: str, out_dir: Path) -> dict:
    record: dict = {
        "query": part,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "digikey.cn",
        "search_url": f"{BASE}/zh/products/result?keywords={part}",
        "output_dir": str(out_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    # 1. curl_cffi (cheap probe, expected to fail)
    cf_rec = attempt_curl_cffi(record["search_url"])
    cf_rec.pop("html", None)
    record["attempts"].append(cf_rec)
    print(f"[digikey] curl_cffi: {cf_rec.get('outcome')} (status={cf_rec.get('status')})")

    # 2. Playwright stealth — sanitize file prefix for path-safe artefacts
    safe_part = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    pw_rec = attempt_playwright(part, out_dir, file_prefix=safe_part)
    product_html = pw_rec.pop("product_html", None)
    next_data = pw_rec.pop("next_data", None)
    record["attempts"].append({k: v for k, v in pw_rec.items() if k != "product_html"})
    print(f"[digikey] playwright: {pw_rec.get('outcome')}")

    if pw_rec.get("outcome") != "ok":
        cf_outcomes = ("cloudflare_persisted", "cloudflare_persisted_on_product")
        record["status"] = "blocked" if pw_rec.get("outcome") in cf_outcomes else "failed"
        if pw_rec.get("outcome") in cf_outcomes:
            record["blocker"] = "cloudflare_just_a_moment"
        return record

    record["resolved_product_url"] = pw_rec.get("product_final_url")
    record["method"] = "playwright_stealth"

    extracted = None
    if next_data:
        envelope_data = extract_from_next_data(next_data)
        if envelope_data:
            extracted = normalize_next(envelope_data)
            extracted["_source"] = "__NEXT_DATA__"
    if not extracted and product_html:
        extracted = extract_from_html(product_html)
        extracted["_source"] = "dom"

    if not extracted:
        record["status"] = "ok_no_data"
        return record

    record["status"] = "ok"
    record["extracted"] = extracted
    record["data_quality"] = assess_quality(extracted)
    return record


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "STM32G030F6P6"
    out_dir_override = argv[2] if len(argv) > 2 else None
    if out_dir_override:
        run_dir = Path(out_dir_override).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = make_run_dir(part)
    print(f"=== DIGIKEY scrape: {part} ===")
    print(f"output folder: {run_dir}")

    safe_part = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    rec = scrape(part, run_dir)
    out = run_dir / f"{safe_part}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = write_summary(rec, run_dir, safe_part)

    print(f"\nWrote {out}")
    print(f"Wrote {summary}")
    print(f"status: {rec.get('status')}  method: {rec.get('method')}  quality: {rec.get('data_quality')}")
    ex = rec.get("extracted") or {}
    if ex:
        print("\nKey fields:")
        for k in (
            "digikey_part_number", "digikey_product_id",
            "manufacturer_part_number", "manufacturer",
            "description_en", "detailed_description_cn",
            "stock_total", "stock_text", "lead_time",
            "lifecycle_status", "package",
            "is_normally_stocking", "min_order_qty",
            "min_order_multiplier", "packaging",
            "datasheet_url",
        ):
            if ex.get(k) is not None:
                print(f"  {k}: {ex.get(k)}")
        print(f"  prices: {len(ex.get('prices') or [])} tiers (string format)")
        for tier in (ex.get("prices") or [])[:10]:
            print(f"    @{tier.get('min_qty')}+ -> {tier.get('unit_price')}  ext={tier.get('ext_price')}")
        print(f"  prices_float: {len(ex.get('prices_float') or [])} tiers (numeric)")
        print(f"  parameters: {len(ex.get('parameters') or [])} attributes")
        for prm in (ex.get("parameters") or [])[:12]:
            print(f"    {prm.get('name')}: {prm.get('value')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
