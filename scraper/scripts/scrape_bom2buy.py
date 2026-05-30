"""bom2buy.com (买芯片网) — multi-distributor BOM aggregator scraper.

Site model: bom2buy aggregates authorized-distributor listings (Digi-Key, element14,
Mouser, Wuhan P&S, RS Components, Future, STMicro 直营 …) into one search page.
Each search returns N MPN variants; each variant has K distributor rows with
stock + multi-currency price tiers + region/lead-time.

Anti-bot:  IconCaptcha gate on every commerce/category URL. Cannot be bypassed
by curl_cffi or Playwright Chromium. Solved by reusing the user's already-
verified browser session: launch Opera via Playwright pointing at the user's
Opera user-data-dir (which inherits the captcha-cleared cookies).

Operational constraints:
- Opera must be FULLY CLOSED before this script runs (Playwright takes
  exclusive lock on user-data-dir).
- Captcha session is finite (hours to days). If expired, this script detects
  the captcha redirect on the homepage warmup, prints a clear user-action
  message, and exits with code 3 — so `batch_scraper_test.py` can SKIP this
  source for the whole batch without failing other channels.

Output convention (matches scraper_report_v3.md):
- Single-variant cell  → flat per-MPN folder
- Multi-variant cell   → parent_summary.md + per-variant subfolder (LCSC v3 pattern)

Usage:
  Single MPN:   .venv/Scripts/python.exe scrape_bom2buy.py <MPN> [out_dir]
  Batch:        .venv/Scripts/python.exe scrape_bom2buy.py --mpns "MPN1;MPN2;..." --out <root_dir>
  Batch (file): .venv/Scripts/python.exe scrape_bom2buy.py --mpns-file <path> --out <root_dir>

  --mpns-file: one MPN per line; lines starting with # ignored; "MPN:MFR" format also accepted.

Exit codes:
   0  ok
   2  unrecoverable error (e.g. Opera profile lock, can't launch)
   3  captcha session expired — user action required (batch driver should skip)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "test" / "scraper"
CHANNEL = "BOM2BUY"

_sys = sys
sys.path.insert(0, str(PROJECT_ROOT / "common"))
from _summary import write_summary  # type: ignore

# Session config — Opera install paths
OPERA_EXE = os.environ.get(
    "OPERA_EXE", str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Opera" / "opera.exe")
)
OPERA_USER_DATA = os.environ.get(
    "OPERA_USER_DATA", str(Path(os.environ.get("APPDATA", "")) / "Opera Software" / "Opera Stable")
)

BASE_URL = "https://www.bom2buy.com"
HOME_URL = BASE_URL + "/"
SEARCH_URL = BASE_URL + "/search?part={mpn}&qty=1"

# Rate limiting (batch mode)
PER_MPN_DELAY_SEC = 3.0          # pause between consecutive MPNs
LONG_DELAY_EVERY_N = 50          # every N MPNs, take a longer pause
LONG_DELAY_SEC = 30.0
NAV_TIMEOUT_MS = 30_000

# Captcha detection markers
CAPTCHA_HOST = "captcha.bom2buy.com"
CAPTCHA_TITLE = "Captcha"


# ─────────────────────────── Folder layout ───────────────────────────

def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def make_run_dir(part: str) -> Path:
    """Single-MPN entry: test/scraper/Test_<MPN>_BOM2BUY_<ts>/"""
    now = datetime.now()
    name = f"Test_{_safe(part)}_{CHANNEL}_{now.strftime('%Y%m%d')}_{now.strftime('%H_%M_%S')}"
    out = TEST_ROOT / name
    out.mkdir(parents=True, exist_ok=True)
    return out


# ─────────────────────────── Captcha check ───────────────────────────

class CaptchaRequired(RuntimeError):
    """Raised when bom2buy session is expired and user must manually re-pass the captcha."""


def _is_captcha_page(page: Page) -> bool:
    """Determine whether the current page is the bom2buy captcha gate."""
    try:
        url = page.url
        title = page.title()
    except Exception:
        return False
    if CAPTCHA_HOST in url:
        return True
    if title.strip().lower() == CAPTCHA_TITLE.lower():
        return True
    return False


def verify_session(page: Page) -> None:
    """Hit the homepage, raise CaptchaRequired if the session has expired."""
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    time.sleep(2)
    if _is_captcha_page(page):
        raise CaptchaRequired(
            "bom2buy session expired (captcha redirect on homepage).\n\n"
            "ACTION REQUIRED:\n"
            "  1. Open Opera manually and navigate to https://www.bom2buy.com/\n"
            "  2. Solve the IconCaptcha when prompted\n"
            "  3. FULLY CLOSE Opera (Task Manager → kill all opera.exe processes if needed)\n"
            "  4. Re-run this script\n"
            "Until fixed, BOM2BUY is being skipped — other channels are unaffected."
        )


# ─────────────────────────── DOM extraction ───────────────────────────

def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


_PRICE_RE = re.compile(r"[¥￥]\s*([\d.,]+)")
_USD_RE = re.compile(r"\$\s*([\d.,]+)")
_QTY_BREAK_RE = re.compile(r"^([\d,]+)\s*\+\s*$")


def _parse_float(s: str) -> Optional[float]:
    try:
        return float((s or "").replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse_int(s: str) -> Optional[int]:
    try:
        return int(re.sub(r"[^\d-]", "", (s or "")))
    except (ValueError, AttributeError):
        return None


def _extract_header_meta(group_el) -> dict:
    """Parse the .search-group-header text block for searched-part metadata.
    Returns {datasheet_url, lifecycle_status, category, description, search_hotness}."""
    meta = {
        "datasheet_url": None,
        "lifecycle_status": None,
        "category": None,
        "description": None,
        "search_hotness": None,
    }
    hdr = group_el.select_one(".search-group-header")
    if not hdr:
        return meta
    text = hdr.get_text(separator="\n", strip=True)
    if m := re.search(r"搜索热度[:：]\s*([\d-]+)", text):
        meta["search_hotness"] = _parse_int(m.group(1))
    if m := re.search(r"生命周期[:：]\s*([^\n]+)", text):
        meta["lifecycle_status"] = _clean(m.group(1))
    if m := re.search(r"类别[:：]\s*([^\n]+)", text):
        meta["category"] = _clean(m.group(1))
    # Datasheet anchor
    ds = hdr.select_one('a[href*=".pdf"], a[href*="datasheet"], a[title*="数据手册"]')
    if ds:
        href = ds.get("href") or ""
        if href and href.lower() != "javascript:;" and not href.startswith("javascript:"):
            if href.startswith("/"):
                href = BASE_URL + href
            meta["datasheet_url"] = href
    # Description: in the header, after the MPN, before the table — try to find a description-like span
    desc_el = hdr.select_one(".info-part-text + *, .part-description, .info-part-description")
    if desc_el:
        d = _clean(desc_el.get_text())
        if d and d != "暂无描述":
            meta["description"] = d
    return meta


def _extract_distributor_row(tr) -> Optional[dict]:
    """Parse one <tr> from a variant's distributor table into a structured dict.

    bom2buy uses these per-row td classes (confirmed 2026-05-20):
      .td-distri          → distributor name + authorized flag + MPN + mfr + SKU
      .td-datecode        → date code (often '-')
      .td-stock           → stock quantity (integer, may be comma-formatted) + optional package text
      .td-delivery-place  → region + lead-time string (multi-region pipe-separated)
      .td-price           → price tiers
      .td-min-pack        → MOQ + increment + subtotal
      .td-buy             → '加入BOM' purchase button (used as the row-validity sentinel)
    """
    td_buy = tr.select_one(".td-buy")
    if not td_buy or "加入BOM" not in td_buy.get_text():
        # Header / empty / wrap row
        return None

    # ---- distributor metadata cell ----
    td_distri = tr.select_one(".td-distri")
    distri_text = _clean(td_distri.get_text(separator=" ")) if td_distri else ""
    distri_name = None
    is_authorized = "授权" in distri_text
    mpn = mfr = sku = None
    if td_distri:
        # Distributor name = the text before any of "授权" / "型号" / "制造商" markers
        if m := re.match(r"^(.+?)\s*(?:授权|型号[:：]|制造商[:：]|分销商编号[:：])", distri_text):
            distri_name = _clean(m.group(1))
        else:
            # Non-authorized rows may not have 授权 marker — strip after "型号" only
            if m := re.match(r"^(.+?)\s+型号[:：]", distri_text):
                distri_name = _clean(m.group(1))
            else:
                distri_name = distri_text[:40].strip() or None
    if m := re.search(r"型号[:：]\s*(\S+)", distri_text):
        mpn = _clean(m.group(1))
    if m := re.search(r"制造商[:：]\s*([^分]+?)(?:\s*分销商编号|$)", distri_text):
        mfr = _clean(m.group(1))
    if m := re.search(r"分销商编号[:：]\s*(\S+)", distri_text):
        sku = _clean(m.group(1))

    # ---- stock cell ----
    td_stock = tr.select_one(".td-stock")
    stock_qty = None
    package_info = None
    if td_stock:
        stock_text = _clean(td_stock.get_text(separator=" "))
        # Stock text may be like "13,159" or "76,624 Tray" or "-"
        if stock_text and stock_text != "-":
            if m := re.match(r"([\d,]+)\s*(.*)?", stock_text):
                stock_qty = _parse_int(m.group(1))
                pkg_tail = (m.group(2) or "").strip()
                if pkg_tail and pkg_tail not in ("-", ""):
                    package_info = pkg_tail

    # ---- delivery / ship_text ----
    td_delivery = tr.select_one(".td-delivery-place")
    ship_text = None
    if td_delivery:
        raw = td_delivery.get_text(separator="\n", strip=True)
        if raw:
            # Normalize: collapse whitespace, keep region prefixes intact
            parts = []
            for line in raw.splitlines():
                line = _clean(line)
                if line and line != "-":
                    parts.append(line)
            ship_text = " | ".join(parts) if parts else None

    # ---- price tiers ----
    prices = []
    td_price = tr.select_one(".td-price")
    if td_price:
        price_text = td_price.get_text(separator="\n", strip=True)
        # Pattern per tier: "<qty>+\n¥<cny>\n$<usd>" (USD optional, may be "-")
        # Walk the text line-by-line.
        lines = [_clean(l) for l in price_text.splitlines() if _clean(l)]
        i = 0
        while i < len(lines):
            qty_match = _QTY_BREAK_RE.match(lines[i])
            if not qty_match:
                i += 1; continue
            qty = _parse_int(lines[i])
            cny = usd = None
            # Look at next 1-2 lines for ¥ then $
            for j in range(i + 1, min(i + 3, len(lines))):
                line = lines[j]
                if line.startswith("¥") or line.startswith("￥"):
                    if pm := _PRICE_RE.search(line):
                        cny = _parse_float(pm.group(1))
                elif line.startswith("$"):
                    if pm := _USD_RE.search(line):
                        usd = _parse_float(pm.group(1))
                elif _QTY_BREAK_RE.match(line):
                    break
            if qty and (cny is not None or usd is not None):
                prices.append({
                    "min_qty": qty,
                    "unit_price": cny,
                    "unit_price_usd": usd,
                    "currency": "CNY",
                })
            i += 1

    # ---- MOQ + increment ----
    td_minpack = tr.select_one(".td-min-pack")
    moq = increment = None
    if td_minpack:
        mt = td_minpack.get_text(separator="\n", strip=True)
        if m := re.search(r"起订量[:：]\s*([\d,]+)", mt):
            moq = _parse_int(m.group(1))
        if m := re.search(r"递增量[:：]\s*([\d,]+)", mt):
            increment = _parse_int(m.group(1))

    if not distri_name:
        return None
    return {
        "distributor": distri_name,
        "authorized": is_authorized,
        "listed_mpn": mpn,
        "manufacturer": mfr,
        "distributor_sku": sku,
        "stock_qty": stock_qty,
        "package_info": package_info,
        "ship_text": ship_text,
        "prices": prices,
        "moq": moq,
        "increment": increment,
    }


def _extract_variants(html: str, input_mpn: str) -> tuple[list[dict], dict]:
    """Walk the search-result page HTML and extract a list of variants.

    Returns (variants, page_meta) where:
      page_meta = {exact_match_count, has_search_results}
      variants = list of {variant_mpn, header_meta..., distributors[...]}

    NB: bom2buy keeps a hidden `<div class="exact-no-result hide" style="display:none">`
    template in EVERY search page (for the empty-result fallback). So we cannot use
    substring `没有找到` as a "no results" signal — that would always fire false. The
    authoritative check is whether `.exact-part-group-list .distributor-results` exists.
    """
    soup = BeautifulSoup(html, "lxml")
    # Match-count header — handles non-breaking space \xa0 between text and (N)
    title_node = soup.select_one(".title-with-left-border")
    exact_match_count = 0
    if title_node:
        title_txt = title_node.get_text(separator=" ", strip=True)
        if m := re.search(r"完全匹配型号[\s\xa0]*\(\s*(\d+)\s*\)", title_txt):
            exact_match_count = int(m.group(1))
    # Walk the exact-match group container
    exact_root = soup.select_one(".exact-part-group-list")
    if not exact_root:
        return [], {"exact_match_count": exact_match_count, "has_search_results": False}

    # Authoritative "no results" check: exact_root contains a VISIBLE .exact-no-result
    visible_no_result = exact_root.select_one(".exact-no-result:not(.hide)")
    if visible_no_result and "display: none" not in (visible_no_result.get("style") or ""):
        return [], {"exact_match_count": 0, "has_search_results": False}

    variants = []
    for group in exact_root.select(".distributor-results"):
        variant_mpn = group.get("data-part") or ""
        if not variant_mpn:
            continue
        hdr_meta = _extract_header_meta(group)
        # Per-distributor rows
        distributors = []
        tbl = group.select_one("table")
        if tbl:
            for tr in tbl.select("tbody tr"):
                d = _extract_distributor_row(tr)
                if d:
                    distributors.append(d)
        variants.append({
            "variant_mpn": variant_mpn,
            **hdr_meta,
            "distributors": distributors,
        })
    return variants, {"exact_match_count": exact_match_count, "has_search_results": True}


# ─────────────────────────── Canonical schema mapping ───────────────────────────

def _canonical_from_variant(v: dict, input_mpn: str) -> dict:
    """Build the canonical extracted dict for one variant.

    Multi-row same-distributor dedup:
      bom2buy may show the same distributor name on multiple `tbody tr` rows when
      it carries multiple packaging variants of the part (e.g. RS Components with
      SKUs 2396332 / 2396333 / 2396333P — three packaging options of the same
      chip from the same distributor). For canonical `stock_breakdown[]`, we keep
      only the FIRST row per distributor name. The full multi-row detail is
      preserved in `site_distributors[]` for any downstream tool that needs it.

    Per-distributor price tiers:
      Each distributor has its OWN tier structure on bom2buy. The canonical
      top-level `prices[]` is the cheapest authorized distributor's tiers
      (single picked source). Each `stock_breakdown[]` entry ALSO carries its
      OWN `prices` field so a downstream warehouse-exploded export
      (e.g. batch_index.csv) can render per-warehouse prices instead of
      repeating the cell-level prices across all rows.
    """
    raw_distributors = v.get("distributors") or []
    # ---- Dedup by distributor name, keep first occurrence ----
    seen_names: set[str] = set()
    distributors: list[dict] = []
    for d in raw_distributors:
        name = d.get("distributor") or ""
        if not name:
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        distributors.append(d)

    # Top-level scalars
    mfr = next((d.get("manufacturer") for d in distributors if d.get("manufacturer")), None)
    # Aggregate stock across distinct distributors (no double-counting packaging dupes)
    stock_now = sum((d.get("stock_qty") or 0) for d in distributors)
    # Pick prices from the cheapest authorized distributor with tier prices (else first with prices)
    auth_with_prices = [d for d in distributors if d.get("authorized") and d.get("prices")]
    cheapest_authorized = None
    if auth_with_prices:
        # Cheapest = lowest unit_price at the highest min_qty break
        def _last_price(d):
            ps = sorted([p for p in d["prices"] if p.get("unit_price") is not None],
                        key=lambda p: p["min_qty"])
            return ps[-1]["unit_price"] if ps else float("inf")
        cheapest_authorized = min(auth_with_prices, key=_last_price)
    elif any(d.get("prices") for d in distributors):
        cheapest_authorized = next(d for d in distributors if d.get("prices"))
    prices = []
    if cheapest_authorized:
        prices = [
            {"min_qty": p["min_qty"], "unit_price": p["unit_price"], "currency": "CNY"}
            for p in cheapest_authorized["prices"]
            if p.get("min_qty") and p.get("unit_price") is not None
        ]
    # MOQ — minimum across distributors
    moqs = [d.get("moq") for d in distributors if d.get("moq")]
    min_order_qty = min(moqs) if moqs else None
    # stock_breakdown[] — one row per DISTINCT distributor with its own price tiers
    breakdown = []
    for d in distributors:
        per_row_prices = [
            {"min_qty": p["min_qty"], "unit_price": p["unit_price"], "currency": "CNY"}
            for p in (d.get("prices") or [])
            if p.get("min_qty") and p.get("unit_price") is not None
        ]
        breakdown.append({
            "label": "授权" if d.get("authorized") else "",
            "warehouse": d.get("distributor"),
            "quantity": d.get("stock_qty"),
            "ship_text": d.get("ship_text"),
            "moq": d.get("moq"),
            "vendor_sku": d.get("distributor_sku"),
            # Per-distributor tier prices — each distributor's tier structure is
            # independent; the warehouse-exploded batch_index downstream should
            # use this field rather than the cell-level top-level prices[].
            "prices": per_row_prices,
            # Unified cross-source packaging field at the WAREHOUSE-ROW level
            # (bom2buy's `package_info` is per distributor — e.g. "Tray" / "Tape & Reel"
            # / "Cut Tape" — extracted from the `.td-stock` cell text tail).
            # Cell-level `packaging_option` is left empty for bom2buy on purpose;
            # the per-row value is what downstream warehouse-exploded exports read.
            "packaging_option": d.get("package_info") or "",
        })
    # Derive package from a heuristic in category text
    pkg = None
    cat = v.get("category") or ""
    if m := re.search(r"(SOT-?\d+[A-Z]?-?\d*|TSSOP-?\d+|LQFP-?\d+|SOIC-?\d+|QFN-?\d+|TQFP-?\d+|DFN-?\d+|MSOP-?\d+|VSON-?\d+|BGA-?\d+|DIP-?\d+|SO[NTP]-?\d+)", cat):
        pkg = m.group(1)
    return {
        "manufacturer_part_number": v["variant_mpn"],
        "manufacturer": mfr,
        "stock_now_qty": stock_now if stock_now else 0,
        "stock_now_ship_text": None,
        "stock_future_qty": None,
        "stock_future_ship_text": None,
        "stock_breakdown": breakdown,
        "prices": prices,
        "datasheet_url": v.get("datasheet_url"),
        "package": pkg,
        "lifecycle_status": v.get("lifecycle_status"),
        "min_order_qty": min_order_qty,
        # bom2buy exposes shipping form per-distributor (`.td-stock` text tail like
        # "76,624 Tray"), NOT at the chip level. Cell-level `packaging_option` is
        # intentionally empty; the warehouse-exploded batch_index reads
        # `stock_breakdown[i].packaging_option` instead.
        "packaging_option": "",
        # Site-native preservation
        "site_distributors": v.get("distributors") or [],
        "site_category": v.get("category"),
        "site_description": v.get("description"),
        "site_search_hotness": v.get("search_hotness"),
    }


def _pick_variant(variants: list[dict], input_mpn: str) -> Optional[dict]:
    """Pick the variant whose MPN most closely matches the input."""
    if not variants:
        return None
    target = re.sub(r"[^A-Za-z0-9]", "", input_mpn).upper()
    # Exact alphanumeric match wins
    for v in variants:
        cand = re.sub(r"[^A-Za-z0-9]", "", v["variant_mpn"]).upper()
        if cand == target:
            return v
    # Fuzzy substring containment
    for v in variants:
        cand = re.sub(r"[^A-Za-z0-9]", "", v["variant_mpn"]).upper()
        if target in cand or cand in target:
            return v
    return None


# ─────────────────────────── Single-MPN scrape ───────────────────────────

def _wait_for_results(page: Page, timeout_sec: int = 15) -> bool:
    """Poll the page until either the distributor table OR the no-result marker appears."""
    for _ in range(timeout_sec * 2):
        try:
            txt = page.evaluate("() => document.body.innerText")
        except Exception:
            return False
        if "加入BOM" in txt or "没有找到" in txt:
            return True
        time.sleep(0.5)
    return False


def scrape_one(page: Page, input_mpn: str, out_dir: Path, expected_mfr: Optional[str] = None) -> dict:
    """Scrape one MPN. Caller is responsible for browser lifecycle.

    Returns the full record (status, attempts, extracted, etc.) and writes
    artifacts into out_dir/Test_<safe_mpn>_BOM2BUY_<ts>/ (so callers can pass
    a batch root and we'll create the per-cell subfolder ourselves).

    For batch invocation, pass out_dir = the parent BatchTest folder; we'll
    create Test_<safe_mpn>_BOM2BUY/ inside it (no timestamp) following the
    batch_scraper_test.py convention.
    """
    safe_mpn = _safe(input_mpn)
    started = datetime.now()
    url = SEARCH_URL.format(mpn=quote(input_mpn))

    attempts = []
    record = {
        "method": "playwright_opera",
        "url_search": url,
        "input_mpn": input_mpn,
        "expected_mfr": expected_mfr,
        "started_at": started.isoformat(timespec="seconds"),
        "status": "scraping",
        "attempts": attempts,
        "extracted": None,
        "error": None,
    }

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PWTimeout as e:
        attempts.append({"step": "navigate", "url": url, "outcome": "timeout", "error": str(e)})
        record.update(status="timeout", error=str(e))
        return record
    except Exception as e:
        attempts.append({"step": "navigate", "url": url, "outcome": "exception", "error": str(e)})
        record.update(status="exception", error=str(e))
        return record

    if _is_captcha_page(page):
        raise CaptchaRequired("Captcha gate hit mid-batch — session expired.")

    got_results = _wait_for_results(page)
    attempts.append({"step": "wait_results", "outcome": "ok" if got_results else "timeout"})

    title = page.title()
    html = page.content()
    text_render = page.evaluate("() => document.body.innerText")
    # Save artifacts
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{safe_mpn}_product.html").write_text(html, encoding="utf-8")
    (out_dir / f"{safe_mpn}_product.txt").write_text(text_render, encoding="utf-8")
    # Dismiss the Usercentrics cookie banner before screenshotting so the user's
    # eyeball-verification PNG isn't visually occluded. The banner is purely
    # cosmetic — DOM extraction was unaffected — but the screenshot is what the
    # human reviews.
    try:
        page.evaluate("""() => {
            // Usercentrics CMP: actual visible banner is <aside id="usercentrics-cmp-ui">,
            // NOT the loader <script id="usercentrics-cmp">. Hide the right node.
            const ids = ['usercentrics-cmp-ui', 'usercentrics-root', 'uc-cookie-banner',
                         'uc-cross-domain-consent-sharing-bridge'];
            for (const id of ids) {
                const el = document.getElementById(id);
                if (el) {
                    el.style.setProperty('display', 'none', 'important');
                    el.style.setProperty('visibility', 'hidden', 'important');
                    el.remove();  // remove from DOM entirely to free layout space
                }
            }
            // Defensive sweep: hide anything with usercentrics/uc- in id/class
            document.querySelectorAll('[id^="usercentrics-"], [id^="uc-"], [class*="usercentrics"]').forEach(el => {
                if (el.tagName !== 'SCRIPT' && el.tagName !== 'STYLE') {
                    el.style.setProperty('display', 'none', 'important');
                }
            });
            // Defensive: any fixed-position overlay with high z-index containing consent keywords
            document.querySelectorAll('div, aside, dialog, section').forEach(el => {
                const cs = window.getComputedStyle(el);
                if ((cs.position === 'fixed' || cs.position === 'sticky') &&
                    cs.zIndex && parseInt(cs.zIndex) > 1000) {
                    const txt = (el.innerText || '').toLowerCase();
                    if (txt.includes('cookie') || txt.includes('consent') || txt.includes('privacy') ||
                        txt.includes('同意') || txt.includes('隐私')) {
                        el.style.setProperty('display', 'none', 'important');
                    }
                }
            });
        }""")
        time.sleep(0.5)  # let the layout reflow
    except Exception as e:
        attempts.append({"step": "dismiss_cookie_banner", "outcome": "warn", "error": str(e)})
    try:
        page.screenshot(path=str(out_dir / f"{safe_mpn}_product.png"), full_page=True)
    except Exception as e:
        attempts.append({"step": "screenshot", "outcome": "fail", "error": str(e)})

    variants, page_meta = _extract_variants(html, input_mpn)
    record["site_title"] = title
    record["page_meta"] = page_meta

    if not variants:
        record["status"] = "no_results"
        record["extracted"] = None
        return record

    # Canonical for each variant
    canonical_variants = [_canonical_from_variant(v, input_mpn) for v in variants]
    record["variants"] = canonical_variants

    # Multi-variant emission — write per-variant JSON + summary
    multi_variant = len(canonical_variants) > 1
    chosen_idx = 0
    chosen = _pick_variant(variants, input_mpn)
    if chosen is not None:
        # find its index
        for i, v in enumerate(variants):
            if v["variant_mpn"] == chosen["variant_mpn"]:
                chosen_idx = i
                break
    canonical_chosen = canonical_variants[chosen_idx]
    record["extracted"] = canonical_chosen
    record["status"] = "ok"

    # Per-variant subfolders for multi-variant cells (LCSC v3 / Future pattern)
    if multi_variant:
        for i, cv in enumerate(canonical_variants):
            safe_variant = _safe(cv["manufacturer_part_number"])
            sub = out_dir / safe_variant
            sub.mkdir(parents=True, exist_ok=True)
            sub_record = {
                **record,
                "extracted": cv,
                "variant_index": i,
                "channel": CHANNEL,
                "source": "bom2buy.com",
                "data_quality": "high" if cv["stock_breakdown"] else "medium",
                "paywall": "none",
                "scraped_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }
            sub_record.pop("variants", None)
            (sub / f"{safe_variant}.json").write_text(
                json.dumps(sub_record, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                write_summary(sub_record, sub, safe_variant)
            except Exception as e:
                attempts.append({"step": "write_summary", "variant": safe_variant, "error": str(e)})

    return record


def write_outputs(record: dict, out_dir: Path, input_mpn: str) -> None:
    safe_mpn = _safe(input_mpn)
    # Add canonical metadata fields the summary renderer expects
    record.setdefault("channel", CHANNEL)
    record.setdefault("source", "bom2buy.com")
    record.setdefault("scraped_at_utc", datetime.utcnow().isoformat(timespec="seconds") + "Z")
    ex = record.get("extracted") or {}
    record.setdefault("data_quality",
        "high" if ex.get("stock_breakdown") else ("none" if record.get("status") != "ok" else "medium"))
    record.setdefault("paywall", "none")

    (out_dir / f"{safe_mpn}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        write_summary(record, out_dir, safe_mpn)
    except Exception as e:
        print(f"[warn] write_summary failed: {e}", file=sys.stderr)


# ─────────────────────────── Browser lifecycle ───────────────────────────

def _check_opera_processes() -> int:
    """Return the number of running opera.exe processes (caller should ensure 0 before launch)."""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq opera.exe"], capture_output=True,
                             text=True, timeout=10, shell=True)
        return sum(1 for line in (out.stdout or "").splitlines() if line.lower().startswith("opera.exe"))
    except Exception:
        return -1  # unknown


def launch_opera(headless: bool = False) -> tuple[object, BrowserContext]:
    if not Path(OPERA_EXE).exists():
        raise FileNotFoundError(f"Opera binary not found: {OPERA_EXE} (set OPERA_EXE env var)")
    if not Path(OPERA_USER_DATA).exists():
        raise FileNotFoundError(f"Opera user data dir not found: {OPERA_USER_DATA} (set OPERA_USER_DATA env var)")
    n = _check_opera_processes()
    if n > 0:
        raise RuntimeError(
            f"{n} opera.exe processes are running. Close Opera fully (check Task Manager) before running this script."
        )
    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=OPERA_USER_DATA,
        executable_path=OPERA_EXE,
        headless=headless,
        viewport={"width": 1280, "height": 800},
        args=["--disable-blink-features=AutomationControlled"],
    )
    return p, ctx


# ─────────────────────────── CLI / batch driver ───────────────────────────

def _parse_mpns_arg(s: str) -> list[tuple[str, Optional[str]]]:
    """Parse semicolon-separated tokens. Each token is 'MPN' OR 'MPN<TAB>MFR'.

    NB: tab is the MPN/MFR separator because some real MPNs contain `:` (e.g.
    typo'd NXP/Nexperia variants like `BTA206X-800CT:127`). Earlier implementations
    used `:` as the separator and chopped these MPNs.
    """
    out = []
    for tok in (s or "").split(";"):
        tok = tok.strip()
        if not tok or tok.startswith("#"):
            continue
        if "\t" in tok:
            mpn, mfr = tok.split("\t", 1)
            out.append((mpn.strip(), mfr.strip() or None))
        else:
            out.append((tok, None))
    return out


def _read_mpns_file(path: Path) -> list[tuple[str, Optional[str]]]:
    """Read one MPN per line. Format: `MPN` or `MPN<TAB>MFR`. Lines starting with
    `#` are ignored.

    NB: tab is the separator (not `:`) — some MPNs contain `:` (typos like
    `BTA206X-800CT:127`).
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip("\n").rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            mpn, mfr = line.split("\t", 1)
            out.append((mpn.strip(), mfr.strip() or None))
        else:
            out.append((line.strip(), None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mpn", nargs="?", help="Single MPN to scrape (positional). For batch, use --mpns / --mpns-file.")
    ap.add_argument("out_dir", nargs="?", help="Output dir for single-MPN mode (default: auto-timestamped under test/scraper/)")
    ap.add_argument("--mpns", help='Semicolon-separated batch: "MPN1:MFR1;MPN2:MFR2;..."')
    ap.add_argument("--mpns-file", help="Text file with one MPN per line (or MPN:MFR)")
    ap.add_argument("--out", help="Batch output root (default: test/scraper/BatchTest_<ts>_bom2buy/)")
    ap.add_argument("--headless", action="store_true", help="Run Opera headless (default: visible window)")
    ap.add_argument("--per-mpn-delay", type=float, default=PER_MPN_DELAY_SEC)
    ap.add_argument("--long-delay-every", type=int, default=LONG_DELAY_EVERY_N)
    ap.add_argument("--long-delay-sec", type=float, default=LONG_DELAY_SEC)
    args = ap.parse_args()

    # Build target list
    targets: list[tuple[str, Optional[str]]] = []
    batch_mode = bool(args.mpns or args.mpns_file)
    if batch_mode:
        if args.mpns:
            targets.extend(_parse_mpns_arg(args.mpns))
        if args.mpns_file:
            targets.extend(_read_mpns_file(Path(args.mpns_file)))
    elif args.mpn:
        targets = [(args.mpn, None)]
    else:
        ap.print_help(); sys.exit(2)

    if not targets:
        print("No MPNs given.", file=sys.stderr); sys.exit(2)

    # Output root
    ts = datetime.now().strftime("%Y%m%d_%H_%M_%S")
    if batch_mode:
        root = Path(args.out) if args.out else (TEST_ROOT / f"BatchTest_{ts}_bom2buy")
        root.mkdir(parents=True, exist_ok=True)
        # Write input snapshot — ONLY if the folder is empty / doesn't already have a
        # batch_input.csv. Otherwise we'd clobber an existing batch's master input list.
        bi_path = root / "batch_input.csv"
        if not bi_path.exists():
            bi_path.write_text(
                "input_mpn,expected_mfr\n" + "\n".join(f'"{m}","{f or ""}"' for m, f in targets) + "\n",
                encoding="utf-8")
        else:
            print(f"[bom2buy] batch_input.csv already exists at {bi_path} — leaving untouched")
    else:
        root = make_run_dir(args.mpn)

    print(f"[bom2buy] target_count={len(targets)}  root={root.relative_to(PROJECT_ROOT)}")

    p = ctx = None
    overall_status = 0
    summary_rows: list[dict] = []
    try:
        try:
            p, ctx = launch_opera(headless=args.headless)
        except RuntimeError as e:
            print(f"[bom2buy] launch failed: {e}", file=sys.stderr)
            sys.exit(2)
        except FileNotFoundError as e:
            print(f"[bom2buy] config error: {e}", file=sys.stderr)
            sys.exit(2)
        page = ctx.new_page()

        # Captcha session check (one-time, costs 1 page load)
        try:
            verify_session(page)
        except CaptchaRequired as e:
            print(f"[bom2buy] {e}", file=sys.stderr)
            (root / "_SESSION_EXPIRED.txt").write_text(str(e), encoding="utf-8")
            sys.exit(3)

        print("[bom2buy] session OK; starting scrapes…")

        for i, (mpn, mfr) in enumerate(targets, 1):
            if batch_mode:
                cell_dir = root / f"Test_{_safe(mpn)}_{CHANNEL}"
            else:
                cell_dir = root
            cell_dir.mkdir(parents=True, exist_ok=True)
            t0 = time.time()
            try:
                rec = scrape_one(page, mpn, cell_dir, expected_mfr=mfr)
            except CaptchaRequired as e:
                # Mid-batch session expiry — abort the rest cleanly
                print(f"[bom2buy] CAPTCHA mid-batch at i={i} ({mpn}): {e}", file=sys.stderr)
                (root / "_SESSION_EXPIRED.txt").write_text(str(e), encoding="utf-8")
                overall_status = 3
                break
            except Exception as e:
                tb = traceback.format_exc()
                rec = {"status": "exception", "error": str(e), "traceback": tb, "input_mpn": mpn,
                       "expected_mfr": mfr, "method": "playwright_opera", "attempts": []}
            elapsed = time.time() - t0
            rec["elapsed_sec"] = round(elapsed, 2)
            write_outputs(rec, cell_dir, mpn)
            ex = rec.get("extracted") or {}
            dist_count = len(ex.get("stock_breakdown") or [])
            print(f"[bom2buy] {i:>3}/{len(targets)}  {mpn:<25}  status={rec.get('status'):12}  "
                  f"distributors={dist_count}  stock={ex.get('stock_now_qty')}  "
                  f"prices={len(ex.get('prices') or [])}  elapsed={elapsed:.1f}s")
            summary_rows.append({
                "input_mpn": mpn, "expected_mfr": mfr, "status": rec.get("status"),
                "returned_mpn": ex.get("manufacturer_part_number"),
                "returned_mfr": ex.get("manufacturer"),
                "stock_now_qty": ex.get("stock_now_qty"),
                "distributors": dist_count,
                "num_price_tiers": len(ex.get("prices") or []),
                "datasheet_url": ex.get("datasheet_url"),
                "lifecycle_status": ex.get("lifecycle_status"),
                "min_order_qty": ex.get("min_order_qty"),
                "elapsed_sec": rec.get("elapsed_sec"),
                "error": (rec.get("error") or "")[:200],
            })
            # Rate limiting between MPNs
            if i < len(targets):
                if i % args.long_delay_every == 0:
                    print(f"[bom2buy] long delay {args.long_delay_sec}s (every {args.long_delay_every} cells)")
                    time.sleep(args.long_delay_sec)
                else:
                    time.sleep(args.per_mpn_delay)
    finally:
        try:
            if ctx is not None:
                ctx.close()
            if p is not None:
                p.stop()
        except Exception:
            pass

    # Write a batch_index.csv / batch_summary.md for batch mode — but ONLY if the
    # target folder doesn't already have a v3-schema batch_index.csv (i.e. we're
    # merging into an existing multi-source batch). When merging, the orchestrator
    # downstream (e.g. _merge_bom2buy_into_batch.py) is responsible for the
    # aggregate files; we'd clobber its 26-column schema with our 13-column
    # bom2buy-only one. Always emit a bom2buy-only sidecar for traceability.
    if batch_mode and summary_rows:
        import csv
        main_idx = root / "batch_index.csv"
        sidecar = root / "bom2buy_only_batch_index.csv"
        if main_idx.exists():
            # Inspect the first line — if it's the v3 26-column schema we don't
            # touch it. If it's an old bom2buy-only file we may safely overwrite.
            header = main_idx.read_text(encoding="utf-8-sig").splitlines()[:1]
            v3_marker = "input_mpn,expected_mfr,source,status"
            if header and header[0].startswith(v3_marker):
                # Multi-source batch present — write sidecar instead
                target = sidecar
                print(f"[bom2buy] main batch_index.csv has v3 schema — writing bom2buy-only sidecar to {target.name}")
            else:
                target = main_idx
        else:
            target = main_idx
        with open(target, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            w.writerows(summary_rows)
        # Same safety on batch_summary.md
        summary_path = root / "batch_summary.md"
        if summary_path.exists() and target.name == sidecar.name:
            summary_path = root / "bom2buy_only_summary.md"
        summary_path.write_text(_render_batch_summary(summary_rows, root), encoding="utf-8")
        print(f"[bom2buy] wrote {target.name} + {summary_path.name} ({len(summary_rows)} rows)")

    sys.exit(overall_status)


def _render_batch_summary(rows: list[dict], root: Path) -> str:
    ok = sum(1 for r in rows if r["status"] == "ok")
    nr = sum(1 for r in rows if r["status"] == "no_results")
    other = len(rows) - ok - nr
    lines = [
        f"# bom2buy batch summary",
        f"",
        f"- Root: `{root.relative_to(PROJECT_ROOT)}`",
        f"- Total cells: {len(rows)}",
        f"- ok: {ok} ({ok/len(rows)*100:.1f} %)" if rows else "",
        f"- no_results: {nr} ({nr/len(rows)*100:.1f} %)" if rows else "",
        f"- other: {other}",
        f"",
        f"## Per-cell",
        f"",
        f"| input_mpn | status | returned_mpn | mfr | stock | distributors | price_tiers | MOQ | lifecycle | elapsed_s |",
        f"|---|---|---|---|---:|---:|---:|---:|---|---:|",
    ]
    for r in rows:
        lines.append(
            "| {input_mpn} | {status} | {returned_mpn} | {returned_mfr} | {stock_now_qty} | {distributors} | "
            "{num_price_tiers} | {min_order_qty} | {lifecycle_status} | {elapsed_sec} |".format(
                input_mpn=r.get("input_mpn") or "",
                status=r.get("status") or "",
                returned_mpn=(r.get("returned_mpn") or "")[:30],
                returned_mfr=(r.get("returned_mfr") or "")[:25],
                stock_now_qty=r.get("stock_now_qty") if r.get("stock_now_qty") is not None else "",
                distributors=r.get("distributors") or "",
                num_price_tiers=r.get("num_price_tiers") or "",
                min_order_qty=r.get("min_order_qty") or "",
                lifecycle_status=r.get("lifecycle_status") or "",
                elapsed_sec=r.get("elapsed_sec") or "",
            )
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
