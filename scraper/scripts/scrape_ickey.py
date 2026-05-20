"""ICKEY (云汉芯城, ickey.cn) product scraper.

ICKEY is a B2B marketplace aggregator (similar to HQEW): one MPN search returns
N supplier listings, each pointing to a `/detail/<sku_id>/<MPN>.html` page.
The detail page is sourced from a specific distributor — ICKEY title format:
  `<MPN>（<distributor>）采购_价格_数据手册-云汉芯城 ICkey.cn`
The (parenthetical) reveals which distributor's inventory this listing is from.

Strategy:
  1. curl_cffi probe (KNOWN to return a "未找到" template even when results
     exist — ickey hydrates products via XHR). Recorded in attempts log.
  2. Playwright Chromium navigate the search URL, wait for `totalNumber > 0`
     and `len(html) > 800k` to indicate XHR results have loaded.
  3. Extract all `/detail/<id>/<MPN>.html` anchors. These are the per-supplier
     product listings.
  4. Pick the first one whose URL contains the exact MPN (case-insensitive),
     else the first one available.
  5. Fetch the detail page (curl_cffi works here — only the search page needs
     JS), parse for canonical schema.

Folder layout: test/scraper_test/Test_<MPN>_ICKEY_<YYYYMMDD>_<HH>_<MM>_<SS>/
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
from curl_cffi import requests as cf_requests
from playwright.sync_api import sync_playwright

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
CHANNEL = "ICKEY"
SEARCH_BASE = "https://search.ickey.cn"
WWW_BASE = "https://www.ickey.cn"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
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


def extract_from_detail_html(html: str, mpn: str) -> dict:
    """Pull canonical-schema fields from ICKEY's product detail page.

    Title format: `<MPN>（<distributor>）采购_价格_数据手册-云汉芯城 ICkey.cn`
    """
    soup = BeautifulSoup(html, "lxml")
    extracted: dict = {
        "manufacturer_part_number": None,
        "manufacturer": None,
        "manufacturer_cn": None,
        "supplier_distributor": None,  # which distributor this listing is from
        "description_en": None,
        "description_cn": None,
        "package": None,
        "category_name_cn": None,
        "lifecycle_status": None,
        "stock_total": None,
        "stock_now_qty": None,
        "stock_now_ship_text": None,
        "stock_future_qty": None,
        "stock_future_ship_text": None,
        "stock_breakdown": [],
        "unit_price_cny": None,
        "min_order_qty": None,
        "delivery_location": None,
        "delivery_time": None,
        "datasheet_url": None,
        "image_url": None,
        "prices": [],
        "parameters": [],
    }

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    extracted["page_title"] = title
    extracted["site_title"] = title

    # Title parse: "<MPN>（<distributor>）..."
    m = re.match(r"^([A-Za-z0-9._\-/,]+)（([^）]+)）", title)
    if m:
        extracted["manufacturer_part_number"] = m.group(1).strip()
        extracted["supplier_distributor"] = m.group(2).strip()
    else:
        # fallback: split on first underscore
        if "_" in title:
            extracted["manufacturer_part_number"] = title.split("_")[0].strip()

    # Manufacturer / brand — try in order of cleanest:
    #  1) Schema.org JSON-LD `manufacturer.name` (most reliable on ICKEY)
    #  2) The "厂牌" link on the right-side info card
    #  3) Open Graph / itemprop meta tags
    #  4) Labeled text patterns
    body_text = soup.get_text(" ", strip=True)
    body_text = re.sub(r"\s+", " ", body_text)
    # 1) Schema.org JSON-LD
    for ld in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(ld.string or "{}")
        except (json.JSONDecodeError, AttributeError):
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue
            mfr = item.get("manufacturer")
            if isinstance(mfr, dict) and mfr.get("name"):
                extracted["manufacturer"] = mfr["name"]
            elif isinstance(mfr, str):
                extracted["manufacturer"] = mfr
            # Description from JSON-LD is cleaner than scraped body text
            if item.get("description") and not extracted.get("description_cn"):
                extracted["description_cn"] = str(item["description"])[:300]
            if extracted["manufacturer"]:
                break
        if extracted["manufacturer"]:
            break
    # 2) "厂牌" link on the info card
    if not extracted["manufacturer"]:
        m = re.search(r'<span[^>]*>\s*厂牌\s*[：:]\s*</span>\s*<a[^>]*>\s*([^<]+?)\s*</a>', html)
        if m:
            extracted["manufacturer"] = m.group(1).strip()
    # 3) Meta tags
    if not extracted["manufacturer"]:
        for meta_pat in (
            r'<meta[^>]+(?:name|property)="og:brand"[^>]+content="([^"]+)"',
            r'<meta[^>]+itemprop="brand"[^>]+content="([^"]+)"',
        ):
            m = re.search(meta_pat, html)
            if m:
                extracted["manufacturer"] = m.group(1).strip()
                break
    # 4) Labeled text in body
    if not extracted["manufacturer"]:
        for label in ("品牌", "制造商", "厂商", "厂家", "Brand", "Manufacturer"):
            m = re.search(rf"{label}\s*[:：]?\s*([A-Za-z0-9一-鿿　·\.\-_/\\&]{{2,80}})", body_text)
            if m:
                v = m.group(1).strip()
                if v and len(v) < 80:
                    extracted["manufacturer"] = v
                    break

    # Package — search ALL of html (not just body_text) so the JSON-LD
    # description is also covered (ICKEY embeds the package there).
    for pkg_pat in (r"\b(LQFP-\d+)\b", r"\b(TSSOP-\d+)\b", r"\b(QFN-\d+)\b",
                    r"\b(SOIC-\d+)\b", r"\b(SOP-\d+)\b", r"\b(BGA-\d+)\b",
                    r"\b(SOT-?\d+)\b", r"\b(TO-?\d+[A-Z]*)\b", r"\b(DIP-\d+)\b",
                    r"\b(LGA-\d+)\b", r"\b(VFQFPN-\d+)\b"):
        m = re.search(pkg_pat, html)
        if m:
            extracted["package"] = m.group(1)
            break

    # Description
    for label in ("描述", "产品描述", "简介", "Description"):
        m = re.search(rf"{label}\s*[:：]?\s*([^\n\r]{{5,300}})", body_text)
        if m:
            extracted["description_cn"] = m.group(1).strip()[:300]
            break

    # Stock 库存 / 现货 — ICKEY exposes the per-supplier stock as
    # `<strong id="proStock">4102</strong>` next to the MPN
    stock_m = re.search(r'id="proStock"[^>]*>\s*([\d,]+)\s*</strong>', html)
    if stock_m:
        qty = _parse_int(stock_m.group(1))
        if qty is not None:
            extracted["stock_now_qty"] = qty
            extracted["stock_total"] = qty
    if extracted["stock_now_qty"] is None:
        # fallback: search labeled text
        for label in ("库存", "现货数量", "现货库存", "可售库存"):
            m = re.search(rf"{label}\s*[:：]?\s*([\d,]+)", body_text)
            if m:
                qty = _parse_int(m.group(1))
                if qty is not None and qty > 0:
                    extracted["stock_now_qty"] = qty
                    extracted["stock_total"] = qty
                    break

    # MOQ (最小起订量) — `<span id="proMoq">109</span>起订` next to the stock row
    moq_m = re.search(r'id="proMoq"[^>]*>\s*([\d,]+)\s*</span>', html)
    if moq_m:
        moq = _parse_int(moq_m.group(1))
        if moq is not None:
            extracted["min_order_qty"] = moq
    if extracted["min_order_qty"] is None:
        # fallback: var moqNum = parseInt("109"); in the embedded JS
        moq_m = re.search(r'var\s+moqNum\s*=\s*parseInt\("([\d]+)"\)', html)
        if moq_m:
            extracted["min_order_qty"] = _parse_int(moq_m.group(1))

    # 交货地 + 交货时间 — `货期：内地 成团后<span>10-14工作日</span>`
    # The location ("内地" or "香港") appears between 货期： and 成团后.
    lt_m = re.search(
        r"货期\s*[：:]\s*([^\s<]+)\s*(?:成团后)?\s*<span[^>]*>\s*([^<]+?)\s*</span>",
        html,
    )
    if lt_m:
        extracted["delivery_location"] = lt_m.group(1).strip()
        extracted["delivery_time"] = lt_m.group(2).strip()
    else:
        # fallback: `<label for="radio-home">内地<span ...>10-14工作日</span></label>`
        lt_m = re.search(
            r'<label[^>]*for="radio-[^"]*"[^>]*>\s*(\S+?)\s*<span[^>]*>\s*([^<]+?)\s*</span>',
            html,
        )
        if lt_m:
            extracted["delivery_location"] = lt_m.group(1).strip()
            extracted["delivery_time"] = lt_m.group(2).strip()

    # Prices ¥X.XX — ICKEY uses `N+` quantity-break format (1+ / 10+ / 100+ etc.)
    # The price tier table has <th>1+</th><th>10+</th>... headers in one row and
    # <td>￥57.2996</td><td>￥43.911</td>... in the following row(s).
    price_pairs: list[dict] = []
    # Pattern 1: scan for `<th>N+</th>` qty headers paired with `<td>￥price</td>`
    # in adjacent positions. Cheapest: find all `qty+` substrings and their nearest
    # ¥price in the surrounding 200 chars.
    for m in re.finditer(r">(\d{1,5})\+</(?:th|td)>", html):
        qty = int(m.group(1))
        # Look up to 600 chars ahead for ￥price
        ahead = html[m.end(): m.end() + 600]
        price_m = re.search(r"[￥¥]\s*([\d.]+)", ahead)
        if price_m:
            price = _parse_float(price_m.group(1))
            if price is not None and price > 0:
                key = (qty, price)
                if not any(p["min_qty"] == qty for p in price_pairs):
                    price_pairs.append({
                        "min_qty": qty,
                        "unit_price": f"¥{price}",
                        "unit_price_float": price,
                        "currency": "CNY",
                    })
    # Pattern 2 (fallback): plain table rows `<td>qty</td><td>¥price</td>`
    if not price_pairs:
        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 2:
                qty = _parse_int(cells[0])
                price_match = re.search(r"[￥¥]\s*([\d.]+)", " ".join(cells))
                if qty is not None and qty > 0 and price_match:
                    price = _parse_float(price_match.group(1))
                    if price is not None and price > 0:
                        price_pairs.append({
                            "min_qty": qty,
                            "unit_price": f"¥{price}",
                            "unit_price_float": price,
                            "currency": "CNY",
                        })
    price_pairs.sort(key=lambda t: t["min_qty"])
    extracted["prices"] = price_pairs
    if price_pairs:
        extracted["unit_price_cny"] = price_pairs[0].get("unit_price_float")
    else:
        single = re.search(r"[￥¥]\s*([\d.]+)", body_text)
        if single:
            extracted["unit_price_cny"] = _parse_float(single.group(1))

    # Datasheet
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        href = a["href"]
        if ("datasheet" in txt or "数据手册" in txt or href.lower().endswith(".pdf")):
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = WWW_BASE + href
            extracted["datasheet_url"] = href
            break

    # Product image
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "product" in src or "/upload/" in src:
            if src.startswith("//"):
                src = "https:" + src
            extracted["image_url"] = src
            break

    # Spec / parameter table — skip placeholder `-` values (ICKEY shows these
    # for unfilled fields; they're noise).
    def _is_real(v: str) -> bool:
        v = (v or "").strip()
        return bool(v) and v not in ("-", "—", "_", "暂无", "暂无数据", "属性值")

    seen_params: set[tuple] = set()
    def _add_param(name: str, value: str):
        k = (name or "").strip().rstrip("：:")
        v = (value or "").strip()
        if not k or len(k) > 30 or len(v) > 200:
            return
        if not _is_real(v):
            return
        if (k, v) in seen_params:
            return
        seen_params.add((k, v))
        extracted["parameters"].append({"name": k, "value": v})

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) == 2:
            _add_param(cells[0], cells[1])
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            _add_param(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
    for item in soup.find_all(class_=re.compile(r"item|spec|param|attr|product-attr", re.I)):
        spans = item.find_all(["span", "div"], recursive=False)
        if len(spans) >= 2:
            _add_param(spans[0].get_text(" ", strip=True),
                       spans[1].get_text(" ", strip=True))

    # Promote 封装 / 通用封装 to top-level package (only if not already set
    # and value is real).
    for p in extracted["parameters"]:
        n = p.get("name") or ""
        v = p.get("value") or ""
        if ("封装" in n or n.lower().startswith("package")) and _is_real(v):
            if not extracted["package"]:
                extracted["package"] = v
            break

    # Stock breakdown
    if extracted["stock_now_qty"]:
        # ship_text reflects ICKEY's "货期：<location> 成团后 <delivery_time>"
        loc = extracted.get("delivery_location")
        dtime = extracted.get("delivery_time")
        if loc and dtime:
            ship_text = f"{loc}成团后 {dtime}"
        elif dtime:
            ship_text = f"成团后 {dtime}"
        else:
            ship_text = "ICKEY 转售供应商现货"

        breakdown_row = {
            "label": "现货",
            "warehouse": f"ICKEY 转售 ({extracted.get('supplier_distributor') or '云汉芯城'})",
            "quantity": extracted["stock_now_qty"],
            "ship_text": ship_text,
        }
        if extracted.get("min_order_qty"):
            breakdown_row["moq"] = extracted["min_order_qty"]
        if extracted.get("delivery_location"):
            breakdown_row["delivery_location"] = extracted["delivery_location"]
        if extracted.get("delivery_time"):
            breakdown_row["delivery_time"] = extracted["delivery_time"]
        extracted["stock_breakdown"].append(breakdown_row)
        extracted["stock_now_ship_text"] = ship_text

    return extracted


def quality_for(ex: dict) -> str:
    has_mpn = bool(ex.get("manufacturer_part_number"))
    has_mfr = bool(ex.get("manufacturer"))
    has_stock = ex.get("stock_now_qty") is not None
    has_price = bool(ex.get("prices")) or ex.get("unit_price_cny") is not None
    has_params = bool(ex.get("parameters"))
    if has_mpn and has_mfr and (has_stock or has_price) and has_params:
        return "high"
    if has_mpn and has_mfr and (has_stock or has_price):
        return "medium"
    if has_mpn:
        return "low"
    return "none"


def scrape(part: str, run_dir: Path) -> dict:
    safe = _safe(part)
    search_url = f"{SEARCH_BASE}/?keyword={part}"
    record: dict = {
        "query": part,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "ickey.cn",
        "search_url": search_url,
        "output_dir": str(run_dir),
        "method": "playwright_chromium",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    # --- 1. curl_cffi probe (always shows "未找到" pre-XHR, recorded for log) ---
    try:
        s = cf_requests.Session(impersonate="chrome131")
        s.headers.update({"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        r0 = s.get(search_url, timeout=15)
        record["attempts"].append({
            "method": "curl_cffi", "profile": "chrome131", "url": search_url,
            "status": r0.status_code, "len": len(r0.text),
            "outcome": "ssr_no_products" if "未找到" in r0.text else "ok",
        })
    except Exception as exc:
        record["attempts"].append({
            "method": "curl_cffi", "profile": "chrome131", "url": search_url,
            "outcome": "exception", "error": str(exc),
        })

    # --- 2. Playwright Chromium: hydrate search, find product anchors ---
    detail_url = None
    detail_html = ""
    detail_title = ""
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = br.new_context(user_agent=UA, locale="zh-CN", viewport={"width": 1440, "height": 1200})
            page = ctx.new_page()
            t0 = time.time()
            print(f"[ickey] goto search {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            try: page.wait_for_load_state("networkidle", timeout=12000)
            except Exception: pass
            hydrated_n = 0
            for i in range(20):
                page.wait_for_timeout(1500)
                html = page.content()
                tn = re.search(r'id="totalNumber"[^>]*>(\d+)', html)
                hydrated_n = int(tn.group(1)) if tn else 0
                if hydrated_n > 0 and len(html) > 800_000:
                    print(f"[ickey] hydrated: totalNumber={hydrated_n}, len={len(html):,}")
                    break
            search_html = page.content()
            search_title = page.title()
            (run_dir / f"{safe}_search.html").write_text(search_html, encoding="utf-8")
            try:
                page.screenshot(path=str(run_dir / f"{safe}_search.png"), full_page=True)
            except Exception: pass
            record["attempts"].append({
                "method": "playwright_chromium", "url": search_url,
                "status": 200, "len": len(search_html),
                "outcome": "ok" if hydrated_n > 0 else "no_results_after_hydration",
                "total_number": hydrated_n, "title": search_title,
            })
            if hydrated_n == 0:
                record["status"] = "no_results"
                br.close()
                return record

            # Extract real detail anchors
            detail_anchors = re.findall(
                r'href="(//www\.ickey\.cn/detail/\d+/[^"]+\.html)"', search_html)
            detail_anchors = sorted(set(detail_anchors))
            print(f"[ickey] detail anchors: {len(detail_anchors)}")
            if not detail_anchors:
                record["status"] = "no_detail_anchors"
                br.close()
                return record

            # Prefer one whose URL contains the exact MPN
            target_safe = re.sub(r"[^A-Za-z0-9]", "", part).upper()
            best = None
            for a in detail_anchors:
                a_clean = re.sub(r"[^A-Za-z0-9]", "", a).upper()
                if target_safe in a_clean:
                    best = a
                    break
            if not best:
                best = detail_anchors[0]
            detail_url = "https:" + best
            record["resolved_product_url"] = detail_url

            # Navigate to detail
            print(f"[ickey] detail → {detail_url}")
            page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
            try: page.wait_for_load_state("networkidle", timeout=12000)
            except Exception: pass
            # ICKEY uses doT.js templates for the price table — wait until JS
            # fills in real ¥ values (raw template HTML has `{{= …}}` markers).
            for poll_i in range(20):
                page.wait_for_timeout(1500)
                html_now = page.content()
                if re.search(r"[￥¥]\s*\d", html_now):
                    print(f"[ickey] detail price hydrated at iter {poll_i}")
                    break
            detail_html = page.content()
            detail_title = page.title()
            (run_dir / f"{safe}_product.html").write_text(detail_html, encoding="utf-8")
            try:
                page.screenshot(path=str(run_dir / f"{safe}_product.png"), full_page=True)
            except Exception: pass
            elapsed = time.time() - t0
            record["attempts"].append({
                "method": "playwright_chromium", "url": detail_url,
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
    part = argv[1] if len(argv) > 1 else "STM32F103C8T6"
    out_dir_override = argv[2] if len(argv) > 2 else None
    run_dir = (Path(out_dir_override).resolve() if out_dir_override else make_run_dir(part))
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== ICKEY scrape: {part} ===")
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
        for k in ("manufacturer_part_number", "manufacturer", "supplier_distributor",
                  "package", "stock_now_qty", "min_order_qty",
                  "delivery_location", "delivery_time",
                  "unit_price_cny", "datasheet_url"):
            v = ex.get(k)
            if v is not None and v != "":
                print(f"  {k}: {v}")
        print(f"  prices: {len(ex.get('prices') or [])} tiers")
        print(f"  parameters: {len(ex.get('parameters') or [])} attributes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
