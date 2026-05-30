"""Mouser Search API client.

POSTs to api.mouser.com/api/v1/search/partnumber (and falls back to
/search/keyword for ambiguous or comma-bearing MPNs that don't resolve on
partnumber search). Normalizes results into the canonical 现货/期货 schema used
across this project — see scraper/doc/scraper_report_v2.md and
common/_summary.py for the shared shape.

Folder convention: test/api/Test_<MPN>_MOUSER_<YYYYMMDD>_<HH>_<MM>_<SS>/
with per-variant subfolders (parent_summary.md at the top, one folder per
distinct ManufacturerPartNumber returned).

Usage:
    .venv/Scripts/python.exe api/scripts/api_mouser.py <MPN>
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
TEST_ROOT = PROJECT_ROOT / "test" / "api"
CHANNEL = "MOUSER"
API_BASE = "https://api.mouser.com/api/v1"
METHOD_TAG = "api_mouser_v1"


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
    """Extract the first integer from a Mouser stock string like '4426 In Stock'."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
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


def _parse_price_str(price_str) -> float | None:
    """Parse '$1.58' / '€2,40' / '0.76627' into a float, currency-agnostic."""
    if price_str is None or price_str == "":
        return None
    s = str(price_str).strip()
    # Strip everything that isn't digit, dot, comma, sign
    s = re.sub(r"[^\d.,\-]", "", s)
    if not s:
        return None
    # European decimal? if there's a comma and no dot, treat comma as decimal point
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def call_search_by_partnumber(api_key: str, mpn: str) -> dict:
    """POST /search/partnumber. Returns a dict with .status / .json / .err."""
    url = f"{API_BASE}/search/partnumber"
    body = {
        "SearchByPartRequest": {
            "mouserPartNumber": mpn,
            "partSearchOptions": "Exact",
        }
    }
    attempt: dict = {
        "method": METHOD_TAG,
        "profile": "search_partnumber_exact",
        "url": url,
    }
    try:
        r = requests.post(
            url,
            params={"apiKey": api_key},
            json=body,
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
        errs = payload.get("Errors") or []
        results = (payload.get("SearchResults") or {})
        n = results.get("NumberOfResult") or 0
        attempt["num_results"] = n
        if errs:
            attempt["api_errors"] = errs
        attempt["outcome"] = "ok" if n > 0 else "no_results"
        attempt["payload"] = payload
    except requests.RequestException as exc:
        attempt["outcome"] = "exception"
        attempt["error"] = str(exc)
    return attempt


def call_search_by_keyword(api_key: str, kw: str) -> dict:
    """POST /search/keyword — fallback when partnumber search returns 0."""
    url = f"{API_BASE}/search/keyword"
    body = {
        "SearchByKeywordRequest": {
            "keyword": kw,
            "records": 50,
            "startingRecord": 0,
            "searchOptions": "",
            "searchWithYourSignUpLanguage": "",
        }
    }
    attempt: dict = {
        "method": METHOD_TAG,
        "profile": "search_keyword",
        "url": url,
    }
    try:
        r = requests.post(
            url,
            params={"apiKey": api_key},
            json=body,
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
        errs = payload.get("Errors") or []
        results = (payload.get("SearchResults") or {})
        n = results.get("NumberOfResult") or 0
        attempt["num_results"] = n
        if errs:
            attempt["api_errors"] = errs
        attempt["outcome"] = "ok" if n > 0 else "no_results"
        attempt["payload"] = payload
    except requests.RequestException as exc:
        attempt["outcome"] = "exception"
        attempt["error"] = str(exc)
    return attempt


def normalize_part(part: dict, query: str) -> dict:
    """Map one Mouser SearchResults.Parts[] item to the canonical schema."""
    mpn = part.get("ManufacturerPartNumber") or ""
    mouser_pn = part.get("MouserPartNumber") or ""
    manufacturer = part.get("Manufacturer") or ""
    description = part.get("Description") or ""

    # Stock
    availability_raw = part.get("Availability") or ""
    factory_raw = part.get("FactoryStock") or ""
    in_stock_raw = part.get("AvailabilityInStock") or ""  # sometimes present in newer schema
    on_order_raw = part.get("AvailabilityOnOrder") or []
    lead_time_raw = part.get("LeadTime") or ""
    min_order = _parse_int(part.get("Min"))
    multiplier = _parse_int(part.get("Mult"))

    # 现货: prefer AvailabilityInStock if present; otherwise parse Availability ("4426 In Stock")
    stock_now_qty = _parse_int(in_stock_raw)
    if stock_now_qty is None:
        stock_now_qty = _parse_int(availability_raw) or 0
    stock_now_ship_text = (
        "Mouser 在库,下单后立即发货" if stock_now_qty and stock_now_qty > 0 else None
    )

    # 期货: prefer parseable FactoryStock; if missing but LeadTime exists, mark as unbounded factory order
    factory_qty = _parse_int(factory_raw)
    has_lead_time = bool(lead_time_raw)
    if factory_qty is not None and factory_qty > 0:
        stock_future_qty = factory_qty
        stock_future_ship_text = (
            f"Mouser 期货 (FactoryStock); Lead Time: {lead_time_raw}"
            if has_lead_time
            else "Mouser 期货 (FactoryStock)"
        )
    elif has_lead_time:
        # Unbounded factory order
        stock_future_qty = None
        stock_future_ship_text = f"原厂标准交货期 {lead_time_raw}"
    else:
        stock_future_qty = 0
        stock_future_ship_text = None

    # stock_breakdown (use Mouser's own labels)
    breakdown: list[dict] = []
    if stock_now_qty and stock_now_qty > 0:
        breakdown.append({
            "label": "Availability",
            "warehouse": "Mouser (in stock)",
            "quantity": stock_now_qty,
            "ship_text": stock_now_ship_text or "",
            "note": availability_raw,
        })
    if factory_qty is not None and factory_qty > 0:
        breakdown.append({
            "label": "FactoryStock",
            "warehouse": "Mouser (factory stock)",
            "quantity": factory_qty,
            "ship_text": f"Lead Time: {lead_time_raw}" if has_lead_time else "",
            "note": factory_raw,
        })
    elif has_lead_time and not (stock_now_qty and stock_now_qty > 0):
        # Factory order only — no committed quantity, just lead time
        breakdown.append({
            "label": "Factory order",
            "warehouse": "Mouser (factory)",
            "quantity": None,
            "ship_text": f"Lead Time: {lead_time_raw}",
            "note": "Unbounded factory order — no committed stock",
        })
    # Surface AvailabilityOnOrder pools as their own rows (each entry typically has Quantity + Date)
    if isinstance(on_order_raw, list):
        for entry in on_order_raw:
            if not isinstance(entry, dict):
                continue
            qty = _parse_int(entry.get("Quantity"))
            date_str = entry.get("Date") or ""
            if qty is None and not date_str:
                continue
            breakdown.append({
                "label": "OnOrder",
                "warehouse": "Mouser (on order)",
                "quantity": qty,
                "ship_text": f"ETA: {date_str}" if date_str else "",
                "note": "已下单中,等待原厂到货",
            })

    # Price tiers
    prices: list[dict] = []
    pb = part.get("PriceBreaks") or []
    currency = None
    for tier in pb:
        if not isinstance(tier, dict):
            continue
        qty = _parse_int(tier.get("Quantity"))
        price_raw = tier.get("Price") or ""
        price_val = _parse_price_str(price_raw)
        tier_currency = tier.get("Currency") or ""
        if tier_currency and currency is None:
            currency = tier_currency
        prices.append({
            "min_qty": qty,
            "unit_price": price_raw,
            "unit_price_float": price_val,
            "currency": tier_currency,
        })

    # Parameters
    parameters: list[dict] = []
    package = None
    standard_pack_qty = None
    lifecycle_status = part.get("LifecycleStatus") or None
    for attr in part.get("ProductAttributes") or []:
        if not isinstance(attr, dict):
            continue
        name = attr.get("AttributeName") or ""
        value = attr.get("AttributeValue") or ""
        if not name:
            continue
        parameters.append({"name": name, "value": value})
        # Common-field promotions
        lname = name.lower()
        if package is None and ("package" in lname or "封装" in name):
            package = value
        if standard_pack_qty is None and "标准包装数量" in name:
            standard_pack_qty = _parse_int(value)

    # Packaging option (per "Distributor packaging options" reference).
    # Mouser exposes Reeling (bool) + AlternatePackagings[] (links to other
    # Mouser PNs that carry the same MPN in different packaging). The native
    # search response does NOT carry a free-text "Tape & Reel/Cut Tape" string,
    # so we infer from `Reeling` + the Mouser PN suffix (`-TR` / `-CT`).
    site_reeling = bool(part.get("Reeling"))
    site_alt_packagings = [
        (d.get("APMfrPN") or "").strip()
        for d in (part.get("AlternatePackagings") or [])
        if isinstance(d, dict)
    ]
    pn_upper = (mouser_pn or "").upper()
    if pn_upper.endswith("-CT") or "-CT-" in pn_upper:
        packaging_option = "Cut Tape"
    elif pn_upper.endswith("-TR") or "-TR-" in pn_upper or pn_upper.endswith("-T&R"):
        packaging_option = "Tape & Reel"
    elif site_reeling:
        packaging_option = "Tape & Reel"
    else:
        packaging_option = ""

    out: dict = {
        # Identity
        "mouser_part_number": mouser_pn,
        "manufacturer_part_number": mpn,
        "manufacturer": manufacturer,
        "description_en": description,
        "datasheet_url": part.get("DataSheetUrl") or None,
        "image_url": part.get("ImagePath") or None,
        "product_url": part.get("ProductDetailUrl") or None,
        "category_name_en": part.get("Category") or None,
        "lifecycle_status": lifecycle_status,
        "is_rohs": part.get("ROHSStatus") or None,
        "package": package,
        "min_order_qty": min_order,
        "min_order_multiplier": multiplier,
        # Stock (canonical)
        "stock_now_qty": stock_now_qty,
        "stock_now_ship_text": stock_now_ship_text,
        "stock_future_qty": stock_future_qty,
        "stock_future_ship_text": stock_future_ship_text,
        "stock_breakdown": breakdown,
        # Stock (site-native — preserve verbatim)
        "site_availability": availability_raw,
        "site_availability_in_stock": in_stock_raw,
        "site_factory_stock": factory_raw,
        "site_availability_on_order": on_order_raw,
        "site_lead_time": lead_time_raw,
        "site_reeling": site_reeling,
        "site_alternate_packagings": site_alt_packagings,
        "site_standard_pack_qty": standard_pack_qty,
        "packaging_option": packaging_option,
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
        or ex.get("stock_future_qty") is not None
        or bool(ex.get("site_availability"))
    )
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
    raw_part: dict,
    run_dir: Path,
    variant_mpn: str,
) -> dict:
    """Write per-variant JSON + summary + raw payload, return the subfolder info."""
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
    # Strip large lists from per-variant attempts to keep file small (they live in parent)
    variant_rec["attempts"] = rec.get("attempts") or []

    json_path = sub / f"{folder_name}.json"
    json_path.write_text(
        json.dumps(variant_rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    raw_path = sub / f"{folder_name}_raw_part.json"
    raw_path.write_text(
        json.dumps(raw_part, ensure_ascii=False, indent=2), encoding="utf-8"
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
    md.append(f"# Mouser API run — {rec.get('query')} ({CHANNEL})")
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
            "| # | MPN | Mouser P/N | Manufacturer | 现货 (qty) | 期货 (qty) | LeadTime | Price tiers |"
        )
        md.append("|---|---|---|---|---|---|---|---|")
        for i, v in enumerate(variant_infos, 1):
            ex = v["extracted"]
            md.append(
                "| {i} | {mpn} | {mp} | {mfr} | {now} | {fut} | {lt} | {tiers} |".format(
                    i=i,
                    mpn=ex.get("manufacturer_part_number") or "",
                    mp=ex.get("mouser_part_number") or "",
                    mfr=ex.get("manufacturer") or "",
                    now=(
                        f"{ex.get('stock_now_qty'):,}"
                        if isinstance(ex.get("stock_now_qty"), int)
                        else ex.get("stock_now_qty") or ""
                    ),
                    fut=(
                        f"{ex.get('stock_future_qty'):,}"
                        if isinstance(ex.get("stock_future_qty"), int)
                        else (
                            "unbounded (factory order)"
                            if ex.get("stock_future_ship_text")
                            and ex.get("stock_future_qty") is None
                            else ""
                        )
                    ),
                    lt=ex.get("site_lead_time") or "",
                    tiers=len(ex.get("prices") or []),
                )
            )
        md.append("")

    md.append("## Note on Mouser's stock model")
    md.append("")
    md.append(
        "Mouser's API exposes three independent stock pools per part. We map them onto the "
        "project's canonical 现货 / 期货 schema as follows:"
    )
    md.append("")
    md.append(
        "- `Availability` (e.g. `\"4426 In Stock\"`) → **现货** (`stock_now_qty`). "
        "This is inventory in Mouser's own distribution warehouse, ships same/next day."
    )
    md.append(
        "- `FactoryStock` (e.g. `\"40000\"`) + `LeadTime` (e.g. `\"9 Weeks\"`) → **期货** "
        "(`stock_future_qty`, `stock_future_ship_text`). Interpretation: factory-held inventory "
        "shipped through Mouser on the manufacturer's lead time. If `FactoryStock` is empty but a "
        "`LeadTime` is given, the part is back-orderable but no committed quantity — surfaced as "
        "an unbounded factory order with `stock_future_qty = null`."
    )
    md.append(
        "- `AvailabilityOnOrder` (array of `{Quantity, Date}`) → extra `OnOrder` rows in the "
        "stock breakdown. These are batches Mouser has already ordered from the factory and that "
        "are expected on the listed date — distinct from open-ended factory order."
    )
    md.append("")
    md.append(
        "Site-native fields (`site_availability`, `site_factory_stock`, `site_lead_time`, "
        "`site_availability_on_order`) are preserved verbatim in each per-variant JSON for audit."
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
    api_key = os.environ.get("MOUSER_API_KEY", "").strip()

    rec: dict = {
        "query": query,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "api.mouser.com/api/v1",
        "search_url": f"{API_BASE}/search/partnumber",
        "output_dir": str(run_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }

    if not api_key:
        rec["status"] = "missing_credentials"
        rec["blocker"] = "MOUSER_API_KEY not set in api/.env"
        return rec

    # 1. Exact-MPN search
    a1 = call_search_by_partnumber(api_key, query)
    payload_1 = a1.pop("payload", None)
    rec["attempts"].append(a1)
    print(
        f"[mouser] search/partnumber: outcome={a1.get('outcome')} "
        f"status={a1.get('status')} num={a1.get('num_results')}"
    )

    payload = payload_1 if (a1.get("outcome") == "ok") else None

    # 2. Fallback: keyword search (handles MPNs with separators that confuse exact-match)
    if not payload:
        a2 = call_search_by_keyword(api_key, query)
        payload_2 = a2.pop("payload", None)
        rec["attempts"].append(a2)
        print(
            f"[mouser] search/keyword: outcome={a2.get('outcome')} "
            f"status={a2.get('status')} num={a2.get('num_results')}"
        )
        if a2.get("outcome") == "ok":
            payload = payload_2

    if not payload:
        rec["status"] = "no_results"
        return rec

    rec["method"] = METHOD_TAG
    rec["raw_payload"] = payload  # consumed by main() — written to parent run dir
    rec["status"] = "ok"
    return rec


def main(argv: list[str]) -> int:
    part = argv[1] if len(argv) > 1 else "STM32G030F6P6"
    run_dir = make_run_dir(part)
    print(f"=== MOUSER API: {part} ===")
    print(f"output folder: {run_dir}")

    rec = call_api(part, run_dir)
    payload = rec.pop("raw_payload", None)

    if payload is not None:
        (run_dir / "raw_response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    variant_infos: list[dict] = []
    if rec.get("status") == "ok" and payload is not None:
        parts = (payload.get("SearchResults") or {}).get("Parts") or []
        # Group by exact ManufacturerPartNumber (treat different MPN strings as
        # different variants per project rule).
        seen: dict[str, dict] = {}
        for raw_part in parts:
            mpn = raw_part.get("ManufacturerPartNumber") or "UNKNOWN"
            # If multiple rows share the same MPN, keep the one with the
            # highest in-stock availability (most informative).
            ex_candidate = normalize_part(raw_part, part)
            prev = seen.get(mpn)
            if prev is None or (
                (ex_candidate.get("stock_now_qty") or 0)
                > (prev["extracted"].get("stock_now_qty") or 0)
            ):
                seen[mpn] = {"raw": raw_part, "extracted": ex_candidate}
        for mpn, bundle in seen.items():
            info = write_variant(
                rec, bundle["extracted"], bundle["raw"], run_dir, mpn
            )
            variant_infos.append(info)

    # Write the parent JSON (lighter than raw_response.json — just our normalized view)
    parent_rec = dict(rec)
    parent_rec["variants_summary"] = [
        {
            "manufacturer_part_number": v["extracted"].get("manufacturer_part_number"),
            "mouser_part_number": v["extracted"].get("mouser_part_number"),
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
            f"  - {ex.get('manufacturer_part_number')} ({ex.get('mouser_part_number')}): "
            f"现货={ex.get('stock_now_qty')} 期货={ex.get('stock_future_qty')} "
            f"lead={ex.get('site_lead_time')!r} tiers={len(ex.get('prices') or [])}"
        )
    print(f"status: {rec.get('status')}  method: {rec.get('method')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
