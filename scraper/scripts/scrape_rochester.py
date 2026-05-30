"""Rochester Electronics (rocelec.com) product scraper.

Salesforce Lightning B2B Commerce site (LWC). Requires Playwright Firefox to
clear Akamai HTTP/2 fingerprint check + homepage warmup so the LWC bundle
loads. Detail URL pattern: `/part/<SalesforceID>-<MPN_no_punct>`.

Strategy:
  1. Playwright Firefox visit homepage; wait for LWC.
  2. Navigate `https://www.rocelec.com/global-search/<MPN>`; poll for hydration
     (len(html) > 440k and MPN appears in body, OR the no-results message
     "Sorry, we couldn't find any results" is present).
  3. If results: click the first `<span class="productName">` matching the
     input MPN (or first row when no exact match). The click navigates to
     `/part/<sf_id>-<mpn_no_punct>` via in-page LWC routing.
  4. On detail page: scrape the spec table, price tier rows, stock indicator,
     and datasheet link.

Catalog caveat: Rochester only stocks EOL / authorized-secondary supply parts.
Most active-production MPNs in our 103-chip master list return "no results" —
that is the source's real catalog scope, not a scraper failure.

Folder layout: test/scraper/Test_<MPN>_ROCHESTER_<YYYYMMDD>_<HH>_<MM>_<SS>/
  <MPN>.json, <MPN>_summary.md
  <MPN>_search.html, <MPN>_search.png
  <MPN>_product.html, <MPN>_product.png
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper"
CHANNEL = "ROCHESTER"
BASE = "https://www.rocelec.com"

UA_FF = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)


def make_run_dir(part: str, channel: str = CHANNEL) -> Path:
    now = datetime.now()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    name = f"Test_{safe}_{channel}_{now.strftime('%Y%m%d')}_{now.strftime('%H_%M_%S')}"
    run_dir = TEST_ROOT / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _parse_int(s) -> int | None:
    if s is None:
        return None
    txt = re.sub(r"[^\d]", "", str(s))
    try:
        return int(txt) if txt else None
    except ValueError:
        return None


def _parse_float(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    txt = re.sub(r"[^\d.,\-]", "", str(s))
    if not txt:
        return None
    txt = txt.replace(",", "")
    try:
        return float(txt)
    except ValueError:
        return None


SPEC_KEYS = [
    "Manufacturer OPN", "Manufacturer Order Code", "Generic PN",
    "Manufacturer Life Cycle", "Package Type", "Package Pin Count",
    "RoHS Compliance", "Lead Free", "Packaging Type", "Packaging Quantity",
    "Technology Category", "Technology Subcategory", "Technology Group",
    "US HTS Code", "ECCN", "Manufacturer",
]


def extract_from_detail_html(html: str, mpn: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    extracted: dict = {
        "manufacturer_part_number": None,
        "manufacturer": None,
        "generic_pn": None,
        "manufacturer_order_code": None,
        "description_en": None,
        "package": None,
        "package_pin_count": None,
        "packaging_type": None,
        "packaging_option": None,  # unified cross-source field — mirrors packaging_type when present
        "packaging_quantity": None,
        "lifecycle_status": None,
        "rohs_compliance": None,
        "lead_free": None,
        "technology_category": None,
        "technology_subcategory": None,
        "technology_group": None,
        "us_hts_code": None,
        "eccn": None,
        "stock_total": None,
        "stock_now_qty": None,
        "stock_now_ship_text": None,
        "stock_future_qty": None,
        "stock_future_ship_text": None,
        "stock_breakdown": [],
        "datasheet_url": None,
        "image_url": None,
        "prices": [],
        "parameters": [],
        "currency": "USD",
    }

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    extracted["page_title"] = title
    # Title format: "<MPN> | <Manufacturer>"
    if "|" in title:
        parts = [p.strip() for p in title.split("|", 1)]
        if len(parts) == 2:
            extracted["manufacturer_part_number"] = parts[0]
            extracted["manufacturer"] = parts[1]

    text_body = re.sub(r"<[^>]+>", " ", html)
    text_body = re.sub(r"\s+", " ", text_body)

    # Spec rows — "Label Value" pattern (no fixed tags; LWC renders as plain text)
    for key in SPEC_KEYS:
        other_keys = [re.escape(k) for k in SPEC_KEYS if k != key]
        pat = (
            re.escape(key)
            + r"\s+([A-Za-z0-9.,\-/()\s]+?)"
            + r"(?=\s+(?:" + "|".join(other_keys) + r")\s|\Z)"
        )
        m = re.search(pat, text_body)
        if m:
            val = m.group(1).strip()[:120]
            # Slot canonical fields
            if key == "Manufacturer" and not extracted["manufacturer"]:
                extracted["manufacturer"] = val
            elif key == "Manufacturer OPN" and not extracted["manufacturer_part_number"]:
                extracted["manufacturer_part_number"] = val
            elif key == "Manufacturer Order Code":
                extracted["manufacturer_order_code"] = val
            elif key == "Generic PN":
                extracted["generic_pn"] = val
            elif key == "Manufacturer Life Cycle":
                extracted["lifecycle_status"] = val
            elif key == "Package Type":
                extracted["package"] = val
            elif key == "Package Pin Count":
                extracted["package_pin_count"] = _parse_int(val)
            elif key == "Packaging Type":
                extracted["packaging_type"] = val
                extracted["packaging_option"] = val  # unified cross-source field
            elif key == "Packaging Quantity":
                extracted["packaging_quantity"] = _parse_int(val)
            elif key == "RoHS Compliance":
                extracted["rohs_compliance"] = (val.lower() == "yes")
            elif key == "Lead Free":
                extracted["lead_free"] = (val.lower() == "yes")
            elif key == "Technology Category":
                extracted["technology_category"] = val
            elif key == "Technology Subcategory":
                extracted["technology_subcategory"] = val
            elif key == "Technology Group":
                extracted["technology_group"] = val
            elif key == "US HTS Code":
                extracted["us_hts_code"] = val
            elif key == "ECCN":
                extracted["eccn"] = val
            extracted["parameters"].append({"name": key, "value": val})

    # Description from search-results-style markup (productDescription class)
    m = re.search(r'class="productDescription"[^>]*>([^<]+)<', html)
    if m:
        extracted["description_en"] = m.group(1).strip()[:300]

    # Stock — "In Stock: N,NNN"
    stock_m = re.search(r"In Stock\s*[:：]?\s*([\d,]+)", text_body)
    if stock_m:
        qty = _parse_int(stock_m.group(1))
        if qty is not None and qty > 0:
            extracted["stock_now_qty"] = qty
            extracted["stock_total"] = qty
            extracted["stock_now_ship_text"] = "In Stock at Rochester Electronics warehouse"
    elif "In Stock" in text_body:
        # In Stock but no qty visible
        extracted["stock_now_qty"] = None
        extracted["stock_now_ship_text"] = "In Stock at Rochester Electronics warehouse"

    # Price tiers — qty-range strings (100-499, 100000+) paired with $price values
    seen_tiers: set = set()
    for m in re.finditer(
        r"(\d{1,7}[\-,]\d{1,7}|\d{2,7}\+)\s*</[a-z]+>\s*"
        r"<[^>]*>\s*\$\s*([\d.]+)\s*</",
        html,
    ):
        qty_str, price_str = m.group(1), m.group(2)
        # parse qty as min_qty
        if qty_str.endswith("+"):
            min_qty = _parse_int(qty_str.rstrip("+"))
            max_qty = None
        else:
            parts = re.split(r"[-,]", qty_str)
            min_qty = _parse_int(parts[0]) if parts else None
            max_qty = _parse_int(parts[1]) if len(parts) > 1 else None
        price = _parse_float(price_str)
        if min_qty is None or price is None:
            continue
        if (min_qty, price) in seen_tiers:
            continue
        seen_tiers.add((min_qty, price))
        tier = {
            "min_qty": min_qty,
            "unit_price": f"${price}",
            "unit_price_float": price,
            "currency": "USD",
        }
        if max_qty is not None:
            tier["max_qty"] = max_qty
        extracted["prices"].append(tier)
    extracted["prices"].sort(key=lambda t: t["min_qty"])

    # Datasheet anchor
    for a in soup.find_all("a", href=True):
        href = a["href"]
        txt = a.get_text(" ", strip=True).lower()
        if "datasheet" in txt and (".pdf" in href.lower() or "widen.net" in href):
            extracted["datasheet_url"] = href
            break

    # Stock breakdown — single row (Rochester sells from own inventory)
    if extracted["stock_now_qty"] or extracted["stock_now_ship_text"]:
        extracted["stock_breakdown"].append({
            "label": "In Stock",
            "warehouse": "Rochester Electronics",
            "quantity": extracted["stock_now_qty"],
            "ship_text": extracted["stock_now_ship_text"] or "In Stock",
        })

    return extracted


def quality_for(ex: dict) -> str:
    has_mpn = bool(ex.get("manufacturer_part_number"))
    has_mfr = bool(ex.get("manufacturer"))
    has_stock = ex.get("stock_now_qty") is not None
    has_price = bool(ex.get("prices"))
    has_params = bool(ex.get("parameters")) and len(ex["parameters"]) >= 4
    if has_mpn and has_mfr and has_stock and has_price and has_params:
        return "high"
    if has_mpn and has_mfr and (has_stock or has_price):
        return "medium"
    if has_mpn:
        return "low"
    return "none"


def scrape(part: str, run_dir: Path) -> dict:
    safe = _safe(part)
    search_url = f"{BASE}/global-search/{part}"
    record: dict = {
        "query": part,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "rocelec.com",
        "search_url": search_url,
        "output_dir": str(run_dir),
        "method": "playwright_firefox",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    detail_url = None
    detail_html = ""
    detail_title = ""
    search_html = ""
    search_title = ""
    sf_id_mpn_safe = None  # for direct /part/ URL construction if click fails

    try:
        with sync_playwright() as p:
            br = p.firefox.launch(headless=True)
            ctx = br.new_context(user_agent=UA_FF, locale="en-US",
                                 viewport={"width": 1440, "height": 1200})
            page = ctx.new_page()
            t0 = time.time()

            # 1. Homepage warmup
            print(f"[rochester] warmup homepage")
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=45000)
            try: page.wait_for_load_state("networkidle", timeout=8000)
            except Exception: pass
            page.wait_for_timeout(2000)

            # 2. Navigate to search
            print(f"[rochester] goto {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            try: page.wait_for_load_state("networkidle", timeout=12000)
            except Exception: pass

            # Poll for hydration — either results appear OR no-results message
            results_state = "pending"
            for poll_i in range(20):
                page.wait_for_timeout(1500)
                html_now = page.content()
                # No-results signal
                if "couldn’t find any results" in html_now or "couldn't find any results" in html_now:
                    results_state = "no_results"
                    print(f"[rochester] no-results message at iter {poll_i}")
                    break
                # Results signal: productName spans appear
                if 'class="productName"' in html_now and len(html_now) > 400_000:
                    results_state = "ok"
                    print(f"[rochester] results hydrated at iter {poll_i}, len={len(html_now):,}")
                    break

            search_html = page.content()
            search_title = page.title()
            (run_dir / f"{safe}_search.html").write_text(search_html, encoding="utf-8")
            try:
                page.screenshot(path=str(run_dir / f"{safe}_search.png"), full_page=True)
            except Exception: pass
            record["attempts"].append({
                "method": "playwright_firefox", "url": search_url,
                "status": 200, "len": len(search_html),
                "outcome": results_state, "title": search_title,
            })

            if results_state == "no_results":
                record["status"] = "no_results"
                br.close()
                return record

            # 3. Click first product. The clickable element is the productName span.
            # Find the product card matching input MPN (or first one).
            target = re.sub(r"[^A-Za-z0-9]", "", part).upper()
            clicked = False
            spans = page.query_selector_all("span.productName")
            print(f"[rochester] productName spans: {len(spans)}")
            for span in spans[:12]:
                try:
                    txt = (span.inner_text() or "").strip()
                except Exception:
                    continue
                if not txt:
                    continue
                txt_clean = re.sub(r"[^A-Za-z0-9]", "", txt).upper()
                if target in txt_clean or txt_clean.startswith(target):
                    print(f"[rochester] → clicking productName: {txt!r}")
                    try:
                        span.scroll_into_view_if_needed()
                        span.click()
                        clicked = True
                        break
                    except Exception as e:
                        print(f"[rochester] click failed: {e}")
            if not clicked and spans:
                # No span matched the input MPN. Rochester's global search returns
                # *related* products by category when no exact match exists, so
                # clicking the first row would silently scrape an unrelated part.
                # Bail with no_results instead.
                print(f"[rochester] no productName matched input MPN — treating as no_results")
                record["status"] = "no_results"
                record["attempts"][-1]["outcome"] = "no_results_no_mpn_match"
                br.close()
                return record

            if not clicked:
                # No productName spans at all — try last-resort URL scan, else no_results
                m = re.search(r"/part/([A-Za-z0-9]+)-([A-Za-z0-9]+)", search_html)
                if m:
                    detail_url = f"{BASE}/part/{m.group(1)}-{m.group(2)}"
                    page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
                    clicked = True
                else:
                    record["status"] = "no_results"
                    br.close()
                    return record

            page.wait_for_timeout(4000)
            try: page.wait_for_load_state("networkidle", timeout=10000)
            except Exception: pass
            # Wait for detail-page hydration: spec values fill in after LWC loads
            for poll_i in range(15):
                page.wait_for_timeout(1500)
                html_now = page.content()
                # Use "Manufacturer OPN" as the hydration marker (always on detail)
                if "Manufacturer OPN" in html_now and ("In Stock" in html_now or "$" in html_now):
                    print(f"[rochester] detail hydrated at iter {poll_i}")
                    break

            detail_html = page.content()
            detail_title = page.title()
            detail_url = page.url
            record["resolved_product_url"] = detail_url
            (run_dir / f"{safe}_product.html").write_text(detail_html, encoding="utf-8")
            try:
                page.screenshot(path=str(run_dir / f"{safe}_product.png"), full_page=True)
            except Exception: pass
            elapsed = time.time() - t0
            record["attempts"].append({
                "method": "playwright_firefox", "url": detail_url,
                "status": 200, "len": len(detail_html),
                "outcome": "ok", "title": detail_title,
                "total_elapsed_sec": round(elapsed, 2),
            })
            br.close()
    except Exception as exc:
        record["status"] = "exception"
        record["error"] = str(exc)
        return record

    extracted = extract_from_detail_html(detail_html, part)
    extracted["product_url"] = detail_url
    record["extracted"] = extracted
    record["status"] = "ok"
    record["data_quality"] = quality_for(extracted)
    return record


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "BTA316-600E"
    out_dir_override = argv[2] if len(argv) > 2 else None
    run_dir = (Path(out_dir_override).resolve() if out_dir_override else make_run_dir(part))
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== ROCHESTER scrape: {part} ===")
    print(f"output folder: {run_dir}")

    safe = _safe(part)
    rec = scrape(part, run_dir)
    out = run_dir / f"{safe}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = write_summary(rec, run_dir, safe)
    print(f"\nWrote {out}")
    print(f"Wrote {summary}")
    print(f"status: {rec.get('status')}  quality: {rec.get('data_quality')}")
    ex = rec.get("extracted") or {}
    if ex:
        for k in ("manufacturer_part_number", "manufacturer", "generic_pn",
                  "package", "stock_now_qty", "lifecycle_status",
                  "datasheet_url"):
            v = ex.get(k)
            if v is not None and v != "":
                print(f"  {k}: {v}")
        print(f"  prices: {len(ex.get('prices') or [])} tiers")
        print(f"  parameters: {len(ex.get('parameters') or [])} attributes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
