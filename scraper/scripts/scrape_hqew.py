"""HQEW (华强电子网, hqew.com) scraper.

hqew.com is a B2B *marketplace* — many independent suppliers list the same MPN
with their own (private) stock and pricing. Unlike LCSC/Digikey (one distributor
with one inventory pool), hqew exposes:
  - Top-of-page aggregate "云价格" (cloud price) — the only public price.
  - A table of supplier listings: {supplier, MPN listed, brand, batch code,
    quantity, package, warehouse city, transaction note, listing date}.
  - No per-listing price (suppliers gate prices behind "询价" / quote request).
  - No "在途" (in-transit) concept. Every listing is `现货` by nature — suppliers
    only list inventory they hold.

Strategy:
  1. curl_cffi — KNOWN to fail (jsjiami.com.v7 obfuscated JS challenge).
  2. Playwright `--headless=new` to render the search page, then scrape
     `tr.ec-data` rows for the supplier table.

Stock-breakdown mapping onto the canonical schema:
  - stock_now_qty       = sum of quantities across all supplier listings (capped)
  - stock_now_ship_text = "供应商现货 (具体发货请询价)" (supplier in-stock, ask for quote)
  - stock_future_qty    = None  (hqew has no future/in-transit concept)
  - stock_future_ship_text = None
  - stock_breakdown     = one row per listing, top-N suppliers
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as cf_requests
from playwright.sync_api import sync_playwright

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper_test"
CHANNEL = "HQEW"
SEARCH_BASE = "https://s.hqew.com"
TOP_N_LISTINGS = 5  # cap on supplier rows kept in `stock_breakdown` + JSON (top-5 by stock)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SHIP_TEXT_NOW = "供应商现货 (具体发货请询价)"


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


def attempt_curl_cffi(url: str) -> dict:
    """Cheap probe — always fails on hqew (jsjiami obfuscated JS gate)."""
    rec = {"method": "curl_cffi", "profile": "chrome131", "url": url}
    try:
        s = cf_requests.Session(impersonate="chrome131")
        s.headers.update({"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        r = s.get(url, timeout=15, allow_redirects=True)
        rec["status"] = r.status_code
        rec["len"] = len(r.text)
        if "jsjiami" in r.text or len(r.text) < 5_000:
            rec["outcome"] = "js_challenge"
        else:
            rec["outcome"] = "unexpected_pass"
    except Exception as exc:
        rec["outcome"] = "exception"
        rec["error"] = str(exc)
    return rec


JS_EXTRACT = r"""
() => {
    const body = (document.body && document.body.innerText) || "";

    // Aggregate header
    const cloud = body.match(/云价格[：:]?\s*[￥¥]?\s*([\d.]+)/);
    const total = body.match(/共\s*([\d,]+)\s*条/);

    // Listing rows
    const trs = document.querySelectorAll('tr.ec-data');
    const rows = [];
    trs.forEach(tr => {
        const cells = Array.from(tr.querySelectorAll('td')).map(td =>
            (td.innerText || "").trim()
        );
        const a = tr.querySelector('a[href*="/product/ic_"]');
        const isAd = !!(cells[0] && cells[0].includes('广告'));
        rows.push({cells, href: a ? a.href : null, isAd});
    });

    return {
        cloud_price: cloud ? cloud[1] : null,
        total_listings: total ? total[1].replace(/,/g, '') : null,
        rows,
    };
}
"""


def parse_supplier_row(cells: list[str], href: str | None) -> dict:
    """Map the 12-column hqew listing row → structured dict."""
    def get(i):
        return cells[i] if i < len(cells) else ""

    # Supplier cell can include multi-line badge ("正品" / "原装" / "600条")
    supplier_raw = get(1)
    supplier_lines = [ln.strip() for ln in supplier_raw.split("\n") if ln.strip()]
    supplier_name = supplier_lines[0] if supplier_lines else ""
    supplier_badge = " / ".join(supplier_lines[1:]) if len(supplier_lines) > 1 else None

    # MPN cell can include badge "原装排名" — keep the first line
    mpn_raw = get(3)
    mpn_lines = [ln.strip() for ln in mpn_raw.split("\n") if ln.strip()]
    listed_mpn = mpn_lines[0] if mpn_lines else ""

    qty_raw = get(6)
    # Quantity cell often contains "150000\n\n1 起订" — pull leading int
    qty_match = re.match(r"([\d,]+)", qty_raw.replace(",", ""))
    qty = int(qty_match.group(1)) if qty_match else None
    moq_match = re.search(r"(\d+)\s*起订", qty_raw)
    moq = int(moq_match.group(1)) if moq_match else None

    return {
        "supplier": supplier_name,
        "supplier_badge": supplier_badge,
        "listed_mpn": listed_mpn,
        "brand": get(4),
        "batch_code": get(5),
        "quantity": qty,
        "moq": moq,
        "package": get(7),
        "warehouse_city": get(8),
        "transaction_note": get(9),
        "listing_date": get(10),
        "detail_url": href,
    }


def attempt_playwright(part: str, out_dir: Path) -> dict:
    rec: dict = {"method": "playwright", "url": f"{SEARCH_BASE}/{part}.html"}
    listings: list[dict] = []
    cloud_price = None
    total_listings = None
    page_title = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--headless=new", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        page = ctx.new_page()
        try:
            url = rec["url"]
            print(f"[hqew] goto {url}")
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            rec["status"] = resp.status if resp else None
            page.wait_for_timeout(5_000)
            try:
                page.wait_for_selector("tr.ec-data", timeout=10_000)
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

            data = page.evaluate(JS_EXTRACT)
            cloud_price = data.get("cloud_price")
            total_listings = data.get("total_listings")
            for r in data.get("rows", []):
                if r.get("isAd"):
                    continue
                listings.append(parse_supplier_row(r["cells"], r.get("href")))

            rec["outcome"] = "ok" if listings or total_listings else "no_listings"
        except Exception as exc:
            rec["outcome"] = "exception"
            rec["error"] = str(exc)
        finally:
            browser.close()

    rec["cloud_price"] = cloud_price
    rec["total_listings_count"] = (
        int(total_listings) if total_listings and total_listings.isdigit() else None
    )
    rec["listings_extracted"] = len(listings)
    rec["page_title"] = page_title
    rec["listings"] = listings
    return rec


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _split_brand(brand: str | None) -> tuple[str | None, str | None]:
    """Split 'ST/意法' style brand strings into (EN, CN). Returns (brand, None) if no slash."""
    if not brand:
        return None, None
    if "/" in brand:
        parts = brand.split("/", 1)
        return parts[0].strip(), parts[1].strip()
    return brand, None


def _build_breakdown_row(l: dict) -> dict:
    """One supplier listing → one stock_breakdown row.

    Includes the hqew-specific extra columns (mpn, moq, batch_code, listing_date,
    remark) so `_summary.py` can render them as a wide breakdown table.
    """
    return {
        "label": "现货",
        "warehouse": f"{l.get('supplier','')}（{l.get('warehouse_city') or '未注明'}）",
        "quantity": l.get("quantity"),
        "ship_text": SHIP_TEXT_NOW,
        # Extra columns (rendered by _summary.py when present on any row)
        "mpn": l.get("listed_mpn"),
        "moq": l.get("moq"),
        "batch_code": l.get("batch_code"),
        "listing_date": l.get("listing_date"),
        "remark": l.get("transaction_note"),
    }


def _normalize_variant(variant_mpn: str, variant_listings: list[dict]) -> dict:
    """Build a per-MPN-variant record from its supplier listings.

    Listings are sorted by quantity (descending) before taking the top-N so
    the `stock_breakdown` reflects the highest-stock distributors, not just
    whoever happened to appear first in the search-results HTML order.
    """
    ranked = sorted(variant_listings, key=lambda l: (l.get("quantity") or 0), reverse=True)
    top = ranked[:TOP_N_LISTINGS]
    sum_qty = sum((l.get("quantity") or 0) for l in top)
    breakdown = [_build_breakdown_row(l) for l in top]

    packages = [l.get("package") for l in top if l.get("package")]
    package = max(set(packages), key=packages.count) if packages else None

    brands = [l.get("brand") for l in top if l.get("brand")]
    brand = max(set(brands), key=brands.count) if brands else None
    brand_en, brand_cn = _split_brand(brand)

    return {
        "manufacturer_part_number": variant_mpn,
        "listing_count": len(variant_listings),
        "top_listings_count": len(top),
        "manufacturer": brand_en,
        "manufacturer_cn": brand_cn,
        "package": package,
        "stock_total": sum_qty,
        "stock_now_qty": sum_qty,
        "stock_now_ship_text": SHIP_TEXT_NOW if sum_qty else None,
        "stock_future_qty": None,
        "stock_future_ship_text": None,
        "stock_breakdown": breakdown,
        "listings": top,
    }


def normalize(pw_rec: dict, mpn: str) -> dict:
    """Map hqew search results → cross-channel schema, grouped by listed MPN.

    hqew search is fuzzy: querying `STM32G030F6P6` returns rows for the base
    part AND for `STM32G030F6P6TR` (the tape-and-reel variant). These are
    DIFFERENT MPNs — same die, different ordering/packaging codes — and must
    be tallied separately so a buyer sees per-variant stock instead of an
    inflated combined number.
    """
    listings = pw_rec.get("listings") or []
    cloud_price = pw_rec.get("cloud_price")
    total_count = pw_rec.get("total_listings_count")

    # Group by listed MPN
    by_mpn: dict[str, list[dict]] = {}
    for l in listings:
        m = (l.get("listed_mpn") or "").strip() or "(unknown MPN)"
        by_mpn.setdefault(m, []).append(l)

    # Sort variants by total stock descending (most-listed variant first)
    sorted_variants = sorted(
        by_mpn.items(),
        key=lambda kv: -sum((l.get("quantity") or 0) for l in kv[1]),
    )
    variants = [_normalize_variant(m, ls) for m, ls in sorted_variants]

    # Aggregate combined fields. The top-level `stock_breakdown` reflects the
    # overall top-N suppliers across all MPN variants — cap at TOP_N_LISTINGS
    # globally (not per-variant) so the chip-level view is a clean top-5
    # snapshot regardless of how many variants HQEW's fuzzy search returned.
    all_listings = [l for v in variants for l in v["listings"]]
    all_listings.sort(key=lambda l: (l.get("quantity") or 0), reverse=True)
    all_top = all_listings[:TOP_N_LISTINGS]
    sum_qty_all = sum((l.get("quantity") or 0) for l in all_top)
    combined_breakdown = [_build_breakdown_row(l) for l in all_top]

    # Headline brand/package — most common across all top rows
    packages = [l.get("package") for l in all_top if l.get("package")]
    package = max(set(packages), key=packages.count) if packages else None
    brands = [l.get("brand") for l in all_top if l.get("brand")]
    brand = max(set(brands), key=brands.count) if brands else None
    brand_en, brand_cn = _split_brand(brand)

    # Canonical MPN — the variant with the most listings (NOT necessarily the query)
    canonical_mpn = variants[0]["manufacturer_part_number"] if variants else mpn

    out: dict = {
        "manufacturer_part_number": canonical_mpn,
        "query_mpn": mpn,
        "manufacturer": brand_en,
        "manufacturer_cn": brand_cn,
        "package": package,
        # Per-variant breakdown (NEW — addresses the STM/STM-TR mixup)
        "variants_count": len(variants),
        "variants": variants,
        # Aggregate (across all variants) — kept for backward compat with
        # cross-channel comparison code that doesn't care about MPN sub-types.
        "stock_total": sum_qty_all,
        "stock_now_qty": sum_qty_all,
        "stock_now_ship_text": SHIP_TEXT_NOW if sum_qty_all else None,
        "stock_future_qty": None,
        "stock_future_ship_text": None,
        "stock_breakdown": combined_breakdown,
        # Pricing — only the public aggregate "云价格" is visible; per-supplier
        # prices require login + 询价. Render it as a single tier.
        "unit_price_cny": float(cloud_price) if cloud_price else None,
        "prices": (
            [{"min_qty": 1, "unit_price_cny": float(cloud_price), "note": "云价格 (aggregate)"}]
            if cloud_price else []
        ),
        # hqew exposes no spec parameter table
        "parameters": [],
        # hqew-specific aggregates
        "total_listings_count": total_count,
        "top_listings_count": len(all_top),
    }
    return out


def quality_for(extracted: dict) -> str:
    has_variants = bool(extracted.get("variants"))
    has_stock = (extracted.get("stock_now_qty") or 0) > 0
    has_brand = bool(extracted.get("manufacturer"))
    if has_variants and has_stock and has_brand:
        return "high"
    if has_variants:
        return "medium"
    return "none"


def scrape(mpn: str, run_dir: Path) -> dict:
    record: dict = {
        "query": mpn,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "hqew.com",
        "search_url": f"{SEARCH_BASE}/{mpn}.html",
        "output_dir": str(run_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    # 1. curl_cffi (cheap probe, expected to fail)
    cf = attempt_curl_cffi(record["search_url"])
    record["attempts"].append(cf)
    print(f"[hqew] curl_cffi: {cf.get('outcome')} (status={cf.get('status')})")

    # 2. Playwright
    pw = attempt_playwright(mpn, run_dir)
    # Don't bloat the attempts log with the full listings array
    record["attempts"].append({k: v for k, v in pw.items() if k != "listings"})
    print(
        f"[hqew] playwright: {pw.get('outcome')} "
        f"listings={pw.get('listings_extracted')}/{pw.get('total_listings_count') or '?'}"
    )

    if pw.get("outcome") != "ok":
        record["status"] = "no_listings"
        return record

    record["status"] = "ok"
    record["method"] = "playwright"
    record["resolved_product_url"] = record["search_url"]
    record["extracted"] = normalize(pw, mpn)
    record["data_quality"] = quality_for(record["extracted"])
    return record


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "STM32G030F6P6"
    out_dir_override = argv[2] if len(argv) > 2 else None
    if out_dir_override:
        run_dir = Path(out_dir_override).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = make_run_dir(part)
    print(f"=== HQEW scrape: {part} ===")
    print(f"output folder: {run_dir}")

    rec = scrape(part, run_dir)
    safe = _safe(part)
    out = run_dir / f"{safe}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = write_summary(rec, run_dir, safe)

    print()
    print(f"Wrote {out}")
    print(f"Wrote {summary}")
    print(
        f"status: {rec.get('status')}  method: {rec.get('method')}  "
        f"quality: {rec.get('data_quality')}"
    )
    ex = rec.get("extracted") or {}
    if ex:
        print()
        print("Key fields:")
        for k in (
            "manufacturer_part_number", "manufacturer", "manufacturer_cn", "package",
            "unit_price_cny", "stock_now_qty",
            "total_listings_count", "variants_count",
        ):
            v = ex.get(k)
            if v is not None and v != "":
                print(f"  {k}: {v}")
        for v in ex.get("variants") or []:
            print(
                f"  variant {v.get('manufacturer_part_number','?'):<24} "
                f"listings={v.get('listing_count')} "
                f"sum_qty={v.get('stock_now_qty'):,} "
                f"package={v.get('package','')}"
            )
            for l in (v.get("listings") or [])[:3]:
                print(
                    f"    {l.get('supplier','?')[:28]:<28}  "
                    f"qty={l.get('quantity')}  "
                    f"MOQ={l.get('moq') or '?'}  "
                    f"批号={l.get('batch_code','')}  "
                    f"仓={l.get('warehouse_city','')}  "
                    f"备注={(l.get('transaction_note','') or '')[:25]}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
