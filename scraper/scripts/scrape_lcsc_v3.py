"""LCSC scraper v3 — szlcsc.com (China site) multi-product.

Strategy:
  1. Search via so.szlcsc.com (`https://so.szlcsc.com/global.html?k=<MPN>`).
  2. Collect every product item link whose URL carries `fromZone=s_s__"<MPN>"`
     (those are the canonical exact-keyword search matches; other links use
     `from=kw` and are recommendation panels — discarded).
  3. For each unique item ID, open `https://item.szlcsc.com/<id>.html` in a
     headless Chromium and read `document.getElementById('__NEXT_DATA__')`.
  4. Pull `props.pageProps.webData` for the data envelope:
       - `productRecord`   → MPN, sku (productCode), encap, packaging, MOQ
       - `gdWarehouseStockNumber` → 现货 (on-hand at Guangdong warehouse)
       - `usableTransitNum` (productRecord) → 在途 (in-transit)
       - `smtStockNumber`  → SMT subsidy stock
       - `totalStockNumber` → headline stock figure
       - `paramList`       → spec parameters
       - `brandVO`         → manufacturer
       - `currentCatalog`  → category
       - `price` (top-level pageProps) → unit price (CNY)
     Plus we record the conventional LCSC SLA strings:
       - 现货 ships "当日17:00前下单" (same-day if ordered before 17:00)
       - 在途 ships "约3个工作日" (about 3 business days)

Folder layout (per the user's "one parent run, per-variant subfolder" request):
  test/scraper_test/Test_<MPN>_LCSC_<YYYYMMDD>_<HH>_<MM>_<SS>/
    ├── parent_summary.md                  # combined view of all variants
    ├── <MPN>.json                         # combined record
    └── <variantMPN>/                      # one subfolder per matched product
         ├── <variantMPN>.json
         ├── <variantMPN>_summary.md
         ├── <variantMPN>_raw_next_data.json
         ├── <variantMPN>_product.html
         └── <variantMPN>_product.png

Usage:
  .venv/Scripts/python.exe scripts/scrape_lcsc_v3.py STM32G030F6P6
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
CHANNEL = "LCSC"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Default SLA strings — overwritten per-variant by DOM extraction when present.
SHIP_TEXT_GD = "最快4小时发货"      # 现货 SLA shown on szlcsc panel
SHIP_TEXT_TRANSIT = "3个工作日内发货"  # 在途 SLA shown on szlcsc panel
SHIP_TEXT_SMT = "SMT扩展库"           # SMT subsidy warehouse label

# JS snippet evaluated on each product page — returns the rendered right-panel
# (price-tier table + stock breakdown) once it has hydrated. Returns null while
# the skeleton is still up.
RIGHT_PANEL_SCRIPT = """
() => {
    const skel = document.querySelectorAll('.animate-pulse').length;
    const body = document.body ? document.body.innerText : '';
    if (!body.includes('梯度') && !body.includes('库存总量')) return null;

    const find_anc = (text) => {
        const el = Array.from(document.querySelectorAll('*'))
            .find(e => e.innerText && e.innerText.trim() === text);
        if (!el) return null;
        let p = el;
        for (let i = 0; i < 8; i++) {
            if (!p.parentElement) break;
            p = p.parentElement;
            const r = p.getBoundingClientRect();
            if (r.width > 200 && r.height > 100 && r.height < 700) return p;
        }
        return null;
    };

    const tier_panel = find_anc('梯度');
    const stock_panel = find_anc('库存总量');
    return {
        skeleton_count: skel,
        tier_text: tier_panel ? tier_panel.innerText : null,
        stock_text: stock_panel ? stock_panel.innerText : null,
    };
}
"""


def parse_price_tiers(tier_text: str) -> list[dict]:
    """Parse the price-tier panel innerText into tier dicts.

    Example tier_text (one tier per pair of lines):
        梯度
        售价
        折合1圆盘
        1+
        ￥4.84
        10+
        ￥4.03
        ...
        1000+
        ￥2.74
        ￥6850         <-- 折合1圆盘 column (trailing)
    """
    if not tier_text:
        return []
    pattern = re.compile(r"(\d+(?:,\d{3})*)\+\s*\n\s*[￥¥]\s*([\d.,]+)")
    tiers = []
    for m in pattern.finditer(tier_text):
        qty = int(m.group(1).replace(",", ""))
        unit = float(m.group(2).replace(",", ""))
        tiers.append({"min_qty": qty, "unit_price_cny": unit})
    return tiers


def parse_stock_panel(stock_text: str) -> dict:
    """Parse stock panel text for the 现货 / 在途 quantities + delivery SLA.

    Example stock_text:
        库存总量
        (单位：个)
        现货：33,839
        最快4小时发货
        在途：
        100,000
        3个工作日内发货
    """
    out = {
        "stock_now_qty": None,
        "stock_now_ship_text": None,
        "stock_transit_qty": None,
        "stock_transit_ship_text": None,
    }
    if not stock_text:
        return out
    # 现货 line + the following non-empty SLA line
    lines = [ln.strip() for ln in stock_text.split("\n") if ln.strip()]
    for i, ln in enumerate(lines):
        m = re.match(r"现货[：:]\s*([\d,]*)$", ln)
        if m:
            qty_str = m.group(1) or (lines[i + 1] if i + 1 < len(lines) else "")
            qty = _parse_qty(qty_str)
            out["stock_now_qty"] = qty
            # SLA line is the next non-numeric line
            for j in range(i + 1, min(i + 4, len(lines))):
                if not re.match(r"^[\d,]+$", lines[j]) and not lines[j].startswith("现货"):
                    out["stock_now_ship_text"] = lines[j]
                    break
        m = re.match(r"在途[：:]\s*([\d,]*)$", ln)
        if m:
            qty_str = m.group(1) or (lines[i + 1] if i + 1 < len(lines) else "")
            qty = _parse_qty(qty_str)
            out["stock_transit_qty"] = qty
            for j in range(i + 1, min(i + 4, len(lines))):
                if not re.match(r"^[\d,]+$", lines[j]) and not lines[j].startswith("在途"):
                    out["stock_transit_ship_text"] = lines[j]
                    break
    return out


def _parse_qty(s: str):
    s = (s or "").replace(",", "").strip()
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


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


def find_search_matches(page, mpn: str) -> list[dict]:
    """Return [{item_id, url}] for products that are actual keyword matches.

    LCSC tags real search-result links with `fromZone=s_s__"<MPN>"`. In the
    rendered HTML this becomes `fromZone=s_s__%2522<MPN>%2522` (the quotes
    are double-encoded). Recommendation/cross-sell links use `from=kw` and
    are discarded.

    Strategy: any href that contains the substring `s_s__` is a keyword-
    match link. Dedupe by item ID. The MPN may not appear *inside* the URL
    after encoding mangling, but `s_s__` always survives.
    """
    hrefs: list[str] = page.eval_on_selector_all(
        "a[href*='item.szlcsc.com']",
        "els => els.map(e => e.getAttribute('href') || '')",
    )

    matches: dict[str, str] = {}
    for h in hrefs:
        if not h or "s_s__" not in h:
            continue
        m = re.search(r"/(\d+)\.html", h)
        if not m:
            continue
        item_id = m.group(1)
        full = h if h.startswith("http") else f"https://item.szlcsc.com{h}"
        matches.setdefault(item_id, full)

    return [{"item_id": iid, "url": url} for iid, url in matches.items()]


def scrape_product(page, item_id: str, url: str, out_dir: Path) -> dict:
    """Open one szlcsc product page and pull both the SSR __NEXT_DATA__ and
    the hydrated right-panel DOM (price tiers + stock breakdown).

    The right-panel content is loaded client-side and only appears in modern
    headless Chrome (--headless=new); legacy headless leaves an animate-pulse
    skeleton in its place.
    """
    print(f"[lcsc/szlcsc] goto item {item_id}: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    # Poll for the right panel to hydrate (skeleton replaced by real content).
    # Most loads finish under 3 s in --headless=new; cap at 25 s.
    right_panel = None
    for _ in range(25):
        page.wait_for_timeout(1_000)
        right_panel = page.evaluate(RIGHT_PANEL_SCRIPT)
        if right_panel and (right_panel.get("tier_text") or right_panel.get("stock_text")):
            break

    nd = page.evaluate(
        "() => { const el = document.getElementById('__NEXT_DATA__'); "
        "return el ? JSON.parse(el.textContent) : null; }"
    )

    html = page.content()
    (out_dir / "_product.html").write_text(html, encoding="utf-8")
    try:
        page.screenshot(path=str(out_dir / "_product.png"), full_page=True)
    except Exception:
        pass

    if not nd:
        return {
            "item_id": item_id,
            "item_url": page.url,
            "status": "no_next_data",
            "right_panel": right_panel,
        }

    (out_dir / "_raw_next_data.json").write_text(
        json.dumps(nd, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    try:
        pp = nd["props"]["pageProps"]
    except (KeyError, TypeError):
        return {
            "item_id": item_id, "item_url": page.url,
            "status": "next_data_shape_unexpected",
            "right_panel": right_panel,
        }

    return {
        "item_id": item_id,
        "item_url": page.url,
        "status": "ok",
        "next_props": pp,
        "right_panel": right_panel,
    }


def normalize(pp: dict, item_id: str, item_url: str, right_panel: dict | None = None) -> dict:
    """Map szlcsc pageProps + rendered right-panel → stable business schema.

    SSR (`webData`) gives stock + spec; the right-panel DOM gives tier
    prices and the human-facing shipping SLA strings.
    """
    wd = pp.get("webData") or {}
    pr = wd.get("productRecord") or {}
    brand = wd.get("brandVO") or {}
    catalog = wd.get("currentCatalog") or {}
    params = wd.get("paramList") or []
    pdf = wd.get("pdfFileDetailVO") or {}
    file_type_list = pr.get("fileTypeVOList") or []

    # Stock breakdown (matches the screenshot panel layout)
    gd_stock = wd.get("gdWarehouseStockNumber") or 0
    js_stock = wd.get("jsWarehouseStockNumber") or 0
    smt_stock = wd.get("smtStockNumber") or 0
    total_stock = wd.get("totalStockNumber") or 0
    transit = pr.get("usableTransitNum") or 0
    display_transit = pr.get("isDisplayUsableTransitNum")

    # Prefer DOM-extracted shipping SLA strings (they're what the user sees).
    dom_stock = parse_stock_panel((right_panel or {}).get("stock_text")) if right_panel else {}
    ship_now = dom_stock.get("stock_now_ship_text") or SHIP_TEXT_GD
    ship_transit = dom_stock.get("stock_transit_ship_text") or SHIP_TEXT_TRANSIT

    stock_breakdown = []
    if gd_stock:
        stock_breakdown.append({
            "label": "现货",
            "warehouse": "广东仓",
            "quantity": gd_stock,
            "ship_text": ship_now,
        })
    if js_stock:
        stock_breakdown.append({
            "label": "现货",
            "warehouse": "江苏仓",
            "quantity": js_stock,
            "ship_text": ship_now,
        })
    if display_transit and transit:
        stock_breakdown.append({
            "label": "在途",
            "warehouse": "在途仓",
            "quantity": transit,
            "ship_text": ship_transit,
        })
    if smt_stock and smt_stock != gd_stock:
        stock_breakdown.append({
            "label": "SMT扩展库",
            "warehouse": "SMT扩展库",
            "quantity": smt_stock,
            "ship_text": SHIP_TEXT_SMT,
        })

    # Datasheet URL
    datasheet_url = None
    if pdf.get("fileUrl"):
        datasheet_url = pdf["fileUrl"]
    else:
        for ft in file_type_list:
            url = ft.get("fileUrl") if isinstance(ft, dict) else None
            if url and ".pdf" in url.lower():
                datasheet_url = url
                break

    # Headline 现货/期货 scalars for cross-channel uniformity (matches Digikey schema).
    # On LCSC, "future stock" is the in-transit pool (在途).
    stock_now_qty = gd_stock + js_stock
    stock_now_ship_text = ship_now if stock_now_qty else None
    stock_future_qty = transit if (display_transit and transit) else 0
    stock_future_ship_text = ship_transit if stock_future_qty else None

    return {
        "lcsc_part_number": pr.get("productCode"),  # e.g. C724040
        "lcsc_product_id": pr.get("productId") or item_id,
        "item_url": item_url,
        "manufacturer_part_number": pr.get("productModel"),  # e.g. STM32G030F6P6TR
        "manufacturer": brand.get("brandName"),
        "manufacturer_id": brand.get("brandId"),
        "description_cn": pr.get("productName"),
        "description_intro_cn": pr.get("remark"),
        "package": pr.get("encapsulationModel"),
        "category_name_cn": catalog.get("catalogName") or pr.get("productType"),
        "category_name_en": catalog.get("catalogNameEn"),
        "category_id": catalog.get("catalogId"),
        "stock_total": total_stock,
        "stock_gd_warehouse": gd_stock,
        "stock_js_warehouse": js_stock,
        "stock_smt": smt_stock,
        "stock_transit": transit if display_transit else 0,
        "stock_now_qty": stock_now_qty,
        "stock_now_ship_text": stock_now_ship_text,
        "stock_future_qty": stock_future_qty,
        "stock_future_ship_text": stock_future_ship_text,
        "stock_breakdown": stock_breakdown,
        "product_cycle": pr.get("productCycle"),
        "has_stock_now": pr.get("hasStockNow") == "yes",
        "is_normally_stocking": pr.get("productStockStatus") == "yes",
        "product_arrange": pr.get("productArrange"),  # 编带 / 管装 / 散料
        "min_buy_number": pr.get("minBuyNumber"),
        "min_whole_number": pr.get("minWholeNumber"),
        "min_packet_unit": pr.get("productMinEncapsulationUnit"),
        "min_packet_number": pr.get("productMinEncapsulationNumber"),
        "encap_price": pr.get("encaptionPrice"),
        "unit_price_cny": pp.get("price"),
        "is_rohs": wd.get("rohsLabal"),
        "is_hot": wd.get("isHot"),
        "datasheet_url": datasheet_url,
        "image_url": pr.get("breviaryImageUrl"),
        "weight_kg": pr.get("productWeight"),
        "prices": _build_prices(wd, right_panel),
        "parameters": [
            {
                "code": p.get("parameterCode") or p.get("paramCode"),
                "name_cn": p.get("parameterName") or p.get("paramName"),
                "name_en": p.get("paramNameEn"),
                "value": p.get("parameterValue") or p.get("paramValue"),
                "value_detail": p.get("parameterDetailValue") or p.get("paramValueEn"),
            }
            for p in params if isinstance(p, dict)
        ],
        "recently_sales_count": wd.get("recentlySalesCount"),
    }


def _build_prices(wd: dict, right_panel: dict | None) -> list[dict]:
    """Tier prices live in the rendered right-panel DOM, not in SSR.

    The full tier table is hydrated client-side once the price-panel React
    component mounts. We pull it from the right_panel.tier_text string and
    parse `(N+, ¥unit)` pairs out of it. Falls back to SSR cutover only.
    """
    if right_panel and right_panel.get("tier_text"):
        tiers = parse_price_tiers(right_panel["tier_text"])
        if tiers:
            return tiers
    # Fallback (SSR-only) — incomplete but better than nothing
    tiers: list[dict] = []
    enc_qty = wd.get("limitNumberEncapPrice")
    enc_price = wd.get("limitNumberPrice")
    if enc_qty:
        tiers.append({
            "min_qty": enc_qty,
            "unit_price_cny": enc_price,
            "note": "limitNumberEncapPrice cutover (SSR only)",
        })
    return tiers


def quality_for(extracted: dict) -> str:
    has_part = bool(extracted.get("manufacturer_part_number"))
    has_stock = extracted.get("stock_total") is not None
    has_breakdown = bool(extracted.get("stock_breakdown"))
    has_params = bool(extracted.get("parameters"))
    if has_part and has_breakdown and has_params:
        return "high"
    if has_part and (has_stock or has_breakdown):
        return "medium"
    if has_part:
        return "low"
    return "none"


def write_parent_summary(root_dir: Path, mpn: str, parent_rec: dict) -> Path:
    md = []
    md.append(f"# LCSC search-results summary — {mpn}")
    md.append("")
    md.append(f"- **Search query:** `{mpn}`")
    md.append(f"- **Search URL:** {parent_rec.get('search_url')}")
    md.append(f"- **Channel:** {CHANNEL} (szlcsc.com)")
    md.append(f"- **Scraped at (UTC):** {parent_rec.get('scraped_at_utc')}")
    md.append(f"- **Matches found:** {len(parent_rec.get('variants', []))}")
    md.append("")
    md.append("## Variants")
    md.append("")
    md.append("| # | MPN | LCSC SKU | Packaging | 现货 (GD) | 在途 | SMT库 | Price (CNY) | Status | Subfolder |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, v in enumerate(parent_rec.get("variants", []), 1):
        ex = v.get("extracted") or {}
        md.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | `{}/` |".format(
                i,
                ex.get("manufacturer_part_number", "?"),
                ex.get("lcsc_part_number", "?"),
                ex.get("product_arrange", ""),
                ex.get("stock_gd_warehouse", 0),
                ex.get("stock_transit", 0),
                ex.get("stock_smt", 0),
                ex.get("unit_price_cny", ""),
                v.get("status", ""),
                v.get("subfolder", ""),
            )
        )
    md.append("")
    md.append("## Stock breakdown (per variant)")
    md.append("")
    for v in parent_rec.get("variants", []):
        ex = v.get("extracted") or {}
        md.append(f"### {ex.get('manufacturer_part_number')} ({ex.get('lcsc_part_number')})")
        md.append("")
        breakdown = ex.get("stock_breakdown") or []
        if not breakdown:
            md.append("_no available stock_")
        else:
            md.append("| 类型 | 仓库 | 数量 | 发货时间 |")
            md.append("|---|---|---|---|")
            for b in breakdown:
                md.append(f"| {b['label']} | {b['warehouse']} | {b['quantity']:,} | {b['ship_text']} |")
        md.append("")
    out = root_dir / "parent_summary.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out


def scrape(mpn: str, run_dir: Path) -> dict:
    search_url = f"https://so.szlcsc.com/global.html?k={mpn}"
    parent_rec: dict = {
        "query": mpn,
        "channel": CHANNEL,
        "source": "szlcsc.com",
        "search_url": search_url,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(run_dir),
        "method": "playwright",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
        "variants": [],
    }

    with sync_playwright() as p:
        # `--headless=new` (modern headless) is REQUIRED for szlcsc's right-
        # panel (price tiers + stock SLA panel) to hydrate. Legacy headless
        # is detected by the page and the panel stays a skeleton placeholder.
        browser = p.chromium.launch(
            headless=True,
            args=["--headless=new", "--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA,
                locale="zh-CN",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )
            page = ctx.new_page()

            print(f"[lcsc/szlcsc] goto search {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(2_000)

            # Persist the search page for forensics
            (run_dir / "_search.html").write_text(page.content(), encoding="utf-8")
            try:
                page.screenshot(path=str(run_dir / "_search.png"), full_page=True)
            except Exception:
                pass

            matches = find_search_matches(page, mpn)
            parent_rec["attempts"].append({
                "method": "playwright",
                "url": search_url,
                "status": 200,
                "outcome": f"{len(matches)} keyword matches",
            })
            print(f"[lcsc/szlcsc] found {len(matches)} keyword-tagged matches")

            if not matches:
                parent_rec["status"] = "no_matches"
                return parent_rec

            # Scrape each variant into its own subfolder, then collapse into one
            for idx, m in enumerate(matches, 1):
                print(f"[lcsc/szlcsc] === variant {idx}/{len(matches)} item={m['item_id']} ===")
                # Provisional folder while we don't know the MPN yet
                tmp_dir = run_dir / f"_tmp_{m['item_id']}"
                tmp_dir.mkdir(parents=True, exist_ok=True)

                try:
                    res = scrape_product(page, m["item_id"], m["url"], tmp_dir)
                except Exception as exc:
                    # Per-variant isolation: one bad page must not abort the batch.
                    print(f"[lcsc/szlcsc] variant {m['item_id']} raised: {exc}")
                    variant_rec = {
                        "item_id": m["item_id"],
                        "item_url": m["url"],
                        "channel": CHANNEL,
                        "source": "szlcsc.com",
                        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
                        "method": "playwright",
                        "paywall": "none",
                        "status": "exception",
                        "error": str(exc),
                        "subfolder": tmp_dir.name,
                        "attempts": [{"method": "playwright", "url": m["url"], "outcome": "exception", "error": str(exc)}],
                        "data_quality": "none",
                    }
                    parent_rec["variants"].append(variant_rec)
                    continue

                if res.get("status") != "ok":
                    # Still keep the folder + record the failure
                    variant_rec = {
                        "item_id": m["item_id"],
                        "item_url": m["url"],
                        "channel": CHANNEL,
                        "source": "szlcsc.com",
                        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
                        "method": "playwright",
                        "paywall": "none",
                        "status": res.get("status"),
                        "subfolder": tmp_dir.name,
                        "attempts": [{"method": "playwright", "url": m["url"], "outcome": res.get("status")}],
                        "data_quality": "none",
                    }
                    parent_rec["variants"].append(variant_rec)
                    continue

                extracted = normalize(
                    res["next_props"], m["item_id"], res["item_url"],
                    right_panel=res.get("right_panel"),
                )

                # Now rename the tmp folder to the variant MPN
                variant_mpn = extracted.get("manufacturer_part_number") or m["item_id"]
                safe_variant = re.sub(r"[^A-Za-z0-9._-]", "_", variant_mpn)
                final_dir = run_dir / safe_variant
                if final_dir.exists():
                    # Rare: collision — append item id
                    final_dir = run_dir / f"{safe_variant}_{m['item_id']}"
                tmp_dir.rename(final_dir)

                # Re-prefix files in the folder with variant MPN
                for src in final_dir.iterdir():
                    if src.name.startswith("_"):
                        src.rename(final_dir / f"{safe_variant}{src.name}")

                variant_rec = {
                    "item_id": m["item_id"],
                    "item_url": res["item_url"],
                    "channel": CHANNEL,
                    "source": "szlcsc.com",
                    "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
                    "method": "playwright",
                    "paywall": "none",
                    "status": "ok",
                    "subfolder": final_dir.name,
                    "attempts": [{"method": "playwright", "url": m["url"], "status": 200, "outcome": "ok"}],
                    "extracted": extracted,
                }
                variant_rec["data_quality"] = quality_for(extracted)

                # Persist per-variant artefacts
                (final_dir / f"{safe_variant}.json").write_text(
                    json.dumps(variant_rec, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                write_summary(variant_rec, final_dir, safe_variant)

                parent_rec["variants"].append(variant_rec)

        finally:
            try:
                browser.close()
            except Exception:
                pass

    # Aggregate parent quality from variants
    qualities = [v.get("data_quality") for v in parent_rec["variants"]]
    if "high" in qualities:
        parent_rec["data_quality"] = "high"
    elif "medium" in qualities:
        parent_rec["data_quality"] = "medium"
    elif "low" in qualities:
        parent_rec["data_quality"] = "low"
    parent_rec["status"] = "ok" if any(v.get("status") == "ok" for v in parent_rec["variants"]) else "failed"
    return parent_rec


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "STM32G030F6P6"
    # Optional argv[2]: absolute output directory. When the batch driver runs
    # us as a subprocess it pre-computes a deterministic per-MPN folder under
    # BatchTest_<ts>/ and passes it here, so we skip the auto-timestamp path.
    out_dir_override = argv[2] if len(argv) > 2 else None
    if out_dir_override:
        run_dir = Path(out_dir_override).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = make_run_dir(part)
    print(f"=== LCSC scrape v3 (szlcsc.com): {part} ===")
    print(f"output folder: {run_dir}")

    safe_part = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    try:
        rec = scrape(part, run_dir)
    except Exception as exc:
        # Outer safety net so a Playwright launch failure still leaves a record
        # for the batch driver to read.
        rec = {
            "query": part,
            "channel": CHANNEL,
            "source": "szlcsc.com",
            "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
            "output_dir": str(run_dir),
            "status": "exception",
            "error": str(exc),
            "method": "playwright",
            "paywall": "none",
            "attempts": [],
            "data_quality": "none",
            "variants": [],
        }
        out = run_dir / f"{safe_part}.json"
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[lcsc] outer exception: {exc}")
        print(f"Wrote stub record to {out}")
        return 1

    out = run_dir / f"{safe_part}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    parent_md = write_parent_summary(run_dir, part, rec)

    print()
    print(f"Wrote {out}")
    print(f"Wrote {parent_md}")
    print(f"status: {rec.get('status')}  variants: {len(rec.get('variants', []))}  quality: {rec.get('data_quality')}")
    for v in rec.get("variants", []):
        ex = v.get("extracted") or {}
        print(
            f"  - {ex.get('manufacturer_part_number','?')} "
            f"({ex.get('lcsc_part_number','?')}) "
            f"现货GD={ex.get('stock_gd_warehouse',0)} "
            f"在途={ex.get('stock_transit',0)} "
            f"SMT={ex.get('stock_smt',0)} "
            f"包装={ex.get('product_arrange','')} "
            f"price=¥{ex.get('unit_price_cny','?')} "
            f"folder=`{v.get('subfolder')}/`"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
