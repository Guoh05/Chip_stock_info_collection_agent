"""Digikey Product Information API v4 client.

Flow:
  1. OAuth2 client_credentials → POST /v1/oauth2/token → bearer access_token.
  2. POST /products/v4/search/keyword with the MPN → ExactMatches[] / Products[].
  3. Normalize each distinct ManufacturerProductNumber into one variant
     subfolder, mirroring the scraper track's LCSC/Future per-variant layout.

Output folder convention:
    test/api_test/Test_<MPN>_DIGIKEY_<YYYYMMDD>_<HH>_<MM>_<SS>/
    ├── parent_summary.md
    ├── <MPN>.json                       (run-level summary + variant index)
    ├── raw_response.json                (full Digikey payload, for audit)
    └── <variant_mpn>/
        ├── <variant_mpn>.json
        ├── <variant_mpn>_raw_product.json
        └── <variant_mpn>_summary.md

Stock mapping (per project canonical schema):
  - 现货 = top-level `QuantityAvailable`, ships "下单后立即发货" if > 0.
  - 期货 = unbounded factory order when `NormallyStocking` or
    `ManufacturerLeadWeeks` is non-empty; `stock_future_qty = null`,
    `stock_future_ship_text = "原厂标准交货期 <N weeks>"`.
  - `ProductVariations[].QuantityAvailableforPackageType` surfaces as
    per-packaging breakdown rows so the buyer sees tube vs. tape-and-reel.

Usage:
    .venv/Scripts/python.exe api/scripts/api_digikey.py <MPN>
"""

from __future__ import annotations

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
TEST_ROOT = PROJECT_ROOT / "test" / "api_test"
CHANNEL = "DIGIKEY"
API_BASE = "https://api.digikey.com"
METHOD_TAG = "api_digikey_v4"

LOCALE_HEADERS = {
    "X-DIGIKEY-Locale-Site": "US",
    "X-DIGIKEY-Locale-Language": "en",
    "X-DIGIKEY-Locale-Currency": "USD",
    "X-DIGIKEY-Customer-Id": "0",
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


# In-process OAuth token cache. Keyed by client_id. A batch driver calling
# fetch_token() 100+ times in sequence will reuse the cached token until it's
# within 30 s of expiry. Each cache hit returns an `attempt` record marked
# `outcome=cached` so the attempts log stays auditable.
_TOKEN_CACHE: dict[str, dict] = {}
_TOKEN_REFRESH_MARGIN_SEC = 30


def fetch_token(client_id: str, client_secret: str) -> dict:
    """OAuth2 client_credentials → bearer token. Returns {token, attempt}.

    Caches the token in-process per client_id so sequential calls within the
    token's TTL skip the round-trip. Refreshes when within
    _TOKEN_REFRESH_MARGIN_SEC of expiry.
    """
    import time

    cached = _TOKEN_CACHE.get(client_id)
    if cached and cached.get("expires_at", 0) - time.time() > _TOKEN_REFRESH_MARGIN_SEC:
        return {
            "token": cached["token"],
            "attempt": {
                "method": METHOD_TAG,
                "profile": "oauth2_token_cached",
                "url": f"{API_BASE}/v1/oauth2/token",
                "outcome": "cached",
                "expires_in_remaining": int(cached["expires_at"] - time.time()),
            },
        }

    url = f"{API_BASE}/v1/oauth2/token"
    attempt: dict = {
        "method": METHOD_TAG,
        "profile": "oauth2_client_credentials",
        "url": url,
    }
    try:
        r = requests.post(
            url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=20,
        )
        attempt["status"] = r.status_code
        attempt["len"] = len(r.text)
        if r.status_code != 200:
            attempt["outcome"] = "http_error"
            attempt["error"] = r.text[:500]
            return {"token": None, "attempt": attempt}
        try:
            payload = r.json()
        except ValueError:
            attempt["outcome"] = "non_json"
            return {"token": None, "attempt": attempt}
        token = payload.get("access_token")
        expires_in = payload.get("expires_in") or 600
        attempt["expires_in"] = expires_in
        attempt["outcome"] = "ok" if token else "no_token"
        if token:
            _TOKEN_CACHE[client_id] = {
                "token": token,
                "expires_at": time.time() + float(expires_in),
            }
        return {"token": token, "attempt": attempt}
    except requests.RequestException as exc:
        attempt["outcome"] = "exception"
        attempt["error"] = str(exc)
        return {"token": None, "attempt": attempt}


def call_keyword_search(token: str, client_id: str, keyword: str) -> dict:
    """POST /products/v4/search/keyword."""
    url = f"{API_BASE}/products/v4/search/keyword"
    body = {
        "Keywords": keyword,
        "Limit": 50,
        "Offset": 0,
    }
    attempt: dict = {
        "method": METHOD_TAG,
        "profile": "products_v4_search_keyword",
        "url": url,
    }
    try:
        r = requests.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": client_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
                **LOCALE_HEADERS,
            },
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
        exact = payload.get("ExactMatches") or []
        products = payload.get("Products") or []
        attempt["exact_matches"] = len(exact)
        attempt["products_count"] = payload.get("ProductsCount") or len(products)
        attempt["outcome"] = "ok" if (exact or products) else "no_results"
        attempt["payload"] = payload
    except requests.RequestException as exc:
        attempt["outcome"] = "exception"
        attempt["error"] = str(exc)
    return attempt


def normalize_product(product: dict, query: str) -> dict:
    """Map one Digikey Product to the canonical schema."""
    mpn = product.get("ManufacturerProductNumber") or ""
    manufacturer = (product.get("Manufacturer") or {}).get("Name") or ""
    desc = product.get("Description") or {}
    description_en = desc.get("ProductDescription") or ""
    detailed_description = desc.get("DetailedDescription") or ""
    category = (product.get("Category") or {}).get("Name") or ""

    qty_available = _parse_int(product.get("QuantityAvailable"))
    normally_stocking = product.get("NormallyStocking")
    lead_weeks = product.get("ManufacturerLeadWeeks") or ""
    back_order_not_allowed = product.get("BackOrderNotAllowed")
    is_back_order_allowed = (
        (not back_order_not_allowed) if back_order_not_allowed is not None else None
    )
    product_status = (product.get("ProductStatus") or {}).get("Status") or ""

    # Primary DigiKey part number — try variations[0] for the canonical SKU
    variations = product.get("ProductVariations") or []
    primary_dk_pn = None
    if variations:
        primary_dk_pn = variations[0].get("DigiKeyProductNumber")

    # --- Canonical stock ---
    stock_now_qty = qty_available if qty_available is not None else 0
    stock_now_ship_text = "下单后立即发货" if stock_now_qty > 0 else None

    # Digikey v4 returns lead weeks as a bare integer string (e.g. "26").
    # Render with the unit so summaries read naturally.
    lead_weeks_display = ""
    if lead_weeks:
        if re.search(r"[a-zA-Z一-鿿]", str(lead_weeks)):
            lead_weeks_display = str(lead_weeks)
        else:
            lead_weeks_display = f"{lead_weeks} weeks"

    has_factory_path = (
        normally_stocking is True
        or bool(lead_weeks)
        or is_back_order_allowed is True
    )
    if has_factory_path:
        stock_future_qty = None  # unbounded — factory order
        if lead_weeks_display:
            stock_future_ship_text = f"原厂标准交货期 {lead_weeks_display}"
        else:
            stock_future_ship_text = "原厂期货 (可下单等待原厂交付)"
    else:
        stock_future_qty = 0
        stock_future_ship_text = None

    # --- Stock breakdown — use Digikey's own labels ---
    breakdown: list[dict] = []
    if stock_now_qty > 0:
        breakdown.append({
            "label": "QuantityAvailable",
            "warehouse": "DigiKey US warehouse",
            "quantity": stock_now_qty,
            "ship_text": stock_now_ship_text or "",
            "note": "DigiKey 在库,下单后立即发货",
        })
    if has_factory_path:
        breakdown.append({
            "label": "Factory Stock (ManufacturerLeadWeeks)",
            "warehouse": "Factory (via DigiKey)",
            "quantity": None,
            "ship_text": stock_future_ship_text or "",
            "note": (
                "工厂期货 — 无承诺库存数量。DigiKey 可接单后等待原厂交期。"
            ),
        })
    # Per-packaging quantities (when given)
    for v in variations:
        qty_pkg = _parse_int(v.get("QuantityAvailableforPackageType"))
        pkg_type = (v.get("PackageType") or {}).get("Name") or ""
        dk_pn = v.get("DigiKeyProductNumber") or ""
        if qty_pkg is None or pkg_type in ("",) or not dk_pn:
            continue
        breakdown.append({
            "label": f"Packaging — {pkg_type}",
            "warehouse": f"DigiKey ({dk_pn})",
            "quantity": qty_pkg,
            "ship_text": "下单后立即发货" if qty_pkg > 0 else "",
            "note": f"DigiKey P/N {dk_pn}",
        })

    # --- Pricing — flatten variations[*].StandardPricing into one tiers list ---
    # Prefer the primary variation's StandardPricing; surface alternate
    # packagings in `prices_alt`.
    prices: list[dict] = []
    prices_alt: list[dict] = []
    currency = "USD"
    if variations:
        primary = variations[0]
        for tier in primary.get("StandardPricing") or []:
            prices.append({
                "min_qty": _parse_int(tier.get("BreakQuantity")),
                "unit_price": tier.get("UnitPrice"),
                "unit_price_usd": tier.get("UnitPrice"),
                "ext_price": tier.get("TotalPrice"),
                "currency": "USD",
            })
        for v in variations[1:]:
            for tier in v.get("StandardPricing") or []:
                prices_alt.append({
                    "min_qty": _parse_int(tier.get("BreakQuantity")),
                    "unit_price": tier.get("UnitPrice"),
                    "ext_price": tier.get("TotalPrice"),
                    "packaging": (v.get("PackageType") or {}).get("Name"),
                    "digikey_part_number": v.get("DigiKeyProductNumber"),
                })

    # --- Parameters ---
    parameters: list[dict] = []
    package = None
    rohs = None
    hts = None
    eccn = None
    lifecycle_status = product_status
    for p in product.get("Parameters") or []:
        if not isinstance(p, dict):
            continue
        name = p.get("ParameterText") or ""
        value = p.get("ValueText") or ""
        if not name:
            continue
        parameters.append({"name": name, "value": value})
        lname = name.lower()
        if package is None and ("package" in lname or "封装" in name):
            package = value
        if rohs is None and "rohs" in lname:
            rohs = value
    classifications = product.get("Classifications") or {}
    # Digikey v4 keys: HtsusCode (US HTS), ExportControlClassNumber (ECCN),
    # RohsStatus, ReachStatus, MoistureSensitivityLevel.
    hts = classifications.get("HtsusCode") or classifications.get("HtsCode")
    eccn = (
        classifications.get("ExportControlClassNumber")
        or classifications.get("EccnCode")
    )
    if rohs is None:
        rohs = classifications.get("RohsStatus")

    # MOQ from primary variation
    min_order_qty = None
    min_multiplier = None
    if variations:
        min_order_qty = _parse_int(variations[0].get("MinimumOrderQuantity"))
        # "Standard Package" is the multiplier in DK terms
        std_pkg = variations[0].get("StandardPackage")
        min_multiplier = _parse_int(std_pkg) if std_pkg is not None else None

    out: dict = {
        # Identity
        "digikey_part_number": primary_dk_pn,
        "manufacturer_part_number": mpn,
        "manufacturer": manufacturer,
        "description_en": description_en,
        "detailed_description_cn": detailed_description,
        "datasheet_url": product.get("DatasheetUrl") or None,
        "image_url": product.get("PhotoUrl") or None,
        "product_url": product.get("ProductUrl") or None,
        "category_name_en": category,
        "lifecycle_status": lifecycle_status,
        "part_status": product_status,
        "is_rohs": rohs,
        "package": package,
        "hts_code": hts,
        "eccn": eccn,
        "min_order_qty": min_order_qty,
        "min_order_multiplier": min_multiplier,
        # Stock (canonical)
        "stock_now_qty": stock_now_qty,
        "stock_now_ship_text": stock_now_ship_text,
        "stock_future_qty": stock_future_qty,
        "stock_future_ship_text": stock_future_ship_text,
        "stock_breakdown": breakdown,
        # Stock (site-native — preserve verbatim)
        "site_quantity_available": product.get("QuantityAvailable"),
        "site_manufacturer_lead_weeks": lead_weeks,
        "site_normally_stocking": normally_stocking,
        "site_back_order_not_allowed": back_order_not_allowed,
        "site_non_stock": product.get("NonStock"),
        "site_discontinued": product.get("Discontinued"),
        "site_end_of_life": product.get("EndOfLife"),
        "site_date_last_buy_chance": product.get("DateLastBuyChance"),
        "site_product_status": product_status,
        # Pricing
        "prices": prices,
        "prices_alt": prices_alt,
        "currency": currency,
        # Parameters
        "parameters": parameters,
        # Packaging variations (audit)
        "product_variations_summary": [
            {
                "digikey_part_number": v.get("DigiKeyProductNumber"),
                "package_type": (v.get("PackageType") or {}).get("Name"),
                "quantity_for_package": _parse_int(
                    v.get("QuantityAvailableforPackageType")
                ),
                "min_order_qty": _parse_int(v.get("MinimumOrderQuantity")),
                "standard_package": v.get("StandardPackage"),
            }
            for v in variations
        ],
    }
    return out


def assess_quality(ex: dict) -> str:
    has_part = bool(ex.get("manufacturer_part_number"))
    has_price = bool(ex.get("prices"))
    has_stock_field = ex.get("stock_now_qty") is not None
    has_params = bool(ex.get("parameters"))
    if has_part and has_price and has_stock_field and has_params:
        return "high"
    if has_part and (has_price or has_stock_field):
        return "medium"
    if has_part:
        return "low"
    return "none"


def write_variant(
    rec: dict,
    variant_extracted: dict,
    raw_product: dict,
    run_dir: Path,
    variant_mpn: str,
) -> dict:
    folder_name = _sanitize_variant_folder(variant_mpn)
    sub = run_dir / folder_name
    sub.mkdir(parents=True, exist_ok=True)

    variant_rec = dict(rec)
    variant_rec["query"] = rec.get("query")
    variant_rec["variant_mpn"] = variant_mpn
    variant_rec["resolved_product_url"] = variant_extracted.get("product_url")
    variant_rec["extracted"] = variant_extracted
    variant_rec["data_quality"] = assess_quality(variant_extracted)
    variant_rec["status"] = "ok"
    variant_rec["attempts"] = rec.get("attempts") or []

    json_path = sub / f"{folder_name}.json"
    json_path.write_text(
        json.dumps(variant_rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    raw_path = sub / f"{folder_name}_raw_product.json"
    raw_path.write_text(
        json.dumps(raw_product, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary_path = write_summary(variant_rec, sub, folder_name)
    return {
        "folder": folder_name,
        "subdir": str(sub),
        "json": str(json_path),
        "summary": str(summary_path),
        "extracted": variant_extracted,
    }


def write_parent_summary(rec: dict, variant_infos: list[dict], run_dir: Path) -> Path:
    md: list[str] = []
    md.append(f"# Digikey API run — {rec.get('query')} ({CHANNEL})")
    md.append("")
    md.append(f"- **Status:** {rec.get('status')}")
    md.append(f"- **Method:** {rec.get('method')}")
    md.append(f"- **Source:** {rec.get('source')}")
    md.append(f"- **Run at (UTC):** {rec.get('scraped_at_utc')}")
    md.append(f"- **Variants captured:** {len(variant_infos)}")
    md.append("")

    if variant_infos:
        md.append("## Variants")
        md.append("")
        md.append(
            "| # | MPN | DK P/N | Manufacturer | 现货 | 期货 lead time | Price tiers | Status |"
        )
        md.append("|---|---|---|---|---|---|---|---|")
        for i, v in enumerate(variant_infos, 1):
            ex = v["extracted"]
            now = ex.get("stock_now_qty")
            lw = ex.get("site_manufacturer_lead_weeks") or ""
            lead_disp = (
                f"{lw} weeks"
                if lw and not re.search(r"[a-zA-Z一-鿿]", str(lw))
                else (lw or "n/a")
            )
            md.append(
                "| {i} | {mpn} | {dk} | {mfr} | {now} | {fut} | {tiers} | {st} |".format(
                    i=i,
                    mpn=ex.get("manufacturer_part_number") or "",
                    dk=ex.get("digikey_part_number") or "",
                    mfr=ex.get("manufacturer") or "",
                    now=f"{now:,}" if isinstance(now, int) else (now or ""),
                    fut=lead_disp,
                    tiers=len(ex.get("prices") or []),
                    st=ex.get("part_status") or "",
                )
            )
        md.append("")

    md.append("## Note on Digikey's stock model")
    md.append("")
    md.append(
        "Digikey's API does not have an in-transit pool. Stock is binary: what Digikey holds in "
        "its own warehouse (`QuantityAvailable` → 现货) and an open-ended factory-order path "
        "(via `ManufacturerLeadWeeks` / `NormallyStocking`). We map the latter as **期货 with "
        "`stock_future_qty = null`** (unbounded) and put the lead-time string in "
        "`stock_future_ship_text`."
    )
    md.append("")
    md.append(
        "Site-native fields (`site_quantity_available`, `site_manufacturer_lead_weeks`, "
        "`site_normally_stocking`, `site_back_order_not_allowed`, `site_non_stock`, "
        "`site_discontinued`, `site_end_of_life`, `site_product_status`) are preserved verbatim "
        "in each per-variant JSON for audit."
    )
    md.append("")
    md.append(
        "Per-packaging stock (`ProductVariations[].QuantityAvailableforPackageType`) is "
        "surfaced as extra `Packaging — <Type>` rows in the stock breakdown, so the buyer can "
        "see how the warehouse total splits across Tube / Tape&Reel / Cut Tape / etc."
    )
    md.append("")

    md.append("## Attempts")
    md.append("")
    md.append("| # | Method | Profile | Status | Len | Outcome |")
    md.append("|---|---|---|---|---|---|")
    for i, a in enumerate(rec.get("attempts") or [], 1):
        md.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                i,
                a.get("method", ""),
                a.get("profile", ""),
                a.get("status", ""),
                a.get("len", ""),
                a.get("outcome", ""),
            )
        )
    md.append("")

    out = run_dir / "parent_summary.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out


def call_api(query: str, run_dir: Path) -> dict:
    load_dotenv(PROJECT_ROOT / "api" / ".env")
    client_id = os.environ.get("DIGIKEY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DIGIKEY_CLIENT_SECRET", "").strip()

    rec: dict = {
        "query": query,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "api.digikey.com/products/v4",
        "search_url": f"{API_BASE}/products/v4/search/keyword",
        "output_dir": str(run_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    if not client_id or not client_secret:
        rec["status"] = "missing_credentials"
        rec["blocker"] = "DIGIKEY_CLIENT_ID/SECRET not set in api/.env"
        return rec

    # 1. Fetch token
    tok = fetch_token(client_id, client_secret)
    rec["attempts"].append(tok["attempt"])
    print(
        f"[digikey] oauth2 token: outcome={tok['attempt'].get('outcome')} "
        f"status={tok['attempt'].get('status')}"
    )
    if not tok["token"]:
        rec["status"] = "auth_failed"
        rec["blocker"] = "OAuth2 token request failed — check DIGIKEY_CLIENT_ID/SECRET"
        return rec

    # 2. Keyword search
    a2 = call_keyword_search(tok["token"], client_id, query)
    payload = a2.pop("payload", None)
    rec["attempts"].append(a2)
    print(
        f"[digikey] products/v4/search/keyword: outcome={a2.get('outcome')} "
        f"status={a2.get('status')} exact={a2.get('exact_matches')} "
        f"total={a2.get('products_count')}"
    )

    if a2.get("outcome") != "ok" or payload is None:
        rec["status"] = "no_results"
        return rec

    rec["method"] = METHOD_TAG
    rec["raw_payload"] = payload
    rec["status"] = "ok"
    return rec


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "STM32G030F6P6"
    run_dir = make_run_dir(part)
    print(f"=== DIGIKEY API: {part} ===")
    print(f"output folder: {run_dir}")

    rec = call_api(part, run_dir)
    payload = rec.pop("raw_payload", None)

    if payload is not None:
        (run_dir / "raw_response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    variant_infos: list[dict] = []
    if rec.get("status") == "ok" and payload is not None:
        exact = payload.get("ExactMatches") or []
        products = payload.get("Products") or []
        # ExactMatches first, then Products (avoiding duplicates by MPN string)
        seen_mpns: set[str] = set()
        candidates: list[dict] = []
        for p in exact:
            mpn = p.get("ManufacturerProductNumber") or ""
            if mpn and mpn not in seen_mpns:
                candidates.append(p)
                seen_mpns.add(mpn)
        for p in products:
            mpn = p.get("ManufacturerProductNumber") or ""
            if mpn and mpn not in seen_mpns:
                candidates.append(p)
                seen_mpns.add(mpn)

        for product in candidates:
            mpn = product.get("ManufacturerProductNumber") or "UNKNOWN"
            extracted = normalize_product(product, part)
            info = write_variant(rec, extracted, product, run_dir, mpn)
            variant_infos.append(info)

    # Parent JSON (lightweight index + canonical fields)
    parent_rec = dict(rec)
    parent_rec["variants_summary"] = [
        {
            "manufacturer_part_number": v["extracted"].get("manufacturer_part_number"),
            "digikey_part_number": v["extracted"].get("digikey_part_number"),
            "stock_now_qty": v["extracted"].get("stock_now_qty"),
            "stock_future_qty": v["extracted"].get("stock_future_qty"),
            "stock_future_ship_text": v["extracted"].get("stock_future_ship_text"),
            "subdir": v["folder"],
        }
        for v in variant_infos
    ]
    (run_dir / f"{part}.json").write_text(
        json.dumps(parent_rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_parent_summary(parent_rec, variant_infos, run_dir)

    print(f"\nWrote {len(variant_infos)} variant subfolder(s):")
    for v in variant_infos:
        ex = v["extracted"]
        print(
            f"  - {ex.get('manufacturer_part_number')} ({ex.get('digikey_part_number')}): "
            f"现货={ex.get('stock_now_qty')} 期货 lead={ex.get('site_manufacturer_lead_weeks')!r} "
            f"tiers={len(ex.get('prices') or [])}"
        )
    print(f"status: {rec.get('status')}  method: {rec.get('method')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
