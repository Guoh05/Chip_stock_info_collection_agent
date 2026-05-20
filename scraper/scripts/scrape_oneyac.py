"""ONEYAC (唯样商城, oneyac.com) product scraper.

Strategy:
  1. Playwright Firefox to clear Cloudflare interstitial on search page
     `https://www.oneyac.com/search/?keyword=<MPN>`.
  2. Find first product anchor `/product/<numeric_id>.html` and navigate to it.
  3. Parse the rendered detail page for canonical fields.

Folder layout: test/scraper/Test_<MPN>_ONEYAC_<YYYYMMDD>_<HH>_<MM>_<SS>/
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
CHANNEL = "ONEYAC"
BASE = "https://www.oneyac.com"

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
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(s)
    txt = re.sub(r"[^\d]", "", str(s))
    try:
        return int(txt) if txt else None
    except ValueError:
        return None


def _parse_float(s) -> float | None:
    if s is None or s == "":
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
    """Pull canonical-schema fields from the rendered ONEYAC detail page.

    The page uses Vue templates; after hydration the spec table renders as
    <div class="item"><span>label</span><span>value</span></div> patterns.
    """
    soup = BeautifulSoup(html, "lxml")
    extracted: dict = {
        "manufacturer_part_number": None,
        "manufacturer": None,
        "manufacturer_cn": None,
        "description_en": None,
        "description_cn": None,
        "package": None,
        "category_name_cn": None,
        "category_id": None,
        "lifecycle_status": None,
        "stock_total": None,
        "stock_now_qty": None,
        "stock_now_ship_text": None,
        "stock_future_qty": None,
        "stock_future_ship_text": None,
        "stock_breakdown": [],
        "unit_price_cny": None,
        "min_order_qty": None,
        "min_pack_qty": None,
        "datasheet_url": None,
        "image_url": None,
        "prices": [],
        "parameters": [],
        "product_url": None,
    }

    # Page title format: "<MPN>_类别_厂商_现货/采购/数据手册_唯样商城"
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    extracted["page_title"] = title
    # try MPN from heading
    h1 = soup.find(["h1", "h2"])
    if h1:
        text = h1.get_text(strip=True)
        if mpn.upper() in text.upper():
            extracted["manufacturer_part_number"] = mpn
    if not extracted["manufacturer_part_number"]:
        # title prefix until first underscore
        if "_" in title:
            cand = title.split("_")[0].strip()
            if cand:
                extracted["manufacturer_part_number"] = cand
        else:
            extracted["manufacturer_part_number"] = mpn

    # Brand/manufacturer — find from text patterns
    body_text = soup.get_text(" ", strip=True)
    body_text_compact = re.sub(r"\s+", " ", body_text)

    # Brand pattern: 品牌：<text>  or 制造商：<text>
    for label in ("品牌", "制造商", "厂商", "厂家", "Brand", "Manufacturer"):
        m = re.search(rf"{label}\s*[:：]\s*([A-Za-z0-9一-鿿　·\.\-_/\\&\s]{{1,80}})", body_text_compact)
        if m:
            v = m.group(1).strip().split("  ")[0].strip()
            v = re.sub(r"\s+(?:封装|包装|分类|描述|参数|数据手册|系列|型号)$", "", v)
            if v and not extracted["manufacturer"]:
                extracted["manufacturer"] = v[:80]
                break
    # Title typically: "<MPN>_<category>_<brand>_..."
    if not extracted["manufacturer"] and title and title.count("_") >= 2:
        parts = title.split("_")
        if len(parts) >= 3:
            extracted["manufacturer"] = parts[2].strip() or None

    # Description — search common label patterns
    for label in ("描述", "产品描述", "Description"):
        m = re.search(rf"{label}\s*[:：]\s*([^\n\r]{{5,200}})", body_text_compact)
        if m:
            extracted["description_cn"] = m.group(1).strip()[:200]
            break

    # Stock — the MAIN product card uses `<span id="detail_inventory">N,NNN</span>`.
    # This is the authoritative source (the "为您推荐如下商品" recommend tile elsewhere
    # on the page shows a DIFFERENT product's stock and must be ignored).
    inv_m = re.search(
        r'id="detail_inventory"[^>]*>\s*([\d,]+)\s*</span>', html,
    )
    if inv_m:
        qty = _parse_int(inv_m.group(1))
        if qty is not None:
            extracted["stock_now_qty"] = qty
            extracted["stock_total"] = qty
            extracted["site_detail_inventory"] = inv_m.group(1).strip()

    # MOQ — ONEYAC exposes two values that look like MOQ:
    #   • 起订量 (dynamicOrderMinNum)  — minimum click-to-order quantity
    #   • 最小包 (minPack)             — full reel / tube / pack size
    # When the two disagree (e.g. 起订量=1 but 最小包=2500 because the part
    # only ships in full 2500-pcs reels), the **effective** MOQ a buyer
    # cannot go below is 最小包. So we take max(起订量, 最小包) as the
    # canonical `min_order_qty`, and keep both raw values as separate fields
    # for downstream inspection.
    order_min = None
    pack_min = None
    moq_m = re.search(r'"dynamicOrderMinNum"\s*:\s*(\d+)', html)
    if moq_m:
        order_min = int(moq_m.group(1))
    else:
        moq_label = re.search(r'<b>\s*起订量\s*[：:]\s*</b>\s*(\d+)', html)
        if moq_label:
            order_min = int(moq_label.group(1))
    pack_m = re.search(r'"minPack"\s*:\s*(\d+)', html)
    if pack_m:
        pack_min = int(pack_m.group(1))
    extracted["site_order_min"] = order_min
    extracted["min_pack_qty"] = pack_min
    candidates = [v for v in (order_min, pack_min) if v]
    if candidates:
        extracted["min_order_qty"] = max(candidates)

    # Lead time — `交期：N天-M天` near the main product card. Captured verbatim.
    lt_m = re.search(
        r'<span>\s*交期\s*[：:]?\s*</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>',
        html,
    )
    if lt_m:
        extracted["site_lead_time"] = lt_m.group(1).strip()
        # Map ONEYAC's "5天-7天" → canonical stock_now_ship_text (this stock
        # IS the immediately-available pool, just with the marketplace's own
        # 5-7 day fulfilment SLA — not a "future stock" concept)
        extracted["stock_now_ship_text"] = f"交期 {extracted['site_lead_time']}"

    # Price tier table — ONEYAC's MAIN product card uses `<div class="detailPri">`
    # with a plain `<table>` (header: 价格梯度 / 单价), NOT the `c-proPri_lst`
    # popover-style table used by similar-product rows below.
    # When out-of-tier-pricing, the table shows `<p class="text-mutedEr">暂无价格</p>`.
    main_block = re.search(
        r'<div[^>]*class="detailPri"[^>]*>(.*?)</div>\s*<!--\s*价格列表end',
        html, re.DOTALL,
    )
    if main_block:
        block_html = main_block.group(1)
        if "暂无价格" in block_html:
            # No price tiers exposed for this product; leave prices empty
            extracted["site_price_state"] = "暂无价格"
        else:
            # Pattern: <td class="td-num">N</td>...<td>￥X.XXX</td>
            tier_rows = re.findall(
                r'<tr[^>]*>\s*<td[^>]*class="td-num"[^>]*>\s*([\d,]+)\s*</td>'
                r'.*?[￥¥]\s*([\d.]+)\s*</td>',
                block_html, re.DOTALL,
            )
            seen = set()
            for qty_str, price_str in tier_rows:
                qty = _parse_int(qty_str)
                price = _parse_float(price_str)
                if qty is None or price is None:
                    continue
                if (qty, price) in seen:
                    continue
                seen.add((qty, price))
                extracted["prices"].append({
                    "min_qty": qty,
                    "unit_price": f"¥{price}",
                    "unit_price_float": price,
                    "currency": "CNY",
                })
            extracted["prices"].sort(key=lambda t: t["min_qty"])
            if extracted["prices"]:
                extracted["unit_price_cny"] = extracted["prices"][0]["unit_price_float"]
    else:
        # Couldn't find the bounded `detailPri` block — leave prices empty.
        # Do NOT fall back to the page-wide `c-proPri_lst` because those are
        # the similar-product popovers and will contaminate this record.
        extracted["site_price_state"] = "main_card_not_found"

    # Description / package from <p> cells near the price tile
    # Pattern: <p>LGA-14 加速计</p>  — the description
    desc_p = re.findall(r'<p[^>]*style="word-break:[^"]*"[^>]*>([^<]+)</p>', html)
    desc_p = [d.strip() for d in desc_p if d.strip() and len(d.strip()) < 200]
    if desc_p:
        # First match is usually this product's description
        d0 = desc_p[0]
        extracted["description_cn"] = d0
        # Package extraction: leading token before space (e.g. "LGA-14 加速计" → "LGA-14")
        m = re.match(r"([A-Z][A-Z0-9\-]+)", d0)
        if m:
            extracted["package"] = m.group(1)

    # Meta description carries category + brand consistently:
    #   "唯样商城提供_<BRAND>_<CATEGORY>_<MPN>，..."
    meta = re.search(r'<meta name="description" content="([^"]+)"', html)
    if meta:
        mc = meta.group(1)
        m = re.search(r"唯样商城提供_([^_]+)_([^_]+)_", mc)
        if m and not extracted["manufacturer"]:
            extracted["manufacturer"] = m.group(1)
        if m and not extracted.get("category_name_cn"):
            extracted["category_name_cn"] = m.group(2)

    # Category from /category/<id>.html anchor
    cat_a = re.search(r'<a[^>]+href="/category/(\d+)\.html"[^>]*>([^<]+)</a>', html)
    if cat_a:
        extracted["category_id"] = cat_a.group(1)
        if not extracted.get("category_name_cn"):
            extracted["category_name_cn"] = cat_a.group(2).strip()

    # Datasheet link
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if "datasheet" in txt or "数据手册" in txt or a["href"].lower().endswith(".pdf"):
            href = a["href"]
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = BASE + href
            extracted["datasheet_url"] = href
            break

    # Image
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "product" in src or "image" in src:
            if src.startswith("//"):
                src = "https:" + src
            extracted["image_url"] = src
            break

    # Spec table: look for label/value <span>...<span>...
    # ONEYAC uses Vue with class="item" rows, each with two spans
    for item in soup.find_all(class_=re.compile(r"item|spec|param|attr", re.I)):
        spans = item.find_all("span", recursive=False)
        if len(spans) == 2:
            k = spans[0].get_text(" ", strip=True).rstrip("：:").strip()
            v = spans[1].get_text(" ", strip=True)
            if k and v and len(k) < 30 and len(v) < 200:
                extracted["parameters"].append({"name": k, "value": v})
    # also try dt/dd
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            k = dt.get_text(" ", strip=True).rstrip("：:").strip()
            v = dd.get_text(" ", strip=True)
            if k and v:
                extracted["parameters"].append({"name": k, "value": v})

    # Promote 封装 to top-level package
    for p in extracted["parameters"]:
        n = (p.get("name") or "").lower()
        if "封装" in p.get("name", "") or "package" in n:
            if not extracted["package"]:
                extracted["package"] = p.get("value")

    # Stock breakdown — emit a row whenever the page exposes ANY usable
    # availability info, NOT only when stock > 0. ONEYAC's typical pattern
    # for out-of-stock items is to show stock=0 alongside MOQ + a factory
    # lead time (e.g. `交期 16W`). Previously we skipped this case because
    # `if stock_now_qty:` is falsy for 0, dropping the MOQ + 交期 from the
    # CSV output even though the JSON had them. Fix:
    #
    #   • stock_now_qty > 0  → 现货 row, quantity=N, ship_text=交期 / 商城SLA
    #   • stock_now_qty == 0 with 交期 or MOQ → 期货 row, quantity=0,
    #     ship_text=交期 (the factory lead time the page advertises),
    #     moq=MOQ. This is interpretation:
    #     OOS + published 交期 means "buyable as factory order".
    #   • neither stock nor 交期 nor MOQ → no row (genuine no_data).
    stock_qty = extracted["stock_now_qty"]
    has_stock = isinstance(stock_qty, int) and stock_qty > 0
    has_lead = bool(extracted.get("site_lead_time"))
    has_moq = bool(extracted.get("min_order_qty"))
    if has_stock:
        ship = extracted.get("stock_now_ship_text") or "唯样商城现货"
        row = {
            "label": "现货",
            "warehouse": "唯样商城",
            "quantity": stock_qty,
            "ship_text": ship,
        }
        if has_moq:
            row["moq"] = extracted["min_order_qty"]
        extracted["stock_breakdown"].append(row)
        if not extracted.get("stock_now_ship_text"):
            extracted["stock_now_ship_text"] = "唯样商城现货"
    elif stock_qty == 0 and (has_lead or has_moq):
        # OOS but page advertises a factory 交期 / MOQ — emit as 期货.
        ship = (f"交期 {extracted.get('site_lead_time')}"
                if has_lead else None)
        row = {
            "label": "期货",
            "warehouse": "唯样商城",
            "quantity": 0,
            "ship_text": ship or "",
        }
        if has_moq:
            row["moq"] = extracted["min_order_qty"]
        extracted["stock_breakdown"].append(row)
        # Mirror onto the canonical future-stock scalars so batch_index's
        # chip-level view (and downstream JOINs) see the lead time too.
        if has_lead and not extracted.get("stock_future_ship_text"):
            extracted["stock_future_ship_text"] = f"交期 {extracted['site_lead_time']}"
        # Don't claim a future quantity — ONEYAC does not publish one.
        # `stock_future_qty` stays None.
        # Also clear the stock_now_ship_text we may have set earlier — it
        # referenced the factory lead time as if it were 现货 SLA, which it
        # is not when stock is 0.
        # NB: `.get(key, default)` returns the stored None when the key
        # exists with value None — must coerce explicitly before .startswith.
        if (extracted.get("stock_now_ship_text") or "").startswith("交期 ") and not has_stock:
            extracted["stock_now_ship_text"] = None

    # Site-native preservation
    extracted["site_title"] = title
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
    search_url = f"{BASE}/search/?keyword={part}"
    record: dict = {
        "query": part,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "oneyac.com",
        "search_url": search_url,
        "output_dir": str(run_dir),
        "method": "playwright_firefox",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    detail_html = ""
    detail_url = None
    detail_title = ""
    try:
        with sync_playwright() as p:
            br = p.firefox.launch(headless=True)
            ctx = br.new_context(user_agent=UA_FF, locale="zh-CN",
                                 viewport={"width": 1440, "height": 1200})
            page = ctx.new_page()
            t0 = time.time()
            print(f"[oneyac] goto search {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            try: page.wait_for_load_state("networkidle", timeout=10000)
            except Exception: pass
            page.wait_for_timeout(3000)
            search_html = page.content()
            search_title = page.title()
            (run_dir / f"{safe}_search.html").write_text(search_html, encoding="utf-8")
            try:
                page.screenshot(path=str(run_dir / f"{safe}_search.png"), full_page=True)
            except Exception: pass
            record["attempts"].append({
                "method": "playwright_firefox", "url": search_url,
                "status": 200, "len": len(search_html),
                "outcome": "ok", "title": search_title,
            })

            # find product anchor /product/<id>.html
            anchors = re.findall(r'href="(/product/\d+\.html)"', search_html)
            anchors = [a for a in anchors if "{{" not in a]
            anchors = sorted(set(anchors))
            print(f"[oneyac] product anchor candidates: {len(anchors)}")
            if not anchors:
                record["status"] = "no_results"
                br.close()
                return record

            detail_url = BASE + anchors[0]
            print(f"[oneyac] detail → {detail_url}")
            record["resolved_product_url"] = detail_url
            page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
            try: page.wait_for_load_state("networkidle", timeout=12000)
            except Exception: pass
            page.wait_for_timeout(4000)
            detail_html = page.content()
            detail_title = page.title()
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
    part = argv[1] if len(argv) > 1 else "LIS2DH12TR"
    out_dir_override = argv[2] if len(argv) > 2 else None
    run_dir = (Path(out_dir_override).resolve() if out_dir_override else make_run_dir(part))
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== ONEYAC scrape: {part} ===")
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
        for k in ("manufacturer_part_number", "manufacturer", "package",
                  "stock_now_qty", "unit_price_cny", "datasheet_url"):
            v = ex.get(k)
            if v is not None and v != "":
                print(f"  {k}: {v}")
        print(f"  prices: {len(ex.get('prices') or [])} tiers")
        print(f"  parameters: {len(ex.get('parameters') or [])} attributes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
