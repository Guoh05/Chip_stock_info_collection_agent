"""RS Components (RS 欧时, rsonline.cn) product scraper.

Strategy:
  1. curl_cffi (chrome131) GET `/web/c/?searchTerm=<MPN>` — Next.js SSR page.
     `<script id="__NEXT_DATA__">` carries the product list (1-N variants) as
     clean JSON objects with `mpn`, `brand`, `description`, `stockStatus`
     (`IN_STOCK`/`OUT_OF_STOCK`), `displayPrice`, `breakQty1`, `packSize`,
     `packType`, `productURL`. Pick the variant matching the input MPN exactly,
     else the highest-priority one.
  2. Fetch the product detail URL. The detail page also has __NEXT_DATA__ with
     a richer product object including Schema.org `offers`, `additionalProperty`
     (the spec parameter table), and tier prices.
  3. Map to canonical schema.

Folder layout: test/scraper/Test_<MPN>_RSONLINE_<YYYYMMDD>_<HH>_<MM>_<SS>/
  <MPN>.json
  <MPN>_summary.md
  <MPN>_search.html
  <MPN>_search.png
  <MPN>_search_next_data.json
  <MPN>_product.html
  <MPN>_product.png
  <MPN>_product_next_data.json
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as cf_requests
from playwright.sync_api import sync_playwright

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper"
CHANNEL = "RSONLINE"
BASE = "https://www.rsonline.cn"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def make_run_dir(part: str, channel: str = CHANNEL) -> Path:
    now = datetime.now()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    name = (
        f"Test_{safe}_{channel}_"
        f"{now.strftime('%Y%m%d')}_{now.strftime('%H_%M_%S')}"
    )
    run_dir = TEST_ROOT / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _extract_next_data(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _walk_product_objects(nd: dict) -> list[dict]:
    """Find product-shaped dicts (with mpn + brand/stockStatus/productURL)."""
    found: list[dict] = []
    seen_urls: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            if "mpn" in o and ("brand" in o or "stockStatus" in o or "productURL" in o):
                key = str(o.get("productURL", "")) + "|" + str(o.get("mpn", ""))
                if key not in seen_urls:
                    seen_urls.add(key)
                    found.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(nd)
    return found


def _walk_schema_org_product(nd: dict) -> dict | None:
    """Detail pages embed a Schema.org `Product` entity (with name, brand, offers,
    additionalProperty). Find it inside the __NEXT_DATA__ tree."""
    def walk(o):
        if isinstance(o, dict):
            if o.get("@type") == "Product" and ("name" in o or "mpn" in o):
                return o
            for v in o.values():
                r = walk(v)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r is not None:
                    return r
        return None
    return walk(nd)


def _parse_price(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    txt = re.sub(r"[^\d.,]", "", str(s))
    if not txt:
        return None
    if "," in txt and "." not in txt:
        txt = txt.replace(",", ".")
    else:
        txt = txt.replace(",", "")
    try:
        return float(txt)
    except ValueError:
        return None


def normalize(search_product: dict, detail_product: dict | None) -> dict:
    """Map RS product objects to canonical schema."""
    dp = detail_product or {}
    sp = search_product or {}

    mpn = dp.get("mpn") or sp.get("mpn")
    # `brand` may be a Schema.org Brand object {"@type":"Brand","name":"…"} on
    # detail pages, or a flat string on search-results pages. Flatten.
    raw_brand = dp.get("brand") or sp.get("brand")
    if isinstance(raw_brand, dict):
        brand = raw_brand.get("name") or raw_brand.get("@id")
    else:
        brand = raw_brand
    description = dp.get("description") or sp.get("description")
    image = dp.get("image") or sp.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url") or image.get("@id")
    product_url = sp.get("productURL") or dp.get("url") or dp.get("@id")

    # Stock: RS shows "IN_STOCK" / "OUT_OF_STOCK" strings (no quantity exposed
    # in JSON for non-logged-in sessions). DO NOT fabricate a warehouse name
    # such as "RS 欧时仓" — the page does not show that label, so claiming it
    # invents UI context the user has no way to verify. Leave ship_text None
    # here; the real one (e.g. "15 件将从其他地点发货" or "暂时缺货 2026年9月1日
    # 发货") is filled in further downstream by the Adobe-data-layer regex in
    # the scrape() body, which actually reads the visible HTML.
    stock_status = sp.get("stockStatus") or ""
    has_stock = stock_status == "IN_STOCK"
    stock_now_qty = None  # RS does not expose exact qty in public JSON
    stock_now_ship_text = None

    # Prices — RS's Schema.org AggregateOffer carries the real tier breakdown.
    # Each Offer has: price, priceCurrency, eligibleQuantity.minValue, availability.
    display_price = sp.get("displayPrice")
    break_qty1 = sp.get("breakQty1")
    prices: list[dict] = []
    currency = None

    offers_root = dp.get("offers")
    if isinstance(offers_root, dict):
        # AggregateOffer with .offers[] OR a single Offer with .price
        if offers_root.get("@type") == "AggregateOffer":
            # AggregateOffer-level availability is more authoritative than the
            # search-page stockStatus string
            avail = offers_root.get("availability") or ""
            if "InStock" in avail:
                has_stock = True
                # ship_text left None — the visible message ("从其他地点发货"
                # / "暂时缺货" / future date) is read from the rendered HTML
                # later, not invented here.
            elif "OutOfStock" in avail:
                has_stock = False
                stock_now_ship_text = None
            sub_offers = offers_root.get("offers") or []
            if isinstance(sub_offers, list):
                for off in sub_offers:
                    if not isinstance(off, dict):
                        continue
                    eq = off.get("eligibleQuantity") or {}
                    min_q = eq.get("minValue") if isinstance(eq, dict) else None
                    max_q = eq.get("maxValue") if isinstance(eq, dict) else None
                    price_val = off.get("price")
                    cur = off.get("priceCurrency")
                    if cur and not currency:
                        currency = cur
                    if price_val is not None and min_q is not None:
                        tier = {
                            "min_qty": int(min_q) if str(min_q).isdigit() else min_q,
                            "unit_price": f"{price_val} {cur or ''}".strip(),
                            "unit_price_float": _parse_price(price_val),
                            "currency": cur,
                        }
                        if max_q is not None:
                            tier["max_qty"] = int(max_q) if str(max_q).isdigit() else max_q
                        prices.append(tier)
            # Fall back to top-level price if no sub_offers
            if not prices and offers_root.get("price") is not None:
                prices.append({
                    "min_qty": _parse_price(break_qty1) and int(_parse_price(break_qty1)),
                    "unit_price": f"{offers_root['price']} {offers_root.get('priceCurrency') or ''}".strip(),
                    "unit_price_float": _parse_price(offers_root.get("price")),
                    "currency": offers_root.get("priceCurrency"),
                })
                if not currency:
                    currency = offers_root.get("priceCurrency")
        elif offers_root.get("@type") == "Offer":
            prices.append({
                "min_qty": _parse_price(break_qty1) and int(_parse_price(break_qty1)),
                "unit_price": str(offers_root.get("price")),
                "unit_price_float": _parse_price(offers_root.get("price")),
                "currency": offers_root.get("priceCurrency"),
            })
            currency = offers_root.get("priceCurrency")
    # If still no prices, fall back to search-page displayPrice scalar
    if not prices and display_price is not None and break_qty1 is not None:
        prices.append({
            "min_qty": int(_parse_price(break_qty1)) if _parse_price(break_qty1) else None,
            "unit_price": display_price,
            "unit_price_float": _parse_price(display_price),
            "packaging": sp.get("packType"),
        })

    # Sort tiers by min_qty
    prices.sort(key=lambda t: (t.get("min_qty") or 0))

    # Parameters from additionalProperty (Schema.org PropertyValue list)
    parameters: list[dict] = []
    addl = dp.get("additionalProperty") or []
    if isinstance(addl, list):
        for prop in addl:
            if isinstance(prop, dict):
                parameters.append({
                    "name": prop.get("name"),
                    "value": prop.get("value"),
                })

    # Spec items from search product
    leaf_cat = sp.get("leafCategoryName")
    pack_size = sp.get("packSize")
    pack_type = sp.get("packType")

    # stock_breakdown starts empty. The scrape() body later rebuilds it from
    # the visible HTML (Adobe data-layer `stockinfo` blob + the rendered
    # "<n> 件将从其他地点发货" / "另外 <n> 件将于 ... 发货" spans) when those
    # exist. We do NOT pre-populate fake per-price-tier rows with invented
    # "RS 欧时仓 (5–95)" warehouse names — the price tiers are tier *prices*,
    # not warehouse partitions.
    stock_breakdown: list[dict] = []

    # Promote 包装类型 / 封装 / Package parameter to canonical `package` field
    package = None
    for p in parameters:
        n = (p.get("name") or "").strip()
        if n in ("封装", "包装", "包装类型", "Package", "封装类型") or n.lower() in ("package", "package type", "case/package"):
            v = p.get("value")
            if isinstance(v, str) and v:
                package = v
                break

    return {
        "rs_stock_no": dp.get("sku") or (product_url.rstrip("/").split("/")[-1] if product_url else None),
        "manufacturer_part_number": mpn,
        "manufacturer": brand,
        "manufacturer_cn": None,
        "description_en": description if description and re.search(r"[A-Za-z]", description) else None,
        "description_cn": description if description and re.search(r"[一-鿿]", description) else None,
        "package": package,
        "packaging": pack_type,  # RS's "FINISHED" / "BULK" pack_type, distinct from chip package
        "pack_size": pack_size,
        "category_name_cn": leaf_cat,
        "lifecycle_status": stock_status,  # RS-native wording preserved here
        "stock_total": stock_now_qty,
        "stock_now_qty": stock_now_qty,
        "stock_now_ship_text": stock_now_ship_text,
        "stock_future_qty": None,
        "stock_future_ship_text": None,
        "stock_breakdown": stock_breakdown,
        "unit_price_cny": display_price,
        "datasheet_url": dp.get("datasheetUrl") or sp.get("datasheetUrl"),
        "image_url": image,
        "prices": prices,
        "parameters": parameters,
        "product_url": product_url,
        "currency": currency or "CNY",
        # site-native fields, preserved verbatim per memory rule
        "site_stock_status": stock_status,
        "site_aggregate_availability": (offers_root.get("availability") if isinstance(offers_root, dict) else None),
        "site_aggregate_high_price": (offers_root.get("highPrice") if isinstance(offers_root, dict) else None),
        "site_aggregate_low_price": (offers_root.get("lowPrice") if isinstance(offers_root, dict) else None),
        "site_aggregate_offer_count": (offers_root.get("offerCount") if isinstance(offers_root, dict) else None),
        "site_seller": (offers_root.get("seller", {}).get("name") if isinstance(offers_root, dict) and isinstance(offers_root.get("seller"), dict) else None),
        "site_break_qty1": break_qty1,
        "site_display_price": display_price,
        "site_pack_size": pack_size,
        "site_pack_type": pack_type,
        "site_leaf_category_name": leaf_cat,
    }


def quality_for(ex: dict) -> str:
    has_mpn = bool(ex.get("manufacturer_part_number"))
    has_mfr = bool(ex.get("manufacturer"))
    has_stock = ex.get("stock_now_qty") is not None or ex.get("site_stock_status")
    has_price = bool(ex.get("prices"))
    has_params = bool(ex.get("parameters"))
    if has_mpn and has_mfr and has_stock and has_params and has_price:
        return "high"
    if has_mpn and has_mfr and (has_price or has_stock):
        return "medium"
    if has_mpn:
        return "low"
    return "none"


def screenshot_url(url: str, out_path: Path) -> None:
    """Render via Playwright Chromium and screenshot. Dismisses RS's privacy
    consent modal ("重要的隐私信息") before capture so the screenshot shows the
    full product page, not just the cookie overlay."""
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = br.new_context(user_agent=UA, locale="zh-CN", viewport={"width": 1440, "height": 1200})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try: page.wait_for_load_state("networkidle", timeout=8000)
            except Exception: pass
            page.wait_for_timeout(2000)
            # Dismiss RS privacy/cookie modal — text-based since the button id changes
            for sel in (
                'button:has-text("我接受所有")',
                'button:has-text("接受所有")',
                'button:has-text("同意")',
                'button:has-text("I Accept All")',
                'button:has-text("Accept All")',
                '[id*="cookie"][id*="accept"]',
                '[class*="cookie"] button',
            ):
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue
            page.wait_for_timeout(500)
            page.screenshot(path=str(out_path), full_page=True)
            br.close()
    except Exception:
        pass


def scrape(part: str, run_dir: Path) -> dict:
    safe = _safe(part)
    search_url = f"{BASE}/web/c/?searchTerm={part}"
    record: dict = {
        "query": part,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "rsonline.cn",
        "search_url": search_url,
        "output_dir": str(run_dir),
        "method": "curl_cffi+next_data",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    s = cf_requests.Session(impersonate="chrome131")
    s.headers.update({"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})

    # --- Stage 1: search ---
    print(f"[rsonline] GET {search_url}")
    r = s.get(search_url, timeout=20, allow_redirects=True)
    record["attempts"].append({
        "method": "curl_cffi", "profile": "chrome131", "url": search_url,
        "status": r.status_code, "len": len(r.text),
        "outcome": "ok" if r.status_code == 200 else f"http_{r.status_code}",
    })
    (run_dir / f"{safe}_search.html").write_text(r.text, encoding="utf-8")
    nd_search = _extract_next_data(r.text)
    if nd_search:
        (run_dir / f"{safe}_search_next_data.json").write_text(
            json.dumps(nd_search, ensure_ascii=False, indent=2), encoding="utf-8")
    products = _walk_product_objects(nd_search or {})
    if not products:
        record["status"] = "no_results"
        return record

    # Pick variant matching input exactly. RS搜索没有精确匹配时会返回同类目下的相关产品 ——
    # 如果直接 fallback 到第一个，会把无关零件当成搜索结果。先尝试精确匹配，再做
    # 字母数字子串模糊匹配；都不中则视为 no_results。
    target = part.strip().lower()
    target_alnum = re.sub(r"[^A-Za-z0-9]", "", target).lower()
    def _norm_mpn(p): return re.sub(r"[^A-Za-z0-9]", "", str(p.get("mpn", ""))).lower()
    exact = [p for p in products if str(p.get("mpn", "")).strip().lower() == target]
    fuzzy = [p for p in products if target_alnum and target_alnum in _norm_mpn(p)]
    if exact:
        chosen = exact[0]
    elif fuzzy:
        chosen = fuzzy[0]
    else:
        record["status"] = "no_results"
        record["search_products_returned"] = [str(p.get("mpn", ""))[:60] for p in products[:5]]
        return record
    detail_url = chosen.get("productURL") or chosen.get("url")
    if not detail_url:
        record["status"] = "no_detail_url"
        record["search_products"] = products
        return record
    if not detail_url.startswith("http"):
        detail_url = BASE + detail_url
    record["resolved_product_url"] = detail_url
    print(f"[rsonline] detail → {detail_url}")

    # --- Stage 2: detail ---
    rd = s.get(detail_url, timeout=20, allow_redirects=True)
    record["attempts"].append({
        "method": "curl_cffi", "profile": "chrome131", "url": detail_url,
        "status": rd.status_code, "len": len(rd.text),
        "outcome": "ok" if rd.status_code == 200 else f"http_{rd.status_code}",
    })
    (run_dir / f"{safe}_product.html").write_text(rd.text, encoding="utf-8")
    nd_detail = _extract_next_data(rd.text)
    if nd_detail:
        (run_dir / f"{safe}_product_next_data.json").write_text(
            json.dumps(nd_detail, ensure_ascii=False, indent=2), encoding="utf-8")
    detail_product = _walk_schema_org_product(nd_detail or {}) if nd_detail else None
    # also try to find an "mpn" object on the detail page (richer than Schema.org sometimes)
    if not detail_product and nd_detail:
        candidates = _walk_product_objects(nd_detail)
        if candidates:
            detail_product = max(candidates, key=lambda d: len(d.keys()))

    extracted = normalize(chosen, detail_product)

    # Augment with stock count + status from the detail HTML's Adobe-analytics
    # data layer. RS uses a pipe-separated `"stockinfo":{"quantity":"15 | 100","date":"2026年5月18日 | 2026年5月25日","status":"IN_STOCK"}`
    # blob: the first slot is what's available now (typically "ships from
    # another location"), additional slots are future-dated batches.
    detail_html = rd.text
    stockinfo_m = re.search(
        r'"stockinfo"\s*:\s*\{[^}]*"date"\s*:\s*"([^"]*)"[^}]*"quantity"\s*:\s*"([^"]*)"[^}]*"status"\s*:\s*"([^"]*)"',
        detail_html,
    )
    if not stockinfo_m:
        # date may come after quantity — try the other ordering
        stockinfo_m = re.search(
            r'"stockinfo"\s*:\s*\{[^}]*"quantity"\s*:\s*"([^"]*)"[^}]*"date"\s*:\s*"([^"]*)"[^}]*"status"\s*:\s*"([^"]*)"',
            detail_html,
        )
        if stockinfo_m:
            # swap so dates is group(1), quantity group(2)
            class _M:
                def __init__(self, q, d, s): self._g = (None, d, q, s)
                def group(self, i): return self._g[i]
            stockinfo_m = _M(stockinfo_m.group(1), stockinfo_m.group(2), stockinfo_m.group(3))
    if stockinfo_m:
        dates = [s.strip() for s in (stockinfo_m.group(1) or "").split("|")]
        qtys  = [s.strip() for s in (stockinfo_m.group(2) or "").split("|")]
        status = stockinfo_m.group(3) or ""
        extracted["site_stock_status"] = status or extracted.get("site_stock_status")
        # Normalise pairs of (qty, date)
        pairs = []
        for i, q in enumerate(qtys):
            try:
                qn = int(q.replace(",", ""))
            except ValueError:
                continue
            d = dates[i] if i < len(dates) else ""
            pairs.append((qn, d))
        if pairs:
            now_qty, now_date = pairs[0]
            extracted["stock_now_qty"] = now_qty
            extracted["stock_total"] = sum(p[0] for p in pairs)
            # Use the visible message: "{q} 件将从其他地点发货" for first batch
            extracted["stock_now_ship_text"] = (
                f"{now_qty} 件将从其他地点发货"
                if len(pairs) > 1 else f"{now_qty} 件可用"
            )
            if len(pairs) > 1:
                fut_qty, fut_date = pairs[1]
                extracted["stock_future_qty"] = fut_qty
                extracted["stock_future_ship_text"] = (
                    f"另外 {fut_qty} 件将于 {fut_date} 发货" if fut_date else f"{fut_qty} 件期货"
                )
        elif "OUT_OF_STOCK" in status.upper():
            # OOS path: stockinfo blob exists, quantity is empty string, but
            # the date slot often carries the next-restock day (visible on
            # page as "暂时缺货 / 2026年9月1日发货"). Reflect that explicitly
            # so the summary shows the shortage instead of looking blank.
            extracted["stock_now_qty"] = 0
            extracted["stock_now_ship_text"] = "暂时缺货"
            fut_date = next((d for d in dates if d), "")
            if fut_date:
                # Future quantity is unknown — only the date is published.
                # Leave stock_future_qty as None; just record the visible date.
                extracted["stock_future_ship_text"] = f"{fut_date} 发货"

    # Backup: "product_page_stock_volume":"15 | 100"
    if extracted.get("stock_now_qty") is None:
        m = re.search(r'"product_page_stock_volume"\s*:\s*"([^"]+)"', detail_html)
        if m:
            parts = [p.strip() for p in m.group(1).split("|")]
            try:
                extracted["stock_now_qty"] = int(parts[0].replace(",", ""))
                extracted["stock_total"] = extracted["stock_now_qty"]
            except ValueError:
                pass
    # Visible stock message backup: "N 件将从其他地点发货"
    if extracted.get("stock_now_qty") is None:
        m = re.search(
            r'<span[^>]*>\s*([\d,]+)\s*</span>\s*<span[^>]*>\s*件[^<]*?(?:库存|发货|可用)',
            detail_html,
        )
        if m:
            try:
                extracted["stock_now_qty"] = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # Rebuild stock_breakdown to reflect the visible RS messaging.
    # We only emit rows when we have a concrete quantity from the page —
    # never warehouse-labelled rows guessed from price tiers. The visible
    # "ships from another location" wording is the only label RS itself
    # publishes; no warehouse name is shown to anonymous visitors.
    if extracted.get("stock_now_qty"):
        breakdown = [{
            "label": "现货 (其他地点)",
            "warehouse": None,   # RS does not name the warehouse to anonymous visitors
            "quantity": extracted.get("stock_now_qty"),
            "ship_text": extracted.get("stock_now_ship_text") or "件将从其他地点发货",
        }]
        if extracted.get("stock_future_qty"):
            breakdown.append({
                "label": "期货",
                "warehouse": None,
                "quantity": extracted.get("stock_future_qty"),
                "ship_text": extracted.get("stock_future_ship_text") or "",
            })
        extracted["stock_breakdown"] = breakdown
    elif extracted.get("stock_now_qty") == 0 and extracted.get("stock_future_ship_text"):
        # OOS with a published restock date — emit a single 期货 row with the
        # date and no quantity (RS does not publish the incoming quantity).
        extracted["stock_breakdown"] = [{
            "label": "期货",
            "warehouse": None,
            "quantity": None,
            "ship_text": extracted.get("stock_future_ship_text"),
        }]
    # else: leave stock_breakdown as whatever normalize() returned (which is
    # now an empty list by default — no fabrication).
    record["extracted"] = extracted
    record["status"] = "ok"
    record["data_quality"] = quality_for(extracted)

    # Screenshots
    screenshot_url(search_url, run_dir / f"{safe}_search.png")
    screenshot_url(detail_url, run_dir / f"{safe}_product.png")
    return record


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "LIS2DH12TR"
    out_dir_override = argv[2] if len(argv) > 2 else None
    run_dir = (Path(out_dir_override).resolve() if out_dir_override else make_run_dir(part))
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== RSONLINE scrape: {part} ===")
    print(f"output folder: {run_dir}")

    safe = _safe(part)
    rec = scrape(part, run_dir)
    out = run_dir / f"{safe}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = write_summary(rec, run_dir, safe)
    print(f"\nWrote {out}")
    print(f"Wrote {summary}")
    print(f"status: {rec.get('status')}  method: {rec.get('method')}  quality: {rec.get('data_quality')}")
    ex = rec.get("extracted") or {}
    if ex:
        for k in ("manufacturer_part_number", "manufacturer", "package",
                  "site_stock_status", "site_display_price", "site_break_qty1",
                  "site_pack_size", "site_pack_type", "site_leaf_category_name"):
            v = ex.get(k)
            if v is not None and v != "":
                print(f"  {k}: {v}")
        print(f"  prices: {len(ex.get('prices') or [])} tiers")
        print(f"  parameters: {len(ex.get('parameters') or [])} attributes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
