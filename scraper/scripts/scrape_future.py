"""Future Electronics (futureelectronics.com) scraper.

Strategy / cascade:
  1. curl_cffi → blocked by Akamai BMP on the homepage `/` (sec-if-cpt
     interstitial). Non-homepage URLs (e.g. `/productnotfound`, `/search?...`)
     pass through Akamai but the rendered SPA is JS-driven so curl_cffi gets
     only the page shell, not product data.
  2. Playwright with the **Firefox** engine. Akamai resets HTTP/2 streams on
     Chromium's fingerprint (`ERR_HTTP2_PROTOCOL_ERROR`), but Firefox's HTTP/2
     frame ordering is allow-listed and goes through cleanly.

Search URL:
  https://www.futureelectronics.com/search?text=<MPN>&q=<MPN>:searchRelevance
  Returns a result list — possibly multiple close-but-distinct MPN variants
  (e.g. searching `ATXMEGA32E5-ANR` returns `ATXMEGA32E5-AU` + `ATXMEGA32E5-M4U`,
  the variants Future actually stocks; the exact `-ANR` isn't carried).

Per-MPN detail URL (linked from each result row):
  /p/<category-path>/<mpn-lowercase>-<manufacturer-lowercase>-<id>

Detail page fields ("Pricing Section"):
  - Global Stock:  N      ← total visible stock at Future
  - <Region>:      N      ← per-region warehouse stock (APAC site shows Singapore)
  - On Order:      N      ← already-ordered stock
  - Factory Stock: N      ← per user's hint: "Inventory held at our manufacturer's
                            warehouse. Subject to availability and transit time."
                            → maps to 期货/在途
  - Factory Lead Time:    ← e.g. "4 Weeks", shown when Factory Stock > 0

Stock-breakdown mapping onto our canonical schema:
  - stock_now_qty   = Global Stock (Future's own stock, ships immediately)
  - stock_future_qty = Factory Stock (at manufacturer's warehouse)
  - stock_future_ship_text = "Factory Lead Time: <N>" + the official note
  - stock_breakdown rows: one for each disclosed pool — Global, Region, On Order,
    Factory Stock — so a buyer can see the full availability picture.

Note: Future does NOT use the Chinese 现货/期货 wording. We follow the user's
"flexible — keep original site wording, add interpretive notes" rule by storing
the original English labels in `stock_breakdown` and clearly tagging the
interpretation in the summary.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests
from playwright.sync_api import sync_playwright

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
CHANNEL = "FUTURE"
BASE = "https://www.futureelectronics.com"
TOP_N_VARIANTS = 8  # cap on per-MPN detail pages we'll visit

UA_FF = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)


def make_run_dir(part: str) -> Path:
    now = datetime.now()
    safe_part = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    name = (
        f"Test_{safe_part}_{CHANNEL}_"
        f"{now.strftime('%Y%m%d')}_{now.strftime('%H_%M_%S')}"
    )
    run_dir = TEST_ROOT / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def attempt_curl_cffi(url: str) -> dict:
    """Cheap probe — search SPA returns a shell with no product data."""
    rec = {"method": "curl_cffi", "profile": "chrome131", "url": url}
    try:
        s = cf_requests.Session(impersonate="chrome131")
        s.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        r = s.get(url, timeout=15, allow_redirects=True)
        rec["status"] = r.status_code
        rec["len"] = len(r.text)
        if "sec-if-cpt" in r.text.lower():
            rec["outcome"] = "akamai_interstitial"
        elif "ATXMEGA" in r.text or "Product Results" in r.text:
            rec["outcome"] = "unexpected_pass"
        else:
            rec["outcome"] = "spa_shell_only"
    except Exception as exc:
        rec["outcome"] = "exception"
        rec["error"] = str(exc)
    return rec


JS_DETAIL_EXTRACT = r"""
() => {
    const body = (document.body && document.body.innerText) || "";

    // Helper: extract value following a labeled line. The detail page renders
    // labels and values on separate lines/tables. We scan line-by-line.
    const lines = body.split('\n').map(s => s.trim()).filter(Boolean);

    function isSectionHeaderLike(s) {
        // Future renders section dividers as lines like:
        //   "Product Specification Section"
        //   "Pricing Section"
        //   "Product Variant Information section"
        //   "Microchip <MPN> - Product Specification"
        //   "Available Packaging"
        // These are NEVER values; skip them when walking forward from a label.
        if (!s) return true;
        if (/\bSection\b/i.test(s)) return true;
        if (/^Available Packaging$/i.test(s)) return true;
        if (/^Microchip\b.*Product Specification$/i.test(s)) return true;
        if (s === 'Active' && false) return false; // (placeholder — Active is a valid value)
        return false;
    }

    function valueAfter(label, maxAhead=4) {
        // Normalize: strip trailing colon from both line and label before comparing
        const normLabel = label.replace(/[:：]\s*$/, '');
        for (let i = 0; i < lines.length; i++) {
            const lineNorm = lines[i].replace(/[:：]\s*$/, '');
            if (lineNorm === normLabel) {
                // Walk ahead skipping label/duplicate rows.
                // A section header means the original label had no value (the
                // next value belongs to the new section) — return null then.
                for (let j = 1; j <= maxAhead; j++) {
                    const next = lines[i+j];
                    if (!next) continue;
                    if (next === ':' || next === '：') continue;       // standalone colon (label split)
                    if (next.replace(/[:：]\s*$/, '') === normLabel) continue;  // duplicate label
                    if (next.endsWith(':')) continue;                  // another label
                    if (isSectionHeaderLike(next)) return null;        // crossed into a new section → no value
                    return next;
                }
            }
        }
        return null;
    }

    // Title + manufacturer
    const titleH1 = document.querySelector('h1, .pdp-title, .product-title');
    const mpnEl = Array.from(document.querySelectorAll('*')).find(
        el => el.previousElementSibling &&
              (el.previousElementSibling.innerText || '').trim() === 'Manufacturer Part #'
    );

    // Look for h1 with format "<Manufacturer> | <MPN>"
    let titleText = '';
    for (const h1 of document.querySelectorAll('h1')) {
        const t = (h1.innerText || '').trim();
        if (t.includes('|')) { titleText = t; break; }
    }

    // Description line — typically appears between MPN and "<MPN> Datasheet"
    // anchor. Heuristic: pick the line just before the "Datasheet" anchor.
    // Note: the actual Datasheet line uses a NON-BREAKING SPACE (\xa0) before
    // "Datasheet", so match on /Datasheet$/ rather than ' Datasheet'.
    let description = null;
    for (let i = 0; i < lines.length - 1; i++) {
        if (/Datasheet$/.test(lines[i+1]) && lines[i].length > 30 && !lines[i].endsWith(':')) {
            description = lines[i];
            break;
        }
    }

    // Price tiers — look for a quantity/unit-price table near "Quantity" header
    const priceTiers = [];
    const tables = document.querySelectorAll('table');
    for (const tbl of tables) {
        const t = (tbl.innerText || '');
        if (!t.includes('Unit Price')) continue;
        for (const row of tbl.querySelectorAll('tr')) {
            const cells = Array.from(row.querySelectorAll('td')).map(c => (c.innerText||'').trim());
            if (cells.length === 2) {
                const m = cells[0].match(/(\d[\d,]*)/);
                const p = cells[1].match(/[\$€£￥¥]\s*([\d.,]+)/);
                if (m && p) priceTiers.push({min_qty: parseInt(m[1].replace(/,/g,'')), unit_price: parseFloat(p[1].replace(/,/g,''))});
            }
        }
    }
    // Fallback price tier scrape from plain text: lines like "10\n$4.0314"
    if (priceTiers.length === 0) {
        for (let i = 0; i < lines.length - 1; i++) {
            const qty = lines[i].match(/^(\d+(?:,\d{3})*)\+?$/);
            const price = lines[i+1].match(/^[\$€£￥¥]\s*([\d.,]+)$/);
            if (qty && price) {
                priceTiers.push({
                    min_qty: parseInt(qty[1].replace(/,/g,'')),
                    unit_price: parseFloat(price[1].replace(/,/g,'')),
                });
            }
        }
    }

    // Attributes table (key: value pairs)
    const attrs = {};
    for (const tbl of tables) {
        for (const row of tbl.querySelectorAll('tr')) {
            const cells = Array.from(row.querySelectorAll('td, th')).map(c => (c.innerText||'').trim());
            if (cells.length === 2 && cells[0] && cells[1]) {
                const key = cells[0].replace(/[:：]\s*$/, '');
                if (key && !attrs[key]) attrs[key] = cells[1];
            }
        }
    }

    // Available packaging row
    const pkgLine = lines.find(l => /^Qty:\s*[\d,]+\+?\s*\/\s*Unit Price:/i.test(l));

    // Shipping packaging form (Tray / Reel / Tube / etc.) — extracted from the
    // "Package Qty:" line which renders as "N per <FORM>" (NB: real space or
    // non-breaking space). Falls back to the Available Packaging label below.
    let shippingPackaging = null;
    let pkgQty = null;
    // The label preceding the "<N> per <FORM>" line varies ("Package Qty:"
    // on some pages, "Standard Pkg:" on others, or omitted). The line uses
    // non-breaking spaces, so a regex scan over the rendered text is more
    // reliable than a label-based lookup.
    for (const ln of lines) {
        const m = ln.match(/^([\d,]+)\s+per\s+([A-Za-z][A-Za-z\s\-]{0,30}?)$/);
        if (m) {
            pkgQty = ln;
            shippingPackaging = m[2].trim();
            break;
        }
    }
    // Fallback: the line right after "Available Packaging" is often the
    // packaging form name (e.g. just "Tray" or "Reel" on its own line).
    if (!shippingPackaging) {
        const apIdx = lines.indexOf('Available Packaging');
        if (apIdx >= 0 && apIdx + 1 < lines.length) {
            const candidate = lines[apIdx + 1];
            if (/^[A-Z][A-Za-z\s\-]{2,30}$/.test(candidate) && !candidate.endsWith(':')) {
                shippingPackaging = candidate.trim();
            }
        }
    }

    // Site-native note for Factory Stock — captures the literal definition
    // Future shows under the "Factory Stock:" pool. Useful for batch runs
    // to audit our interpretation against the source text.
    let factoryStockSiteNote = null;
    for (let i = 0; i < lines.length; i++) {
        if (lines[i] === "Inventory held at our manufacturer's warehouse. Subject to availability and transit time." ||
            lines[i].startsWith("Inventory held at our manufacturer")) {
            factoryStockSiteNote = lines[i];
            break;
        }
    }

    // Datasheet anchor
    let datasheetUrl = null;
    for (const a of document.querySelectorAll('a[href]')) {
        const t = (a.innerText || '').trim();
        if (t.toLowerCase().includes('datasheet')) {
            datasheetUrl = a.href;
            break;
        }
    }

    // Currency hint: look for $ / SGD / USD etc.
    let currency = null;
    const curMatch = body.match(/\b(SGD|USD|EUR|GBP|CNY|CAD|JPY)\b/);
    if (curMatch) currency = curMatch[1];

    return {
        title: titleText,
        description: description,
        global_stock: valueAfter('Global Stock'),
        region_label: (() => {
            // Find a label-line not "Global Stock"/"On Order"/"Factory Stock"
            // between Global Stock and On Order
            const gsIdx = lines.indexOf('Global Stock');
            const ooIdx = lines.indexOf('On Order:');
            if (gsIdx >= 0 && ooIdx > gsIdx) {
                for (let i = gsIdx + 1; i < ooIdx; i++) {
                    const m = lines[i].match(/^([A-Z][a-zA-Z\s]+):$/);
                    if (m) return m[1];
                }
            }
            return null;
        })(),
        region_stock: (() => {
            const gsIdx = lines.indexOf('Global Stock');
            const ooIdx = lines.indexOf('On Order:');
            if (gsIdx >= 0 && ooIdx > gsIdx) {
                for (let i = gsIdx + 1; i < ooIdx; i++) {
                    const m = lines[i].match(/^([A-Z][a-zA-Z\s]+):$/);
                    if (m && lines[i+1] && /^[\d,]+$/.test(lines[i+1])) return lines[i+1];
                }
            }
            return null;
        })(),
        on_order: valueAfter('On Order:'),
        factory_stock: valueAfter('Factory Stock:'),
        factory_lead_time: valueAfter('Factory Lead Time:'),
        min_order: valueAfter('Minimum Order:'),
        multiple_of: valueAfter('Multiple Of:'),
        date_code: valueAfter('Date Code:'),
        part_status: valueAfter('Part Status:'),
        hts_code: valueAfter('HTS Code:'),
        eccn: valueAfter('ECCN:'),
        ecad_model: null,
        mfr_name: valueAfter('Mfr. Name:'),
        package_style: attrs['Package Style'] || null,
        shipping_packaging: shippingPackaging,
        package_qty_line: pkgQty,
        available_packaging_line: pkgLine,
        factory_stock_site_note: factoryStockSiteNote,
        prices: priceTiers,
        attributes: attrs,
        currency,
        datasheet_url: datasheetUrl,
    };
}
"""


def _parse_qty(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def attempt_playwright_search(part: str, out_dir: Path) -> dict:
    """Open search results in Firefox, capture variant list (MPN + detail URL)."""
    search_url = f"{BASE}/search?text={part}&q={part}:searchRelevance"
    rec: dict = {"method": "playwright_firefox", "url": search_url}
    variants: list[dict] = []
    page_title = None

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx = browser.new_context(
            user_agent=UA_FF,
            locale="en-US",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = ctx.new_page()
        try:
            print(f"[future] goto search {search_url}")
            resp = page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
            rec["status"] = resp.status if resp else None
            page.wait_for_timeout(10_000)
            try:
                page.wait_for_selector("a.product__list--code", timeout=15_000)
            except Exception:
                pass

            page_title = page.title()
            rec["title"] = page_title

            # Save artefacts
            (out_dir / f"{_safe(part)}_search.html").write_text(
                page.content(), encoding="utf-8"
            )
            try:
                page.screenshot(
                    path=str(out_dir / f"{_safe(part)}_search.png"), full_page=True
                )
            except Exception:
                pass

            # Extract MPN + detail URL for each result row
            search_links = page.evaluate(
                "() => Array.from(document.querySelectorAll('a.product__list--code'))"
                ".map(a => ({mpn: (a.innerText||'').trim(), href: a.href}))"
            )
            # Dedupe by mpn — sometimes the same anchor appears twice (mobile/desktop layout)
            seen = set()
            for s in search_links or []:
                mpn = (s.get("mpn") or "").strip()
                href = s.get("href")
                if not mpn or not href or mpn in seen:
                    continue
                seen.add(mpn)
                variants.append({"mpn": mpn, "detail_url": href})

            rec["outcome"] = "ok" if variants else "no_variants_found"

            # Now visit each detail page in the same context
            for v in variants[:TOP_N_VARIANTS]:
                print(f"[future]   detail {v['mpn']}: {v['detail_url']}")
                try:
                    page.goto(v["detail_url"], wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(8_000)
                    # Wait for Pricing Section to appear
                    try:
                        page.wait_for_function(
                            "() => document.body && document.body.innerText.includes('Factory Stock')",
                            timeout=15_000,
                        )
                    except Exception:
                        pass

                    detail_dir = out_dir / _safe(v["mpn"])
                    detail_dir.mkdir(exist_ok=True)
                    (detail_dir / f"{_safe(v['mpn'])}_product.html").write_text(
                        page.content(), encoding="utf-8"
                    )
                    try:
                        page.screenshot(
                            path=str(detail_dir / f"{_safe(v['mpn'])}_product.png"),
                            full_page=True,
                        )
                    except Exception:
                        pass
                    v["raw"] = page.evaluate(JS_DETAIL_EXTRACT)
                    v["final_url"] = page.url
                except Exception as exc:
                    v["error"] = str(exc)
        except Exception as exc:
            rec["outcome"] = "exception"
            rec["error"] = str(exc)
        finally:
            browser.close()

    rec["variants"] = variants
    rec["variants_count"] = len(variants)
    return rec


def normalize_variant(v: dict) -> dict:
    """Map one Future product-detail extract → canonical per-variant record."""
    mpn = v.get("mpn")
    raw = v.get("raw") or {}
    attrs = raw.get("attributes") or {}

    # Parse manufacturer from title "Microchip | ATXMEGA32E5-AU"
    title = raw.get("title") or ""
    manufacturer = None
    if "|" in title:
        manufacturer = title.split("|", 1)[0].strip()
    manufacturer = manufacturer or raw.get("mfr_name") or attrs.get("Mfr. Name")

    global_stock = _parse_qty(raw.get("global_stock"))
    region_stock = _parse_qty(raw.get("region_stock"))
    region_label = raw.get("region_label")
    on_order = _parse_qty(raw.get("on_order"))
    factory_stock = _parse_qty(raw.get("factory_stock"))
    factory_lead_time = raw.get("factory_lead_time")  # e.g. "4 Weeks"

    # Build the canonical stock_breakdown
    stock_breakdown: list[dict] = []
    # Future's own "Global Stock" = stock_now (ships immediately when > 0)
    stock_breakdown.append({
        "label": "Global Stock",
        "warehouse": "Future Electronics (global)",
        "quantity": global_stock,
        "ship_text": "Ships immediately" if global_stock and global_stock > 0 else "Out of stock at Future",
        "note": "Future's own inventory visible globally — interpretation: this is 现货 (in-stock).",
    })
    if region_label and region_stock is not None:
        stock_breakdown.append({
            "label": f"{region_label} stock",
            "warehouse": f"Future Electronics ({region_label})",
            "quantity": region_stock,
            "ship_text": f"Local {region_label} warehouse" if region_stock > 0 else "Out of stock at this region",
            "note": "Per-region slice of Global Stock (this scraper used the APAC site).",
        })
    if on_order is not None:
        stock_breakdown.append({
            "label": "On Order",
            "warehouse": "Future Electronics (incoming)",
            "quantity": on_order,
            "ship_text": "On order — already reserved",
            "note": "Stock Future has on order; not immediately shippable to new orders.",
        })
    if factory_stock is not None:
        # Prefer the literal site note captured from the page when available
        site_note = raw.get("factory_stock_site_note") or (
            "Inventory held at our manufacturer's warehouse. "
            "Subject to availability and transit time."
        )
        stock_breakdown.append({
            "label": "Factory Stock",
            "warehouse": f"Manufacturer warehouse ({manufacturer or '?'})",
            "quantity": factory_stock,
            "ship_text": (
                f"Factory Lead Time: {factory_lead_time}" if factory_lead_time
                else "Subject to transit time"
            ),
            "note": (
                f"Site definition: '{site_note}'  "
                "Interpretation: 在途/期货 (futures / in-transit from factory)."
            ),
        })

    # Canonical scalars for cross-channel comparison
    stock_now_qty = global_stock
    stock_now_ship_text = (
        "Ships immediately (Future global stock)"
        if (global_stock and global_stock > 0) else None
    )
    stock_future_qty = factory_stock
    stock_future_ship_text = (
        f"Factory Lead Time: {factory_lead_time}" if (factory_stock and factory_lead_time)
        else (factory_lead_time if factory_stock else None)
    )

    # Price tiers
    prices = []
    for t in raw.get("prices") or []:
        if t.get("min_qty") is not None and t.get("unit_price") is not None:
            prices.append({
                "min_qty": t["min_qty"],
                "unit_price": t["unit_price"],
                "currency": raw.get("currency"),
            })

    # Parameters (from attributes table) — skip non-spec rows
    skip_keys = {
        "Mfr. Name", "Standard Pkg", "Date Code",
        "Minimum Order", "Multiple Of",
        "Global Stock", "On Order", "Factory Stock", "Factory Lead Time",
        "Singapore", "Total",
    }
    parameters = []
    for k, val in attrs.items():
        if k in skip_keys:
            continue
        parameters.append({"name": k, "value": val})

    return {
        "manufacturer_part_number": mpn,
        "manufacturer": manufacturer,
        "description_en": raw.get("description") or attrs.get("Description"),
        "package": raw.get("package_style") or attrs.get("Package Style"),
        "shipping_packaging": raw.get("shipping_packaging"),
        "package_qty_line": raw.get("package_qty_line"),
        "part_status": raw.get("part_status") or attrs.get("Part Status"),
        "date_code": raw.get("date_code") or attrs.get("Date Code"),
        "min_buy_number": _parse_qty(raw.get("min_order")),
        "min_order_multiplier": _parse_qty(raw.get("multiple_of")),
        "hts_code": raw.get("hts_code") or attrs.get("HTS Code"),
        "eccn": raw.get("eccn") or attrs.get("ECCN"),
        # Canonical 现货 / 期货 scalars + breakdown
        "stock_total": (global_stock or 0) + (factory_stock or 0),
        "stock_now_qty": stock_now_qty,
        "stock_now_ship_text": stock_now_ship_text,
        "stock_future_qty": stock_future_qty,
        "stock_future_ship_text": stock_future_ship_text,
        "stock_breakdown": stock_breakdown,
        # Site-native fields (preserved per user's "follow site original wording" rule)
        "site_global_stock": global_stock,
        "site_region_label": region_label,
        "site_region_stock": region_stock,
        "site_on_order": on_order,
        "site_factory_stock": factory_stock,
        "site_factory_lead_time": factory_lead_time,
        # Pricing
        "currency": raw.get("currency"),
        "unit_price": prices[0]["unit_price"] if prices else None,
        "prices": prices,
        # Spec
        "parameters": parameters,
        # Links / media
        "detail_url": v.get("final_url") or v.get("detail_url"),
        "datasheet_url": raw.get("datasheet_url"),
        # Packaging summary
        "available_packaging_line": raw.get("available_packaging_line"),
    }


def _description_from_title(title: str, mpn: str, manufacturer: str | None) -> str | None:
    """Title is 'Microchip | ATXMEGA32E5-AU'; the longer description is elsewhere
    on the page. We only have title here, so return None and rely on attrs."""
    return None


def quality_for(variants: list[dict]) -> str:
    if not variants:
        return "none"
    high_count = 0
    for v in variants:
        has_part = bool(v.get("manufacturer_part_number"))
        has_stock_info = (
            v.get("stock_now_qty") is not None
            or v.get("stock_future_qty") is not None
        )
        has_price = bool(v.get("prices"))
        if has_part and has_stock_info and has_price:
            high_count += 1
    if high_count == len(variants):
        return "high"
    if high_count > 0:
        return "medium"
    return "low"


def write_parent_summary(root_dir: Path, query: str, parent_rec: dict) -> Path:
    """Cross-variant overview table for the parent Test_<query>_FUTURE_<ts>/ folder."""
    md: list[str] = []
    md.append(f"# Future Electronics search-results summary — {query}")
    md.append("")
    md.append(f"- **Search query:** `{query}`")
    md.append(f"- **Search URL:** {parent_rec.get('search_url')}")
    md.append(f"- **Channel:** {CHANNEL} (futureelectronics.com)")
    md.append(f"- **Scraped at (UTC):** {parent_rec.get('scraped_at_utc')}")
    md.append(f"- **Variants matched:** {len(parent_rec.get('variants') or [])}")
    md.append(f"- **Method:** {parent_rec.get('method')}")
    md.append("")
    md.append("> **Note on Future Electronics' stock model:**")
    md.append("> Future surfaces two distinct stock pools per product page —")
    md.append("> **Global Stock** = inventory at Future's own warehouses (interpretation: 现货 / ships immediately when > 0); and")
    md.append("> **Factory Stock** = inventory at the manufacturer's warehouse (Future's own definition: \"Inventory held at our manufacturer's warehouse. Subject to availability and transit time.\" — interpretation: 期货/在途, subject to **Factory Lead Time**).")
    md.append("> There may also be regional (e.g. Singapore) stock and an On Order pool — both shown in each variant's per-variant summary.")
    md.append("")
    md.append("## Variants")
    md.append("")
    md.append(
        "| # | MPN | Manufacturer | Package | Global Stock (现货) | Factory Stock (期货/在途) | Factory Lead Time | Unit price | Currency | MOQ | Subfolder |"
    )
    md.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, v in enumerate(parent_rec.get("variants") or [], 1):
        moq_str = (
            f"{v.get('min_buy_number','?')} / mult {v.get('min_order_multiplier','?')}"
            if v.get("min_buy_number") or v.get("min_order_multiplier") else ""
        )
        md.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | `{}/` |".format(
                i,
                v.get("manufacturer_part_number", "?"),
                v.get("manufacturer", ""),
                v.get("package", "") or "",
                _fmt(v.get("site_global_stock")),
                _fmt(v.get("site_factory_stock")),
                v.get("site_factory_lead_time", "") or "",
                v.get("unit_price", "") or "",
                v.get("currency", "") or "",
                moq_str,
                v.get("subfolder", ""),
            )
        )
    md.append("")
    out = root_dir / "parent_summary.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out


def _fmt(v):
    if v is None or v == "":
        return ""
    if isinstance(v, int):
        return f"{v:,}"
    try:
        return f"{int(v):,}"
    except (ValueError, TypeError):
        return str(v)


def scrape(part: str, run_dir: Path) -> dict:
    search_url = f"{BASE}/search?text={part}&q={part}:searchRelevance"
    record: dict = {
        "query": part,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "futureelectronics.com",
        "search_url": search_url,
        "output_dir": str(run_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
        "variants": [],
    }

    # 1. curl_cffi probe (expected: SPA shell only)
    cf = attempt_curl_cffi(search_url)
    record["attempts"].append(cf)
    print(f"[future] curl_cffi: {cf.get('outcome')} (status={cf.get('status')})")

    # 2. Playwright Firefox
    pw = attempt_playwright_search(part, run_dir)
    record["attempts"].append({k: v for k, v in pw.items() if k != "variants"})
    print(
        f"[future] playwright_firefox: {pw.get('outcome')} "
        f"variants={pw.get('variants_count')}"
    )

    raw_variants = pw.get("variants") or []
    if not raw_variants:
        record["status"] = "no_results"
        return record

    # Each Future variant has its own product detail page with full data —
    # mirror the LCSC v3 pattern: parent folder with per-variant subfolders,
    # each carrying a complete <MPN>.json + <MPN>_summary.md.
    record["method"] = "playwright_firefox"
    record["resolved_product_url"] = raw_variants[0].get("detail_url")
    qualities = []

    for v in raw_variants[:TOP_N_VARIANTS]:
        mpn = v.get("mpn") or "(unknown)"
        safe_mpn = _safe(mpn)
        variant_dir = run_dir / safe_mpn
        variant_dir.mkdir(exist_ok=True)

        # Move the per-MPN HTML / screenshot we already wrote into the subfolder
        # (already inside variant_dir from attempt_playwright_search)

        if not v.get("raw"):
            variant_rec = {
                "query_mpn": part,
                "manufacturer_part_number": mpn,
                "channel": CHANNEL,
                "source": "futureelectronics.com",
                "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
                "method": "playwright_firefox",
                "paywall": "none",
                "status": "error",
                "error": v.get("error", "no_data"),
                "subfolder": safe_mpn,
                "attempts": [{"method": "playwright_firefox", "url": v.get("detail_url"), "outcome": "error"}],
                "data_quality": "none",
                "detail_url": v.get("detail_url"),
            }
            record["variants"].append(variant_rec)
            (variant_dir / f"{safe_mpn}.json").write_text(
                json.dumps(variant_rec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            qualities.append("none")
            continue

        extracted = normalize_variant(v)
        variant_rec = {
            "query_mpn": part,
            "manufacturer_part_number": mpn,
            "channel": CHANNEL,
            "source": "futureelectronics.com",
            "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
            "method": "playwright_firefox",
            "paywall": "none",
            "status": "ok",
            "subfolder": safe_mpn,
            "resolved_product_url": v.get("final_url") or v.get("detail_url"),
            "attempts": [{"method": "playwright_firefox", "url": v.get("detail_url"), "status": 200, "outcome": "ok"}],
            "extracted": extracted,
        }
        # Quality per-variant
        if extracted.get("manufacturer_part_number") and extracted.get("prices") and (
            extracted.get("site_global_stock") is not None or extracted.get("site_factory_stock") is not None
        ):
            variant_rec["data_quality"] = "high"
        elif extracted.get("manufacturer_part_number"):
            variant_rec["data_quality"] = "medium"
        else:
            variant_rec["data_quality"] = "low"
        qualities.append(variant_rec["data_quality"])

        (variant_dir / f"{safe_mpn}.json").write_text(
            json.dumps(variant_rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_summary(variant_rec, variant_dir, safe_mpn)

        # Lift a few fields onto the parent variants list for the overview table
        parent_v = {
            "manufacturer_part_number": mpn,
            "subfolder": safe_mpn,
            "manufacturer": extracted.get("manufacturer"),
            "package": extracted.get("package"),
            "site_global_stock": extracted.get("site_global_stock"),
            "site_factory_stock": extracted.get("site_factory_stock"),
            "site_factory_lead_time": extracted.get("site_factory_lead_time"),
            "unit_price": extracted.get("unit_price"),
            "currency": extracted.get("currency"),
            "min_buy_number": extracted.get("min_buy_number"),
            "min_order_multiplier": extracted.get("min_order_multiplier"),
        }
        record["variants"].append(parent_v)

    # Parent quality = best variant quality
    if "high" in qualities:
        record["data_quality"] = "high"
    elif "medium" in qualities:
        record["data_quality"] = "medium"
    elif "low" in qualities:
        record["data_quality"] = "low"
    record["status"] = "ok"
    return record


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "ATXMEGA32E5-ANR"
    out_dir_override = argv[2] if len(argv) > 2 else None
    if out_dir_override:
        run_dir = Path(out_dir_override).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = make_run_dir(part)
    print(f"=== FUTURE Electronics scrape: {part} ===")
    print(f"output folder: {run_dir}")

    rec = scrape(part, run_dir)
    safe = _safe(part)
    out = run_dir / f"{safe}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    parent_md = write_parent_summary(run_dir, part, rec)

    print()
    print(f"Wrote {out}")
    print(f"Wrote {parent_md}")
    print(
        f"status: {rec.get('status')}  method: {rec.get('method')}  "
        f"quality: {rec.get('data_quality')}  variants: {len(rec.get('variants', []))}"
    )
    for v in rec.get("variants") or []:
        print(
            f"  variant {v.get('manufacturer_part_number','?'):<24} "
            f"Global={v.get('site_global_stock')} "
            f"Factory={v.get('site_factory_stock')} "
            f"LeadTime={v.get('site_factory_lead_time','')} "
            f"package={v.get('package','')} "
            f"MOQ={v.get('min_buy_number')}/{v.get('min_order_multiplier')} "
            f"price={v.get('unit_price')} {v.get('currency','')} "
            f"folder=`{v.get('subfolder')}/`"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
