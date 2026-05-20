"""LCSC (立创商城 / 嘉立创开放平台) product search API client.

POSTs `https://open-api.jlc.com/lcsc/openapi/product/search/global` with
`{"keyword": <MPN>}` and normalizes results into the canonical 现货/期货
schema. Authenticated with HMAC-SHA256 over a 5-line string-to-sign per the
docs in `ref/lcsc立创商城_API_doc/立创商城-接口文档-请求签名.pdf`.

Auth scheme (verified offline against the doc's worked example):
    Authorization: JOP appid="…",accesskey="…",nonce="…",timestamp="…",signature="…"

where
    signature = base64( HMAC-SHA256(string_to_sign, SecretKey) )
    string_to_sign = "{METHOD}\n{path[?query]}\n{timestamp}\n{nonce}\n{body}\n"

Folder convention: test/api_test/Test_<MPN>_LCSC_<YYYYMMDD>_<HH>_<MM>_<SS>/
with per-variant subfolders.

Usage:
    .venv/Scripts/python.exe api/scripts/api_lcsc.py <MPN>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
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
CHANNEL = "LCSC"
API_HOST = "https://open-api.jlc.com"
SEARCH_PATH = "/lcsc/openapi/product/search/global"
METHOD_TAG = "api_lcsc_v1"

# Doc-verified golden case for offline self-test of the signature pipeline.
# If this assertion ever fails, the signing code is broken and we must NOT
# burn rate-limit credits hitting the real server with bad auth.
_SIG_SELFTEST = {
    "secret_key": "z0BWlikshimuyiwBsH1i2qwnzMb3j3kA",
    "string_to_sign": (
        "POST\n"
        "/order/v1/createOrder\n"
        "1625208260\n"
        "IZHEJYNIHYZIE8S0LLC0VWTPJVRRTO50\n"
        '{"goodsId":100,"quantity":52,"createdTime":"2024-03-21 10:03:20"}\n'
    ),
    "expected": "sygwKhKBkLwHVv0c7D+a/A7JTEJjGH/kLugFKh16918=",
}


# --- helpers ----------------------------------------------------------------


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


# --- signature --------------------------------------------------------------


def _build_string_to_sign(method: str, path_with_query: str, timestamp: int,
                          nonce: str, body: str) -> str:
    return (
        f"{method}\n"
        f"{path_with_query}\n"
        f"{timestamp}\n"
        f"{nonce}\n"
        f"{body}\n"
    )


def _sign(string_to_sign: str, secret_key: str) -> str:
    digest = hmac.new(
        secret_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _make_nonce() -> str:
    """32-char alphanumeric per the doc spec."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(32))


def _selftest_signature() -> None:
    """Refuse to call the real API if the signature pipeline is broken.
    Runs once at module import; cheap and side-effect-free."""
    sig = _sign(_SIG_SELFTEST["string_to_sign"], _SIG_SELFTEST["secret_key"])
    if sig != _SIG_SELFTEST["expected"]:
        raise RuntimeError(
            "api_lcsc signature self-test FAILED. "
            f"Computed {sig!r} but doc-verified value is {_SIG_SELFTEST['expected']!r}. "
            "Refusing to call live API with a broken signing pipeline."
        )


_selftest_signature()


def _build_auth_header(app_id: str, access_key: str, secret_key: str,
                       method: str, path_with_query: str, body: str
                       ) -> tuple[str, dict]:
    """Returns (Authorization header value, debug info dict). The debug info is
    safe to log — it contains nonce + timestamp + masked secrets — and is
    captured in attempts[] for audit."""
    timestamp = int(time.time())
    nonce = _make_nonce()
    sts = _build_string_to_sign(method, path_with_query, timestamp, nonce, body)
    signature = _sign(sts, secret_key)
    header = (
        f'JOP appid="{app_id}",'
        f'accesskey="{access_key}",'
        f'nonce="{nonce}",'
        f'timestamp="{timestamp}",'
        f'signature="{signature}"'
    )
    debug = {
        "timestamp": timestamp,
        "nonce": nonce,
        "string_to_sign_preview": sts[:200],
        "sig_len": len(signature),
    }
    return header, debug


# --- API call ---------------------------------------------------------------


def call_search(app_id: str, access_key: str, secret_key: str, mpn: str) -> dict:
    attempt: dict = {
        "method": METHOD_TAG,
        "profile": "product_search_global_keyword",
        "url": API_HOST + SEARCH_PATH,
    }
    # CRITICAL: the body bytes used for signing MUST be byte-identical to what
    # we send over the wire. Don't use `requests.post(json=…)` (it re-serializes
    # with default separators); serialize once, pass as `data=`.
    body_obj = {"keyword": mpn}
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    auth_header, sig_debug = _build_auth_header(
        app_id, access_key, secret_key, "POST", SEARCH_PATH, body
    )
    attempt["sig_debug"] = sig_debug
    try:
        r = requests.post(
            API_HOST + SEARCH_PATH,
            data=body.encode("utf-8"),
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/json",
                "Accept": "application/json",
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
        code = payload.get("code")
        successful = payload.get("successful", False)
        data = payload.get("data") or []
        if code != 0 and code != 200:
            attempt["outcome"] = "api_error"
            attempt["error"] = f"code={code} message={payload.get('message', '')[:200]}"
            attempt["payload"] = payload
            return attempt
        if not successful:
            attempt["outcome"] = "api_error"
            attempt["error"] = f"successful=false; message={payload.get('message', '')[:200]}"
            attempt["payload"] = payload
            return attempt
        n = len(data) if isinstance(data, list) else 0
        attempt["num_results"] = n
        attempt["outcome"] = "ok" if n > 0 else "no_results"
        attempt["payload"] = payload
        # On no_results, echo the full body excerpt for diagnostics
        if n == 0:
            attempt["body_excerpt"] = r.text[:500]
    except requests.RequestException as exc:
        attempt["outcome"] = "exception"
        attempt["error"] = str(exc)
    return attempt


# --- normalization ----------------------------------------------------------


def normalize_product(prod: dict, query: str) -> dict:
    """Map one LCSC product entry to the canonical schema."""
    mpn = str(prod.get("productModel") or "").strip()
    brand = str(prod.get("brandName") or "").strip()  # may contain "(中文名)"
    sku = str(prod.get("productCode") or "").strip()  # e.g. "C60568"
    product_id = prod.get("productId")
    name = prod.get("productName") or ""
    description = prod.get("description") or ""
    package = prod.get("standard") or ""

    # Stock: two warehouses (广东 + 江苏).
    gd_qty = _parse_int(prod.get("gdStockNum")) or 0
    js_qty = _parse_int(prod.get("jsStockNum")) or 0
    stock_now_qty = gd_qty + js_qty
    stock_now_ship_text = "立创商城 在库,下单后立即发货" if stock_now_qty > 0 else None

    # LCSC search endpoint doesn't expose factory lead time → no future stock.
    stock_future_qty = 0
    stock_future_ship_text = None

    breakdown: list[dict] = []
    breakdown.append({
        "label": "广东仓",
        "warehouse": "LCSC / 广东仓",
        "quantity": gd_qty,
        "ship_text": "在库 · 下单后立即发货" if gd_qty > 0 else "无库存",
        "note": "",
    })
    breakdown.append({
        "label": "江苏仓",
        "warehouse": "LCSC / 江苏仓",
        "quantity": js_qty,
        "ship_text": "在库 · 下单后立即发货" if js_qty > 0 else "无库存",
        "note": "",
    })

    # Prices: priceList[] of {startStep, originPrice, discountedPrice}.
    # Use discountedPrice as the canonical unit price (actual selling price);
    # keep originPrice as a site-native field per row.
    prices: list[dict] = []
    for tier in prod.get("priceList") or []:
        if not isinstance(tier, dict):
            continue
        min_qty = _parse_int(tier.get("startStep"))
        disc = _parse_price(tier.get("discountedPrice"))
        orig = _parse_price(tier.get("originPrice"))
        unit = disc if disc is not None else orig
        prices.append({
            "min_qty": min_qty,
            "unit_price": unit,
            "unit_price_float": unit,
            "currency": "CNY",
            "site_origin_price": orig,
            "site_discounted_price": disc,
        })

    product_url = f"https://www.szlcsc.com/product-detail/{sku}.html" if sku else None

    out: dict = {
        # Identity
        "lcsc_product_id": product_id,
        "lcsc_sku": sku,
        "manufacturer_part_number": mpn,
        "manufacturer": brand,
        "description_en": name,
        "description_cn": description,
        "datasheet_url": None,  # not exposed by search endpoint
        "image_url": None,
        "product_url": product_url,
        "lifecycle_status": "",
        "part_status": "",
        "is_rohs": None,
        "package": package,
        # Stock (canonical)
        "stock_now_qty": stock_now_qty,
        "stock_now_ship_text": stock_now_ship_text,
        "stock_future_qty": stock_future_qty,
        "stock_future_ship_text": stock_future_ship_text,
        "stock_breakdown": breakdown,
        # Stock (site-native)
        "site_gd_stock_num": gd_qty,
        "site_js_stock_num": js_qty,
        # Pricing
        "prices": prices,
        "currency": "CNY",
        # Parameters (search endpoint doesn't return spec params; would need
        # the商品基础信息查询 follow-up call by productId for those).
        "parameters": [],
    }
    return out


def assess_quality(ex: dict) -> str:
    has_part = bool(ex.get("manufacturer_part_number"))
    has_price = bool(ex.get("prices"))
    has_stock_field = ex.get("stock_now_qty") is not None
    if has_part and has_price and has_stock_field:
        return "high"
    if has_part and (has_price or has_stock_field):
        return "medium"
    if has_part:
        return "low"
    return "none"


# --- writers ----------------------------------------------------------------


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
    md.append(f"# LCSC API run — {rec.get('query')} ({CHANNEL})")
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
        md.append("| # | MPN | LCSC SKU | Brand | 现货 (qty) | 广东仓 | 江苏仓 | Price tiers |")
        md.append("|---|---|---|---|---|---|---|---|")
        for i, v in enumerate(variant_infos, 1):
            ex = v["extracted"]
            now = ex.get("stock_now_qty")
            md.append(
                "| {i} | {mpn} | {sku} | {br} | {now} | {gd} | {js} | {tiers} |".format(
                    i=i,
                    mpn=ex.get("manufacturer_part_number") or "",
                    sku=ex.get("lcsc_sku") or "",
                    br=ex.get("manufacturer") or "",
                    now=f"{now:,}" if isinstance(now, int) else (now or ""),
                    gd=f"{ex.get('site_gd_stock_num', 0):,}",
                    js=f"{ex.get('site_js_stock_num', 0):,}",
                    tiers=len(ex.get("prices") or []),
                )
            )
        md.append("")
    md.append("## Note on LCSC's stock model")
    md.append("")
    md.append(
        "The `/lcsc/openapi/product/search/global` endpoint returns two warehouse "
        "pools per product — `gdStockNum` (广东仓) and `jsStockNum` (江苏仓). "
        "The canonical 现货 quantity is their sum; per-warehouse rows are kept in "
        "`stock_breakdown`. The search endpoint does NOT expose factory lead time "
        "or detailed parameters — those require follow-up calls to the商品基础信息查询 "
        "endpoint (`/lcsc/openapi/sku/product/basic`) keyed by productId."
    )
    md.append("")
    md.append("## Attempts")
    md.append("")
    md.append("| # | Method | Profile | Status | Len | Outcome |")
    md.append("|---|---|---|---|---|---|")
    for i, a in enumerate(rec.get("attempts") or [], 1):
        md.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                i, a.get("method", ""), a.get("profile", ""),
                a.get("status", ""), a.get("len", ""), a.get("outcome", ""),
            )
        )
    md.append("")
    out = run_dir / "parent_summary.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out


# --- entry points ----------------------------------------------------------


def call_api(query: str, run_dir: Path) -> dict:
    load_dotenv(PROJECT_ROOT / "api" / ".env")
    app_id = os.environ.get("lcsc_AppID", "").strip()
    access_key = os.environ.get("lcsc_AccessKey", "").strip()
    secret_key = os.environ.get("lcsc_SecretKey", "").strip()

    rec: dict = {
        "query": query,
        "channel": CHANNEL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "open-api.jlc.com/lcsc/openapi/product/search/global",
        "search_url": API_HOST + SEARCH_PATH,
        "output_dir": str(run_dir),
        "method": "failed",
        "paywall": "none",
        "attempts": [],
        "data_quality": "none",
    }
    if not app_id or not access_key or not secret_key:
        rec["status"] = "missing_credentials"
        rec["blocker"] = "lcsc_AppID / lcsc_AccessKey / lcsc_SecretKey not all set in api/.env"
        return rec

    a = call_search(app_id, access_key, secret_key, query)
    payload = a.pop("payload", None)
    rec["attempts"].append(a)
    print(
        f"[lcsc] product/search/global: outcome={a.get('outcome')} "
        f"status={a.get('status')} num={a.get('num_results')}"
    )
    if a.get("outcome") == "ok":
        rec["method"] = METHOD_TAG
        rec["raw_payload"] = payload
        rec["status"] = "ok"
        return rec
    if a.get("outcome") == "http_error" and a.get("status") in (401, 403):
        rec["status"] = "auth_failed"
        rec["blocker"] = a.get("error", "")
        return rec

    rec["status"] = a.get("outcome") or "no_results"
    return rec


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mpn", nargs="?", default="STM32G030F6P6")
    args = parser.parse_args(argv[1:])

    part = args.mpn
    run_dir = make_run_dir(part)
    print(f"=== LCSC API: {part} ===")
    print(f"output folder: {run_dir}")

    rec = call_api(part, run_dir)
    payload = rec.pop("raw_payload", None)
    if payload is not None:
        (run_dir / "raw_response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    variant_infos: list[dict] = []
    if rec.get("status") == "ok" and payload is not None:
        data = payload.get("data") or []
        # Group by exact returned MPN string — different productIds with the
        # same productModel get the highest-stock representative.
        seen: dict[str, dict] = {}
        for raw in data:
            ex = normalize_product(raw, part)
            mpn = ex.get("manufacturer_part_number") or "UNKNOWN"
            prev = seen.get(mpn)
            if prev is None or (
                (ex.get("stock_now_qty") or 0)
                > (prev["extracted"].get("stock_now_qty") or 0)
            ):
                seen[mpn] = {"raw": raw, "extracted": ex}
        for mpn, bundle in seen.items():
            info = write_variant(rec, bundle["extracted"], bundle["raw"], run_dir, mpn)
            variant_infos.append(info)

    parent_rec = dict(rec)
    parent_rec["variants_summary"] = [
        {
            "manufacturer_part_number": v["extracted"].get("manufacturer_part_number"),
            "lcsc_sku": v["extracted"].get("lcsc_sku"),
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
            f"  - {ex.get('manufacturer_part_number')} ({ex.get('lcsc_sku')}): "
            f"现货={ex.get('stock_now_qty')} (广东 {ex.get('site_gd_stock_num')} / "
            f"江苏 {ex.get('site_js_stock_num')}) tiers={len(ex.get('prices') or [])}"
        )
    print(f"status: {rec.get('status')}  method: {rec.get('method')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
