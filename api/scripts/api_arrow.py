"""Arrow Electronics Pricing & Availability API client.

GETs api.arrow.com/itemservice/v4/en/search/list with the request payload as a
JSON-encoded `req` querystring parameter. Authentication requires BOTH
`login` and `apikey` querystring params (in addition to login/apikey being
nested inside the `req` JSON — Arrow's docs show it both ways).

Folder convention: test/api_test/Test_<MPN>_ARROW_<YYYYMMDD>_<HH>_<MM>_<SS>/
with per-variant subfolders (one per distinct returned MPN).

Docs of record:
  https://developers.arrow.com/api/index.php/site/page?view=v4isSearchList
  https://developers.arrow.com/api/index.php/site/page?view=gettingStarted

Usage:
    .venv/Scripts/python.exe api/scripts/api_arrow.py <MPN>
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
TEST_ROOT = PROJECT_ROOT / "test" / "api_test"
CHANNEL = "ARROW"
API_BASE = "https://api.arrow.com/itemservice/v4/en"
METHOD_TAG = "api_arrow_v4"


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


def call_search_list(login: str, api_key: str, mpn: str, use_exact: bool = True) -> dict:
    """POST/GET against /search/list with `useExact=true` for exact MPN match."""
    attempt: dict = {
        "method": METHOD_TAG,
        "profile": f"search_list_useExact_{use_exact}",
        "url": f"{API_BASE}/search/list",
    }
    req_body = {
        "request": {
            "login": login,
            "apikey": api_key,
            "useExact": use_exact,
            "parts": [{"partNum": mpn}],
        }
    }
    try:
        r = requests.get(
            f"{API_BASE}/search/list",
            params={
                "login": login,
                "apikey": api_key,
                "req": json.dumps(req_body),
            },
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
        # Arrow always wraps responses in itemserviceresult; check returnCode.
        result = payload.get("itemserviceresult") or {}
        ta = result.get("transactionArea") or []
        rc_msg = ""
        rc_success = False
        if ta and isinstance(ta[0], dict):
            response = ta[0].get("response") or {}
            rc_msg = response.get("returnMsg", "") or ""
            rc_success = bool(response.get("success", False))
        attempt["return_msg"] = rc_msg
        if not rc_success:
            attempt["outcome"] = "auth_failed" if "AUTH" in rc_msg.upper() or "LOGIN" in rc_msg.upper() else "api_error"
            attempt["error"] = rc_msg
            return attempt
        # Find the PartList. Arrow's actual nesting is
        # itemserviceresult.data[0].resultList[].PartList[].
        data = result.get("data") or []
        parts: list[dict] = []
        if data and isinstance(data[0], dict):
            top0 = data[0]
            attempt["parts_found"] = _parse_int(top0.get("partsFound"))
            attempt["parts_requested"] = _parse_int(top0.get("partsRequested"))
            for entry in top0.get("resultList") or []:
                if isinstance(entry, dict):
                    parts.extend(entry.get("PartList") or [])
        attempt["num_results"] = len(parts)
        attempt["outcome"] = "ok" if parts else "no_results"
        attempt["payload"] = payload
        attempt["parts"] = parts
    except requests.RequestException as exc:
        attempt["outcome"] = "exception"
        attempt["error"] = str(exc)
    return attempt


def normalize_part(part: dict, query: str) -> dict:
    """Map one Arrow PartList entry to the canonical schema."""
    mpn = part.get("partNum") or ""
    mfr_obj = part.get("manufacturer") or {}
    manufacturer = mfr_obj.get("mfrName") or mfr_obj.get("mfrCd") or ""
    desc = part.get("desc") or ""
    package_type = part.get("packageType") or ""
    item_id = part.get("itemId")
    part_status = part.get("status") or ""

    # Resources: pull useful URLs by type
    resources = part.get("resources") or []
    datasheet_url = None
    image_url = None
    product_url = None
    for r in resources:
        if not isinstance(r, dict):
            continue
        rtype = (r.get("type") or "").lower()
        uri = r.get("uri") or ""
        if not uri:
            continue
        if rtype == "datasheet" and not datasheet_url:
            datasheet_url = uri
        elif rtype.startswith("image_") and not image_url:
            image_url = uri
        elif rtype == "cloud_part_detail" and not product_url:
            product_url = uri

    # Compliance / RoHS
    env = part.get("EnvData") or {}
    rohs = None
    for c in env.get("compliance") or []:
        if not isinstance(c, dict):
            continue
        label = (c.get("displayLabel") or "").lower()
        value = c.get("displayValue") or ""
        if "rohs" in label and rohs is None:
            rohs = value

    # Inventory: walk InvOrg.webSites[].sources[].sourceParts[]
    inv_org = part.get("InvOrg") or {}
    websites = inv_org.get("webSites") or []
    sources_flat: list[dict] = []  # one entry per sourcePart
    for site in websites:
        if not isinstance(site, dict):
            continue
        site_code = site.get("code") or ""
        site_name = site.get("name") or site_code
        for src in site.get("sources") or []:
            if not isinstance(src, dict):
                continue
            currency = src.get("currency") or ""
            source_cd = src.get("sourceCd") or ""
            source_display = src.get("displayName") or source_cd
            for sp in src.get("sourceParts") or []:
                if not isinstance(sp, dict):
                    continue
                # Aggregate fohQty across all Availability rows; capture pipeline
                # entries (future shipments with date) separately.
                avail = sp.get("Availability") or []
                foh_total = 0
                avail_messages: list[str] = []
                pipeline_entries: list[dict] = []
                for a in avail:
                    if not isinstance(a, dict):
                        continue
                    q = _parse_int(a.get("fohQty"))
                    if q is not None:
                        foh_total += q
                    msg = a.get("availabilityMessage") or ""
                    if msg:
                        avail_messages.append(msg)
                    for pe in a.get("pipeline") or []:
                        if isinstance(pe, dict):
                            pipeline_entries.append(pe)
                # Pricing
                resale = (sp.get("Prices") or {}).get("resaleList") or []
                tiers: list[dict] = []
                for t in resale:
                    if not isinstance(t, dict):
                        continue
                    tiers.append({
                        "min_qty": _parse_int(t.get("minQty")),
                        "max_qty": _parse_int(t.get("maxQty")),
                        "unit_price": _parse_price(t.get("price")),
                        "unit_price_float": _parse_price(t.get("price")),
                        "display_price": t.get("displayPrice"),
                        "currency": currency,
                    })
                sources_flat.append({
                    "site_code": site_code,
                    "site_name": site_name,
                    "source_cd": source_cd,
                    "source_display": source_display,
                    "currency": currency,
                    "foh_qty": foh_total,
                    "availability_message": " / ".join(avail_messages),
                    "pipeline": pipeline_entries,
                    "mfr_lead_time_days": _parse_int(sp.get("mfrLeadTime")),
                    "arrow_lead_time": sp.get("arrowLeadTime") or "",
                    "ships_from": sp.get("shipsFrom") or "",
                    "ships_in": sp.get("shipsIn") or "",
                    "moq": _parse_int(sp.get("minimumOrderQuantity")),
                    "pack_size": _parse_int(sp.get("packSize")),
                    "date_code": sp.get("dateCode") or "",
                    "eccn_code": sp.get("eccnCode") or "",
                    "hts_code": sp.get("htsCode") or "",
                    "country_of_origin": sp.get("countryOfOrigin") or "",
                    "is_in_stock": bool(sp.get("inStock")),
                    "is_ncnr": bool(sp.get("isNcnr")),
                    "is_npi": bool(sp.get("isNpi")),
                    "tiers": tiers,
                    "resources": sp.get("resources") or [],
                })

    # Arrow republishes the same physical inventory under multiple `sources`
    # (e.g. a Verical 816K Netherlands pool also shows up as Arrow EUROPE 816K
    # Netherlands — same shipment, two sales channels). Dedup by the triple
    # `(fohQty, shipsFrom, shipsIn)` before summing; flag duplicates in the
    # breakdown so the buyer can see what got collapsed.
    seen_keys: set[tuple] = set()
    deduped_qty = 0
    for s in sources_flat:
        if s["foh_qty"] <= 0:
            s["is_mirror_of_earlier"] = False
            continue
        key = (s["foh_qty"], s["ships_from"], s["ships_in"])
        if key in seen_keys:
            s["is_mirror_of_earlier"] = True
            continue
        seen_keys.add(key)
        s["is_mirror_of_earlier"] = False
        deduped_qty += s["foh_qty"]
    stock_now_qty = deduped_qty
    raw_sum_with_mirrors = sum(s["foh_qty"] for s in sources_flat)
    in_stock_sources = [s for s in sources_flat if s["foh_qty"] > 0 and not s["is_mirror_of_earlier"]]
    if in_stock_sources:
        bits: list[str] = []
        for s in in_stock_sources:
            sh = s["ships_in"] or s["availability_message"]
            label = s["source_display"] or s["source_cd"]
            if sh:
                bits.append(f"{label}: {sh}")
            else:
                bits.append(label)
        stock_now_ship_text = " · ".join(bits) if bits else None
    else:
        stock_now_ship_text = None

    # Future stock: aggregate pipeline qty across sources. If none, treat the
    # manufacturer lead time as the "unbounded factory order" path.
    pipeline_total = 0
    pipeline_details: list[dict] = []
    lead_time_days_set = set()
    for s in sources_flat:
        for pe in s["pipeline"]:
            q = _parse_int(pe.get("qty") or pe.get("Qty") or pe.get("quantity"))
            if q is not None:
                pipeline_total += q
            pipeline_details.append({"source": s["source_display"], **pe})
        if s["mfr_lead_time_days"]:
            lead_time_days_set.add(s["mfr_lead_time_days"])

    if pipeline_total > 0:
        stock_future_qty = pipeline_total
        stock_future_ship_text = "Arrow pipeline (committed factory shipments)"
    elif lead_time_days_set:
        stock_future_qty = None  # unbounded factory order
        best_lead = min(lead_time_days_set)
        stock_future_ship_text = f"原厂标准交货期 {best_lead} 天 (Arrow mfrLeadTime, shortest of {sorted(lead_time_days_set)})"
    else:
        stock_future_qty = 0
        stock_future_ship_text = None

    # stock_breakdown: one row per source row
    breakdown: list[dict] = []
    for s in sources_flat:
        ship_bits: list[str] = []
        if s["is_in_stock"]:
            ship_bits.append("在库")
        if s["ships_in"]:
            ship_bits.append(s["ships_in"].strip())
        if s["mfr_lead_time_days"]:
            ship_bits.append(f"mfr lead {s['mfr_lead_time_days']} 天")
        label = f"{s['source_display']} ({s['site_code'] or s['site_name']})"
        if s.get("is_mirror_of_earlier"):
            label += " — mirror"
        note_bits: list[str] = []
        if s.get("is_mirror_of_earlier"):
            note_bits.append("mirror of earlier source (same qty+origin+ship-time); not counted in canonical 现货 total")
        if s["date_code"] or s["hts_code"] or s["eccn_code"]:
            note_bits.append(
                f"date code {s['date_code']}; HTS {s['hts_code']}; ECCN {s['eccn_code']}"
            )
        breakdown.append({
            "label": label,
            "warehouse": (
                f"Arrow / {s['source_cd']} — ships from {s['ships_from']}"
                if s["ships_from"] else f"Arrow / {s['source_cd']}"
            ),
            "quantity": s["foh_qty"],
            "ship_text": " · ".join(ship_bits),
            "moq": s["moq"],
            "note": "; ".join(note_bits),
        })
    if pipeline_details:
        for pe in pipeline_details:
            breakdown.append({
                "label": f"Pipeline ({pe.get('source','')})",
                "warehouse": "Arrow pipeline (committed factory shipment)",
                "quantity": _parse_int(pe.get("qty") or pe.get("Qty") or pe.get("quantity")),
                "ship_text": str(pe.get("date") or pe.get("Date") or pe.get("eta") or ""),
                "note": "Future shipment scheduled — not yet on shelf",
            })

    # Primary pricing: take the first in-stock source's tiers; fall back to
    # the first source with any tiers. Capture other sources' tiers as alt.
    primary_tiers: list[dict] = []
    alt_tiers: list[dict] = []
    for s in sources_flat:
        if not s["tiers"]:
            continue
        if not primary_tiers and (s["is_in_stock"] or s["foh_qty"] > 0):
            primary_tiers = s["tiers"]
            primary_currency = s["currency"]
        else:
            for t in s["tiers"]:
                alt_tiers.append({**t, "source_cd": s["source_cd"], "source_display": s["source_display"]})
    if not primary_tiers and sources_flat:
        for s in sources_flat:
            if s["tiers"]:
                primary_tiers = s["tiers"]
                primary_currency = s["currency"]
                break
        else:
            primary_currency = ""
    elif not sources_flat:
        primary_currency = ""

    # Promote first source's HTS/ECCN/COO/MOQ as headline values
    hts = next((s["hts_code"] for s in sources_flat if s["hts_code"]), "")
    eccn = next((s["eccn_code"] for s in sources_flat if s["eccn_code"]), "")
    coo = next((s["country_of_origin"] for s in sources_flat if s["country_of_origin"]), "")
    moq = next((s["moq"] for s in sources_flat if s["moq"] is not None), None)

    out: dict = {
        # Identity
        "arrow_item_id": item_id,
        "manufacturer_part_number": mpn,
        "manufacturer": manufacturer,
        "manufacturer_code": mfr_obj.get("mfrCd"),
        "description_en": desc,
        "datasheet_url": datasheet_url,
        "image_url": image_url,
        "product_url": product_url,
        "category_name_en": part.get("categoryName") or "",
        "lifecycle_status": part_status,
        "part_status": part_status,
        "is_rohs": rohs,
        "package": package_type,
        "min_order_qty": moq,
        "hts_code": hts,
        "eccn": eccn,
        "country_of_origin": coo,
        # Stock (canonical)
        "stock_now_qty": stock_now_qty,
        "stock_now_ship_text": stock_now_ship_text,
        "stock_future_qty": stock_future_qty,
        "stock_future_ship_text": stock_future_ship_text,
        "stock_breakdown": breakdown,
        # Stock (site-native)
        "site_sources": sources_flat,
        "site_websites_count": len(websites),
        "site_raw_sum_with_mirrors": raw_sum_with_mirrors,
        # Pricing
        "prices": primary_tiers,
        "prices_alt": alt_tiers,
        "currency": primary_currency,
        # Parameters (Arrow's search/list endpoint returns minimal params;
        # full parametrics live on the /detail endpoint which Basic-tier keys
        # may not have access to). Leave empty for now.
        "parameters": [],
    }
    return out


def assess_quality(ex: dict) -> str:
    has_part = bool(ex.get("manufacturer_part_number"))
    has_price = bool(ex.get("prices"))
    has_stock = ex.get("stock_now_qty") is not None
    if has_part and has_price and has_stock:
        return "high"
    if has_part and (has_price or has_stock):
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
    raw_path = sub / f"{folder_name}_raw_part.json"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = write_summary(variant_rec, sub, folder_name)
    return {"folder": folder_name, "subdir": str(sub),
            "json": str(json_path), "summary": str(summary_path), "extracted": ex}


def write_parent_summary(rec, variant_infos, run_dir):
    md: list[str] = []
    md.append(f"# Arrow API run — {rec.get('query')} ({CHANNEL})")
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
        md.append("| # | MPN | Arrow itemId | Manufacturer | 现货 | Sources | Price tiers | Currency |")
        md.append("|---|---|---|---|---|---|---|---|")
        for i, v in enumerate(variant_infos, 1):
            ex = v["extracted"]
            now = ex.get("stock_now_qty")
            sources = ex.get("site_sources") or []
            md.append(
                "| {i} | {mpn} | {iid} | {mfr} | {now} | {nsrc} | {tiers} | {cur} |".format(
                    i=i,
                    mpn=ex.get("manufacturer_part_number") or "",
                    iid=ex.get("arrow_item_id") or "",
                    mfr=ex.get("manufacturer") or "",
                    now=f"{now:,}" if isinstance(now, int) else (now or ""),
                    nsrc=len(sources),
                    tiers=len(ex.get("prices") or []),
                    cur=ex.get("currency") or "",
                )
            )
        md.append("")
    md.append("## Note on Arrow's stock model")
    md.append("")
    md.append(
        "Arrow exposes inventory through multiple sales channels per part: Arrow.com "
        "(regional warehouses) + Verical.com (Arrow's spot-buy marketplace) + any "
        "supplier-direct channels. Each appears as a `sourceParts[]` entry under "
        "`InvOrg.webSites[].sources[]`. We sum `fohQty` (free-on-hand) across all "
        "sources for the canonical 现货 quantity and emit one `stock_breakdown` row "
        "per source so the buyer can see which warehouse / which currency / what "
        "shipping SLA applies. Future shipments scheduled by Arrow (Availability."
        "pipeline[]) are surfaced as separate 'Pipeline' rows; manufacturer lead "
        "time falls back to the unbounded-factory-order path."
    )
    md.append("")
    md.append("## Attempts")
    md.append("")
    md.append("| # | Method | Profile | Status | Len | Outcome | Return msg |")
    md.append("|---|---|---|---|---|---|---|")
    for i, a in enumerate(rec.get("attempts") or [], 1):
        md.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                i, a.get("method", ""), a.get("profile", ""),
                a.get("status", ""), a.get("len", ""),
                a.get("outcome", ""), (a.get("return_msg") or "")[:60],
            )
        )
    md.append("")
    out = run_dir / "parent_summary.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out


def call_api(query: str, run_dir: Path) -> dict:
    load_dotenv(PROJECT_ROOT / "api" / ".env", override=True)
    login = os.environ.get("ARROW_LOGIN", "").strip()
    api_key = os.environ.get("ARROW_API_KEY", "").strip()

    rec: dict = {
        "query": query,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "api.arrow.com/itemservice/v4",
        "search_url": f"{API_BASE}/search/list",
        "output_dir": str(run_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }
    if not login or not api_key:
        rec["status"] = "missing_credentials"
        rec["blocker"] = "ARROW_LOGIN and/or ARROW_API_KEY not set in api/.env"
        return rec

    # 1. Exact-match search
    a1 = call_search_list(login, api_key, query, use_exact=True)
    parts = a1.pop("parts", None)
    payload = a1.pop("payload", None)
    rec["attempts"].append(a1)
    print(
        f"[arrow] search/list (exact): outcome={a1.get('outcome')} "
        f"status={a1.get('status')} num={a1.get('num_results')} "
        f"msg={(a1.get('return_msg') or '')[:60]!r}"
    )
    if a1.get("outcome") == "ok":
        rec["method"] = METHOD_TAG
        rec["raw_payload"] = payload
        rec["parts"] = parts
        rec["status"] = "ok"
        return rec

    # 2. Fallback: useExact=false (looser match)
    if a1.get("outcome") == "no_results":
        a2 = call_search_list(login, api_key, query, use_exact=False)
        parts = a2.pop("parts", None)
        payload = a2.pop("payload", None)
        rec["attempts"].append(a2)
        print(
            f"[arrow] search/list (fuzzy): outcome={a2.get('outcome')} "
            f"status={a2.get('status')} num={a2.get('num_results')}"
        )
        if a2.get("outcome") == "ok":
            rec["method"] = METHOD_TAG
            rec["raw_payload"] = payload
            rec["parts"] = parts
            rec["status"] = "ok"
            return rec

    rec["status"] = (
        "auth_failed" if a1.get("outcome") == "auth_failed" else "no_results"
    )
    return rec


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mpn", nargs="?", default="STM32G030F6P6")
    args = parser.parse_args(argv[1:])

    part = args.mpn
    run_dir = make_run_dir(part)
    print(f"=== ARROW API: {part} ===")
    print(f"output folder: {run_dir}")

    rec = call_api(part, run_dir)
    payload = rec.pop("raw_payload", None)
    parts = rec.pop("parts", None)

    if payload is not None:
        (run_dir / "raw_response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    variant_infos: list[dict] = []
    if rec.get("status") == "ok" and parts:
        # Group by exact MPN string returned by Arrow (per project rule).
        seen: dict[str, dict] = {}
        for raw in parts:
            ex = normalize_part(raw, part)
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
            "arrow_item_id": v["extracted"].get("arrow_item_id"),
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
            f"  - {ex.get('manufacturer_part_number')} (itemId {ex.get('arrow_item_id')}): "
            f"现货={ex.get('stock_now_qty')} sources={len(ex.get('site_sources') or [])} "
            f"tiers={len(ex.get('prices') or [])} cur={ex.get('currency')}"
        )
    print(f"status: {rec.get('status')}  method: {rec.get('method')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
