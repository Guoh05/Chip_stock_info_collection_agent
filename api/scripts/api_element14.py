"""Element14 / Farnell (e络盟) catalog API client.

GETs api.element14.com/catalog/products with `term=manuPartNum:<MPN>` and
normalizes results into the canonical 现货/期货 schema. The e络盟 store
(`cn.element14.com`) is the default store ID since the API key on file is the
China account; pass --store to switch.

Docs of record: partner.element14.com/search_api/Description (overview),
/Request_URL, /Query_Parameters, /storeInfoid_Values.

Folder convention: test/api/Test_<MPN>_ELEMENT14_<YYYYMMDD>_<HH>_<MM>_<SS>/
with per-variant subfolders (one per distinct returned MPN).

Usage:
    .venv/Scripts/python.exe api/scripts/api_element14.py <MPN>
    .venv/Scripts/python.exe api/scripts/api_element14.py <MPN> --store farnell.com
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "api"
CHANNEL = "ELEMENT14"
API_BASE = "https://api.element14.com/catalog/products"
METHOD_TAG = "api_element14_v1"

# Default store — e络盟 China (the official store ID is `cn.element14.com`,
# NOT `cn.farnell.com`; the Farnell-branded China hostname does not exist on
# Element14's API). Other commonly-useful IDs:
#   uk.farnell.com (UK), www.newark.com (US), sg.element14.com, hk.element14.com,
#   au.element14.com, in.element14.com, jp.farnell.com
# Full list: https://partner.element14.com/search_api/storeInfoid_Values
DEFAULT_STORE = "cn.element14.com"

# Heuristic: which currency each store returns. Element14 doesn't ship a
# currency code in the payload — it follows the store locale.
STORE_CURRENCY = {
    "cn.element14.com": "CNY",
    "uk.farnell.com": "GBP",
    "www.newark.com": "USD",
    "canada.newark.com": "CAD",
    "mexico.newark.com": "MXN",
    "de.farnell.com": "EUR",
    "fr.farnell.com": "EUR",
    "es.farnell.com": "EUR",
    "it.farnell.com": "EUR",
    "nl.farnell.com": "EUR",
    "ie.farnell.com": "EUR",
    "se.farnell.com": "SEK",
    "no.farnell.com": "NOK",
    "dk.farnell.com": "DKK",
    "ch.farnell.com": "CHF",
    "pl.farnell.com": "PLN",
    "ru.farnell.com": "RUB",
    "tr.farnell.com": "TRY",
    "il.farnell.com": "ILS",
    "jp.farnell.com": "JPY",
    "sg.element14.com": "SGD",
    "hk.element14.com": "HKD",
    "au.element14.com": "AUD",
    "nz.element14.com": "NZD",
    "my.element14.com": "MYR",
    "ph.element14.com": "PHP",
    "th.element14.com": "THB",
    "in.element14.com": "INR",
    "tw.element14.com": "TWD",
    "kr.element14.com": "KRW",
    "vn.element14.com": "VND",
}


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


def _sanitize_variant_folder(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", mpn) or "UNKNOWN"


def _parse_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).replace(",", "").strip()
    if not s:
        return None
    m = re.match(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def _parse_price(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def call_search(api_key: str, mpn: str, store_id: str, term_field: str) -> dict:
    """One GET against /catalog/products. `term_field` is one of `manuPartNum`,
    `any`, or `id` per the official Search API docs.
    """
    attempt: dict = {
        "method": METHOD_TAG,
        "profile": f"catalog_products_term_{term_field}",
        "url": API_BASE,
        "store": store_id,
    }
    # Note: `versionNumber` is NOT a valid query parameter for this endpoint
    # (per partner.element14.com/search_api/Query_Parameters). Including it
    # makes the upstream return a generic 400 with no body.
    params = {
        "term": f"{term_field}:{mpn}",
        "storeInfo.id": store_id,
        "resultsSettings.offset": 0,
        "resultsSettings.numberOfResults": 25,
        "resultsSettings.responseGroup": "large",
        "callInfo.responseDataFormat": "json",
        "callInfo.apiKey": api_key,
    }
    try:
        r = requests.get(
            API_BASE,
            params=params,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        attempt["status"] = r.status_code
        attempt["len"] = len(r.text)
        if r.status_code != 200:
            attempt["outcome"] = "http_error"
            attempt["error"] = r.text[:500]
            return attempt
        try:
            payload = r.json()
        except ValueError:
            attempt["outcome"] = "non_json"
            attempt["error"] = r.text[:500]
            return attempt
        # Element14 returns either `keywordSearchReturn` (term=... queries) or
        # `manufacturerPartNumberSearchReturn` (legacy). Normalize to a single
        # `_root` value below.
        root = None
        for k in (
            "keywordSearchReturn",
            "manufacturerPartNumberSearchReturn",
            "premierFarnellPartNumberReturn",
        ):
            if k in payload:
                root = payload[k]
                attempt["payload_root_key"] = k
                break
        if root is None:
            attempt["outcome"] = "unexpected_shape"
            attempt["error"] = "no recognized root key in response; got: " + ",".join(payload.keys())[:300]
            return attempt
        n = _parse_int(root.get("numberOfResults")) or len(root.get("products") or [])
        attempt["num_results"] = n
        attempt["outcome"] = "ok" if n > 0 else "no_results"
        attempt["payload"] = payload
        attempt["root"] = root
    except requests.RequestException as exc:
        attempt["outcome"] = "exception"
        attempt["error"] = str(exc)
    return attempt


def normalize_product(prod: dict, query: str, store_id: str) -> dict:
    """Map one Element14 product entry to the canonical schema."""
    mpn = (
        prod.get("translatedManufacturerPartNumber")
        or prod.get("manufacturerPartNumber")
        or ""
    )
    manufacturer = prod.get("translatedManufacturer") or prod.get("vendorName") or ""
    sku = prod.get("sku") or ""
    display_name = prod.get("displayName") or ""
    product_url = prod.get("productOverviewUrl") or ""
    if product_url and not product_url.startswith("http"):
        product_url = f"https://{store_id}{product_url}"

    # Stock — Element14 returns `stock.level` (total across regions),
    # `stock.leastLeadTime` (shortest lead time in DAYS — verified against
    # actual values like 218 for STM32F030; weeks would be implausible), and
    # `stock.regionalBreakdown` (per-region totals).
    stock = prod.get("stock") or {}
    stock_level = _parse_int(stock.get("level")) or 0
    lead_time = _parse_int(stock.get("leastLeadTime"))
    stock_status = stock.get("status") or ""
    regional = stock.get("regionalBreakdown") or []

    stock_now_qty = stock_level
    stock_now_ship_text = "e络盟 在库,下单后立即发货" if stock_level > 0 else None

    has_lead_time = lead_time is not None and lead_time > 0
    if has_lead_time:
        stock_future_qty = None  # unbounded factory order
        stock_future_ship_text = f"原厂标准交货期 {lead_time} 天 (Element14 leastLeadTime, days)"
    else:
        stock_future_qty = 0
        stock_future_ship_text = None

    breakdown: list[dict] = []
    if stock_level > 0:
        breakdown.append({
            "label": "Stock level (total)",
            "warehouse": f"Element14 ({store_id})",
            "quantity": stock_level,
            "ship_text": stock_now_ship_text or "",
            "note": f"status={stock_status}" if stock_status else "",
        })
    # Per-region rows — one per warehouse region exposed by the API.
    for r in regional:
        if not isinstance(r, dict):
            continue
        r_level = _parse_int(r.get("level")) or 0
        r_lead = _parse_int(r.get("leastLeadTime"))
        r_warehouse = r.get("warehouse") or ""
        ship_bits = []
        if r_level > 0:
            ship_bits.append("在库")
        if r_lead is not None:
            ship_bits.append(f"lead {r_lead} 天")
        breakdown.append({
            "label": f"Region — {r_warehouse}",
            "warehouse": f"Element14 / {r_warehouse}",
            "quantity": r_level,
            "ship_text": " · ".join(ship_bits) if ship_bits else "",
            "note": "",
        })
    if has_lead_time:
        breakdown.append({
            "label": "Factory lead time",
            "warehouse": "Factory (via Element14)",
            "quantity": None,
            "ship_text": stock_future_ship_text or "",
            "note": "Unbounded factory order — no committed quantity",
        })

    # Prices
    prices: list[dict] = []
    currency = STORE_CURRENCY.get(store_id, "")
    for tier in prod.get("prices") or []:
        if not isinstance(tier, dict):
            continue
        from_qty = _parse_int(tier.get("from"))
        cost = _parse_price(tier.get("cost"))
        prices.append({
            "min_qty": from_qty,
            "unit_price": cost,
            "unit_price_float": cost,
            "currency": currency,
            "to_qty": _parse_int(tier.get("to")),
        })

    # Parameters
    parameters: list[dict] = []
    package = None
    for attr in prod.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        label = attr.get("attributeLabel") or ""
        value = attr.get("attributeValue") or ""
        if not label:
            continue
        parameters.append({"name": label, "value": value})
        lname = label.lower()
        if package is None and ("package" in lname or "case style" in lname or "封装" in label):
            package = value

    # Datasheets — Element14 returns a list of {url, type, language}; pick the
    # first English PDF if present, else the first entry.
    datasheets = prod.get("datasheets") or []
    datasheet_url = None
    if isinstance(datasheets, list):
        for ds in datasheets:
            if isinstance(ds, dict) and ds.get("url"):
                if "EN" in (ds.get("language") or "").upper():
                    datasheet_url = ds.get("url")
                    break
        if datasheet_url is None:
            for ds in datasheets:
                if isinstance(ds, dict) and ds.get("url"):
                    datasheet_url = ds.get("url")
                    break

    # Image
    image_url = None
    img = prod.get("image")
    if isinstance(img, dict):
        base = img.get("baseName")
        if base:
            image_url = f"https://{store_id}/productimages/standard/{base}"

    rohs_status = prod.get("rohsStatusCode") or prod.get("rohsStatusComplianceCode")
    product_status = prod.get("productStatus") or ""

    out: dict = {
        # Identity
        "element14_sku": sku,
        "element14_display_name": display_name,
        "manufacturer_part_number": mpn,
        "manufacturer": manufacturer,
        "description_en": display_name,
        "datasheet_url": datasheet_url,
        "image_url": image_url,
        "product_url": product_url or None,
        "lifecycle_status": product_status,
        "part_status": product_status,
        "is_rohs": rohs_status,
        "package": package,
        # Stock (canonical)
        "stock_now_qty": stock_now_qty,
        "stock_now_ship_text": stock_now_ship_text,
        "stock_future_qty": stock_future_qty,
        "stock_future_ship_text": stock_future_ship_text,
        "stock_breakdown": breakdown,
        # Stock (site-native)
        "site_stock_level": stock_level,
        "site_stock_status": stock_status,
        "site_lead_time_days": lead_time,
        "site_regional_breakdown": regional,
        "site_warehouse_breakdown": stock.get("breakdown") or [],
        "site_store_id": store_id,
        # Pricing
        "prices": prices,
        "currency": currency,
        # Parameters
        "parameters": parameters,
    }
    return out


def assess_quality(ex: dict) -> str:
    has_part = bool(ex.get("manufacturer_part_number"))
    has_price = bool(ex.get("prices"))
    has_stock_field = (
        ex.get("stock_now_qty") is not None
        or ex.get("site_lead_time_days") is not None
    )
    has_params = bool(ex.get("parameters"))
    if has_part and has_price and has_stock_field and has_params:
        return "high"
    if has_part and (has_price or has_stock_field):
        return "medium"
    if has_part:
        return "low"
    return "none"


def write_variant(rec, ex, raw, run_dir, variant_mpn):
    folder_name = _sanitize_variant_folder(variant_mpn)
    sub = run_dir / folder_name
    sub.mkdir(parents=True, exist_ok=True)
    variant_rec = dict(rec)
    variant_rec["query"] = rec.get("query")
    variant_rec["variant_mpn"] = variant_mpn
    variant_rec["resolved_product_url"] = ex.get("product_url")
    variant_rec["extracted"] = ex
    variant_rec["data_quality"] = assess_quality(ex)
    variant_rec["status"] = "ok"
    variant_rec["attempts"] = rec.get("attempts") or []
    json_path = sub / f"{folder_name}.json"
    json_path.write_text(json.dumps(variant_rec, ensure_ascii=False, indent=2), encoding="utf-8")
    raw_path = sub / f"{folder_name}_raw_product.json"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = write_summary(variant_rec, sub, folder_name)
    return {"folder": folder_name, "subdir": str(sub),
            "json": str(json_path), "summary": str(summary_path), "extracted": ex}


def write_parent_summary(rec, variant_infos, run_dir):
    md: list[str] = []
    md.append(f"# Element14 API run — {rec.get('query')} ({CHANNEL})")
    md.append("")
    md.append(f"- **Status:** {rec.get('status')}")
    md.append(f"- **Method:** {rec.get('method')}")
    md.append(f"- **Source:** {rec.get('source')}")
    md.append(f"- **Store:** {rec.get('store_id')}")
    md.append(f"- **Run at (UTC):** {rec.get('scraped_at_utc')}")
    md.append(f"- **Variants captured:** {len(variant_infos)}")
    md.append("")
    if variant_infos:
        md.append("## Variants")
        md.append("")
        md.append("| # | MPN | Element14 SKU | Manufacturer | 现货 (qty) | Lead time | Price tiers | Currency |")
        md.append("|---|---|---|---|---|---|---|---|")
        for i, v in enumerate(variant_infos, 1):
            ex = v["extracted"]
            now = ex.get("stock_now_qty")
            md.append(
                "| {i} | {mpn} | {sku} | {mfr} | {now} | {lt} | {tiers} | {cur} |".format(
                    i=i,
                    mpn=ex.get("manufacturer_part_number") or "",
                    sku=ex.get("element14_sku") or "",
                    mfr=ex.get("manufacturer") or "",
                    now=f"{now:,}" if isinstance(now, int) else (now or ""),
                    lt=ex.get("site_lead_time_days") or "n/a",
                    tiers=len(ex.get("prices") or []),
                    cur=ex.get("currency") or "",
                )
            )
        md.append("")
    md.append("## Note on Element14's stock model")
    md.append("")
    md.append(
        "Element14's API returns one warehouse pool (`stock.level`) for the chosen store, "
        "plus a manufacturer lead time (`stock.leastLeadTime`, in weeks) when the part is "
        "back-orderable. We map `stock.level` → 现货 and the lead-time path → 期货 with "
        "`stock_future_qty = null` (unbounded factory order). Currency is inferred from the "
        "store ID (cn.farnell.com → CNY, us.newark.com → USD, etc.); the API itself does not "
        "echo a currency code."
    )
    md.append("")
    md.append("## Attempts")
    md.append("")
    md.append("| # | Method | Profile | Store | Status | Len | Outcome |")
    md.append("|---|---|---|---|---|---|---|")
    for i, a in enumerate(rec.get("attempts") or [], 1):
        md.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                i, a.get("method", ""), a.get("profile", ""),
                a.get("store", ""), a.get("status", ""),
                a.get("len", ""), a.get("outcome", ""),
            )
        )
    md.append("")
    out = run_dir / "parent_summary.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out


def call_api(query: str, run_dir: Path, store_id: str = DEFAULT_STORE) -> dict:
    load_dotenv(PROJECT_ROOT / "api" / ".env")
    api_key = os.environ.get("ELEMENT14_API_KEY", "").strip()

    rec: dict = {
        "query": query,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "api.element14.com/catalog/products",
        "store_id": store_id,
        "search_url": API_BASE,
        "output_dir": str(run_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }
    if not api_key:
        rec["status"] = "missing_credentials"
        rec["blocker"] = "ELEMENT14_API_KEY not set in api/.env"
        return rec

    # The docs list three term keys: `manuPartNum` (manufacturer part number,
    # exact match), `id` (Element14 SKU), `any` (keyword search). Try
    # manuPartNum first for an exact MPN lookup; fall back to keyword search
    # via `any:` so we still surface near-matches when the exact MPN isn't
    # in the chosen store's catalog.
    for term_field in ("manuPartNum", "any"):
        a = call_search(api_key, query, store_id, term_field)
        payload = a.pop("payload", None)
        root = a.pop("root", None)
        rec["attempts"].append(a)
        print(
            f"[element14] term={term_field}: outcome={a.get('outcome')} "
            f"status={a.get('status')} num={a.get('num_results')}"
        )
        if a.get("outcome") == "ok":
            rec["method"] = METHOD_TAG
            rec["raw_payload"] = payload
            rec["root"] = root
            rec["status"] = "ok"
            return rec

    rec["status"] = "no_results"
    return rec


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mpn", nargs="?", default="LTV817B-V-G")
    parser.add_argument("--store", default=DEFAULT_STORE,
                        help=f"Element14 store ID (default {DEFAULT_STORE})")
    args = parser.parse_args(argv[1:])

    part = args.mpn
    run_dir = make_run_dir(part)
    print(f"=== ELEMENT14 API: {part} (store={args.store}) ===")
    print(f"output folder: {run_dir}")

    rec = call_api(part, run_dir, args.store)
    payload = rec.pop("raw_payload", None)
    root = rec.pop("root", None)

    if payload is not None:
        (run_dir / "raw_response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    variant_infos: list[dict] = []
    if rec.get("status") == "ok" and root is not None:
        products = root.get("products") or []
        # Group by exact MPN — different SKUs sharing the same MPN get the
        # highest-stock representative.
        seen: dict[str, dict] = {}
        for raw in products:
            ex = normalize_product(raw, part, args.store)
            mpn = ex.get("manufacturer_part_number") or "UNKNOWN"
            prev = seen.get(mpn)
            if prev is None or (
                (ex.get("stock_now_qty") or 0) > (prev["extracted"].get("stock_now_qty") or 0)
            ):
                seen[mpn] = {"raw": raw, "extracted": ex}
        for mpn, bundle in seen.items():
            info = write_variant(rec, bundle["extracted"], bundle["raw"], run_dir, mpn)
            variant_infos.append(info)

    parent_rec = dict(rec)
    parent_rec["variants_summary"] = [
        {
            "manufacturer_part_number": v["extracted"].get("manufacturer_part_number"),
            "element14_sku": v["extracted"].get("element14_sku"),
            "stock_now_qty": v["extracted"].get("stock_now_qty"),
            "stock_future_qty": v["extracted"].get("stock_future_qty"),
            "stock_future_ship_text": v["extracted"].get("stock_future_ship_text"),
            "subdir": v["folder"],
        }
        for v in variant_infos
    ]
    (run_dir / f"{_sanitize_variant_folder(part)}.json").write_text(
        json.dumps(parent_rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_parent_summary(parent_rec, variant_infos, run_dir)

    print(f"\nWrote {len(variant_infos)} variant subfolder(s):")
    for v in variant_infos:
        ex = v["extracted"]
        print(
            f"  - {ex.get('manufacturer_part_number')} ({ex.get('element14_sku')}): "
            f"现货={ex.get('stock_now_qty')} lead={ex.get('site_lead_time_days')!r} "
            f"tiers={len(ex.get('prices') or [])}"
        )
    print(f"status: {rec.get('status')}  method: {rec.get('method')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
