# Scraper batch output schema

> **v2.2 schema as of 2026-05-27** — warehouse-exploded, 29 columns. The first 25 columns (incl. `packaging_option`) mirror `api/doc/batch_output_schema.md` so the two tracks' `batch_index.csv` files share a column reference and can be `UNION ALL`'d. Scraper-only extras: `elapsed_sec`, `num_variants`, `llm_mfr_verdict`, `llm_mfr_reason`. See [Version history](#version-history) for the v1 → v2.2 evolution.

This is the data contract for `batch_scraper_test.py` outputs. Any downstream tool (or future Claude session) parsing files under `test/scraper/BatchTest_<YYYYMMDD>_<HH_MM_SS>/` should rely on the column names and value rules below, not on column order.

Driver: `scraper/scripts/batch_scraper_test.py`. Sources: `LCSC`, `DIGIKEY`, `HQEW`, `FUTURE`, `RSONLINE`, `ONEYAC`, `ICKEY`, `ROCHESTER` (extendable via `CHANNELS` / `CHANNEL_ORDER` in the driver).

## Vocabulary

- **source** — the vendor / distributor (`LCSC` / `DIGIKEY` / `HQEW` / `FUTURE` / `RSONLINE` / `ONEYAC` / `ICKEY` / `ROCHESTER`). Same concept as `source` on the API track; called `channel` in the v1 schema and in the driver's internal `CHANNELS` dict, which is the same enum.
- **warehouse** — one row in the chosen variant's `extracted.stock_breakdown[]`. The human-readable name lives in `stock_breakdown[].warehouse`. Examples: `"Future Electronics (global)"`, `"Future Electronics (Singapore)"`, `"On Order"`, `"ICKEY 转售 (Digi-Key)"`. Some channels (LCSC, RSONLINE after the recent fix) emit rows with `warehouse=None` — the page itself does not name a warehouse, and the CSV cell is left empty.
- **status** — *our* normalized outcome of the scrape, not an HTTP code. Values: `ok` / `no_results` / `blocked` / `timeout` / `exit_<N>` / `exception` / `failed`. The literal HTTP / Playwright outcome of any single attempt lives in the per-MPN JSON's `attempts[]` log.

The `status` enum on the scraper track is **source-native** (`blocked` for Akamai BMP / Cloudflare gates; `timeout` for hung browsers) and intentionally diverges from the API track's enum (which has `auth_failed` / `http_error` / `missing_credentials`). Cross-track joins should pivot on `(input_mpn, source, status == 'ok')` rather than on the raw enum value.

## File manifest

A completed batch folder contains:

| File | Form | Rows per chip | Purpose |
|---|---|---|---|
| `batch_input.csv` | flat | 1 | Verbatim input from the xlsx |
| `batch_index.csv` / `.xlsx` | long | varies (≈ N_sources × 1–6 warehouses) | Per `(MPN × source × warehouse)` row — primary analysis table |
| `batch_index.json` | nested | N_sources | Machine-readable: full `record` + chosen `extracted_best` per `(MPN × source)` |
| `failures.md` | markdown | varies | Non-ok `(chip × source)` pairs grouped by source |
| `batch_summary.md` | markdown | — | TL;DR + per-source pass rate + chip-level highlights + manufacturer mismatches |
| `Test_<safe_mpn>_<SOURCE>/` | folder | — | Per-MPN-per-source run folder — one folder per `(chip, source)` pair |

`<safe_mpn>` = input MPN with non-`[A-Za-z0-9._-]` replaced by `_`. Examples: `PIC16F18446T-I/SS` → `PIC16F18446T-I_SS`; `BT168GW,115` → `BT168GW_115`.

For an 8-chip × 8-source mini-batch the (chip × source) cell count is 64. The warehouse-row count is typically 1.2–2× the cell count (Future contributes 3–4 warehouses per chip; HQEW per-distributor rows are 1–6; LCSC / RSONLINE / ONEYAC / ICKEY / Rochester are usually 0–1).

---

## `batch_index.csv` / `batch_index.xlsx` — long form (PRIMARY)

Granularity: **one row per `(input_mpn × source × warehouse)`**. When a chip is not carried by a given source (or the scrape failed), exactly **one row** is emitted for that `(chip, source)` with `status` set and the warehouse-level columns empty.

**Encoding:** CSV is UTF-8 with BOM (`utf-8-sig`). XLSX is openpyxl Workbook, sheet `batch_index`, header row bold + frozen at A2.

**Column order is fixed** (column index meaningful for parsers that need it):

| # | Column | Type | Nullable | Description |
|---:|---|---|---|---|
| 1 | `input_mpn` | str | no | MPN exactly as read from the xlsx (preserve original case, slashes, commas, spaces). Repeated across all warehouse rows for a given cell. |
| 2 | `expected_mfr` | str | no | Manufacturer as read from xlsx col 2. Repeated. May be Chinese or English. |
| 3 | `source` | enum | no | Bilingual source label written into the CSV cell — one of `LCSC_立创商城` / `DIGIKEY_得捷电子` / `HQEW_华强电子网` / `FUTURE_Future_Electronics` / `RSONLINE_RS欧时` / `ONEYAC_唯样商城` / `ICKEY_云汉芯城` / `ROCHESTER_Rochester_Electronics`. The short English prefix before the underscore matches the API track's `source` enum exactly (`LCSC`, `HQEW`, …) so a tolerant join can split on `_`. Internal code paths, folder names, and the `--only` CLI flag still use the short enum only. |
| 4 | `status` | enum | no | Our normalized outcome — see [Status values](#status-values). |
| 5 | `returned_mpn` | str | yes | MPN of the variant chosen by `pick_best_extracted`. Picked by exact case-insensitive match against `input_mpn`; if none match, by highest `stock_now_qty`. Empty when `status != ok`. |
| 6 | `vendor_sku` | str | yes | Vendor SKU of the chosen variant. LCSC `lcsc_part_number` (e.g. `C256448`); Digikey `digikey_part_number`; RSONLINE `rs_stock_no`; ICKEY/ONEYAC product IDs; HQEW/Future/Rochester empty. |
| 7 | `returned_mfr` | str | yes | Manufacturer string returned by the source. May differ from `expected_mfr` (full names like "STMicroelectronics" vs. shorthand "STM"; PRC marketplaces relisting STM parts as UMW; clone brands). |
| 8 | `mfr_match` | bool | no | `True` iff one of `expected_mfr` / `returned_mfr` is a substring of the other after `[^A-Z0-9]` strip + uppercase. Always `False` when either side is empty. |
| 9 | `warehouse` | str | yes | The warehouse's human-readable name — `stock_breakdown[].warehouse`. Empty when the source itself does not name a warehouse on the page (LCSC's single 现货 aggregate, RSONLINE rows) and for failed `(chip, source)` rows. |
| 10 | `warehouse_idx` | int | yes | 1-based position of this warehouse within the chip×source group. Useful for reconstructing per-MPN ordering after CSV sort. Empty on failed / no-warehouse rows. |
| 11 | `ships_from` | str | yes | Country of origin of the pool. Not populated by the current scrapers (reserved for parity with the API track's Arrow rows). Empty in v2. |
| 12 | `stockpool_qty` | int | yes | Quantity in *this* warehouse — `stock_breakdown[].quantity`. `null` for unbounded / unknown qty rows (RSONLINE 期货 rows with only a date; "Factory Stock" rows without a published count). Distinguish from `0` (out of stock) — check the matching `ship_text`. |
| 13 | `ship_text` | str | yes | Canonical 发货时间 string for this warehouse — `stock_breakdown[].ship_text`. e.g. `"最快4小时发货"`, `"Ships immediately"`, `"15 件将从其他地点发货"`, `"另外 100 件将于 2026年5月25日 发货"`, `"内地成团后 10-15工作日"`. Channel-native wording is preserved verbatim. |
| 14 | `lead_time_days` | int | yes | Lead time **in days** parsed from `ship_text`. Recognised patterns: `Factory Lead Time: N Weeks/Days`, `原厂(标准)?交货期 N 周/天/日`, `lead N 天`, `N 天数`, `N 工作日`, `N Weeks`. Empty when no pattern matches. |
| 15 | `moq` | int | yes | Minimum order quantity for *this* warehouse if the source exposes per-pool MOQ (ICKEY breakdown rows carry `moq`); otherwise falls back to `extracted.min_order_qty` / `extracted.min_buy_number`. |
| 16 | `min_break_qty` | int | yes | Smallest qty break in the chosen variant's price-tier table. Repeated across the cell's warehouse rows. |
| 17 | `price_at_min_qty` | float | yes | Unit price at quantity break = 1 if a qty=1 tier exists, otherwise at `min_break_qty`. |
| 18 | `max_break_qty` | int | yes | Largest qty break (volume-tier boundary). |
| 19 | `price_at_max_qty` | float | yes | Unit price at `max_break_qty` (typically the cheapest tier). |
| 20 | `num_price_tiers` | int | no | Total tier count for the cell's price list (0 when no prices). |
| 21 | `currency` | str | yes | Currency for the price tiers. LCSC implicit `CNY` (set explicitly when `unit_price_cny` is populated). RSONLINE / ICKEY / ONEYAC: `CNY`. Future: `SGD` (APAC site). Digikey: empty (Digikey tier dicts omit a currency code — treat as USD on `.com`, CNY on `.cn`). HQEW: empty (云价格 is CNY by convention). |
| 22 | `packaging_option` | str | yes | Cross-source canonical shipping / break form (`Tape & Reel` / `Cut Tape` / `Tray` / `Tube` / `Reel` / `编带` / `管装` / `散料` / `Bulk` / `Ammo Pack` / `Each` / etc.). **Original site wording, never translated.** Populated by LCSC / Digikey / Future / Rochester at the cell level; populated PER-WAREHOUSE-ROW by bom2buy (each distributor ships in a different form). Empty for HQEW / ONEYAC / ICKEY / RSONLINE (source does not publish a shipping form). Position mirrors the API track. |
| 23 | `datasheet_url` | str | yes | Direct PDF URL when the source exposes one. May be relative for HQEW/LCSC. Repeated across warehouse rows. |
| 24 | `run_subdir` | str | no | Forward-slash relative path from PROJECT_ROOT to the per-MPN-per-source folder. Use to load `<safe_mpn>.json` for full detail. |
| 25 | `error` | str | yes | Truncated error message (≤300 chars). For `blocked`: blocker name (e.g. `cloudflare_just_a_moment`). For `exception`: `TypeName: message`. Empty on ok / no_results. |
| 26 | `llm_mfr_verdict` | str | yes | Deepseek-v4-pro verdict on the `(expected_mfr, returned_mfr)` pair when `mfr_match=False` AND `status=ok`. One of `YES` (legitimate equivalence — acquisitions / sub-brands / abbreviations / language variants), `NO` (real mismatch), `WEAK_YES` (model used speculative wording like "likely" / "probably"), `UNCERTAIN`. Empty when `mfr_match=True`, `status!=ok`, or LLM step was skipped. Added by `common/_llm_mfr_normalize.py` after the bom2buy merge. |
| 27 | `llm_mfr_reason` | str | yes | Short free-text justification from the LLM (≤80 chars). Empty when `llm_mfr_verdict` is empty. |
| 28 | `elapsed_sec` | float | yes | Wallclock seconds for the subprocess call. Scraper-only extra (no API counterpart). Repeated across warehouse rows. Empty when row was reloaded via `--resume`. |
| 29 | `num_variants` | int | no | Number of MPN variants the source returned (≥1 when ok; 0 on `no_results`). For LCSC/Future this counts variant subfolders; for HQEW it counts MPN variants inside `extracted.variants`; for single-listing sources it is 1 when ok. Scraper-only extra. |

### Status values

| Value | Meaning | When |
|---|---|---|
| `ok` | Scrape succeeded, `extracted` populated, at least one variant returned. | Happy path. |
| `no_results` | Source ran fine but returned no matching products. | LCSC `no_matches`; HQEW `no_listings`; Future no `product__list--code` anchors; RSONLINE/ROCHESTER `no_results` after the recent exact-match guard. |
| `blocked` | Bot/Cloudflare/Akamai interstitial did not clear within the poll budget. | Mostly Digikey Cloudflare `_abck` revocation; rerun usually clears. |
| `exception` | Scraper raised an unhandled exception. | Inspect `error` + `run_subdir/_*.html` for forensics. |
| `timeout` | Subprocess exceeded the source's hard wallclock budget. | Per-source timeouts: see `CHANNELS` in the driver (LCSC 240s, Digikey 180s, HQEW 90s, Future 300s, RSONLINE 90s, ONEYAC 120s, ICKEY 150s, Rochester 180s). |
| `exit_<N>` | Subprocess exited with non-zero code and no usable record on disk. | Rare; usually a missing dependency or import error. Check `batch_index.json` for `stderr_tail`. |
| `failed` | Source-specific "method = failed". | E.g. Digikey's search returned no `/products/detail/` link. Treat as "not stocked by this source", not a scraper bug. |

### Type coercion notes (when reading the CSV)

The CSV is loose-typed — every cell is a string. Apply:

- `mfr_match`: parse as `bool('True'/'False')`. Empty → `False`.
- `warehouse_idx`, `stockpool_qty`, `lead_time_days`, `moq`, `min_break_qty`, `max_break_qty`, `num_price_tiers`, `num_variants`: empty → `None` (or `0` for `num_*`); else `int()`. **`stockpool_qty` empty distinguishes "unbounded / unknown" from `0` "out of stock"** — check the matching `ship_text` cell to disambiguate.
- `price_at_min_qty`, `price_at_max_qty`, `elapsed_sec`: empty → `None`; else `float()`.

Reading the XLSX with `openpyxl` returns the underlying Python types directly — no coercion needed.

### Currency caveat (do not skip)

`batch_index` is **multi-currency on a single row basis**. Future's APAC site quotes `SGD`; PRC sites quote `CNY` / `RMB`; Digikey leaves it empty (treat as `USD` on `.com`, `CNY` on `.cn`). Cross-source price math must reconcile units; **no FX-rate column** is provided.

Lead-time units are normalized to **days** in `lead_time_days`. The free-text `ship_text` may still carry the source-native unit (`Weeks`, `天`, `工作日`).

### Aggregation hint

Naive `SUM(stockpool_qty) GROUP BY input_mpn, source` will **double-count** any source whose `stock_breakdown` carries a region-mirror row of its global total — Future is the prominent example (`"Future Electronics (global)"` 141,000 + `"Future Electronics (Singapore)"` 141,000 reports the *same* physical stock viewed two ways). Either:

- Take the chip-level scalar from `batch_index.json[i].extracted_best.stock_now_qty` instead of summing CSV rows.
- Or filter `WHERE warehouse_idx = 1` (the first / Global row) before summing.

---

## `batch_index.json` — machine-readable per-`(MPN × source)` records

Array of objects, one per `(MPN × source)` cell (NOT per warehouse — for warehouse granularity, use `batch_index.csv`). Each object:

```json
{
    "source": "FUTURE",
    "input_mpn": "BTA12-800BWRG",
    "expected_mfr": "STM",
    "elapsed_sec": 66.3,
    "subprocess_status": "ok",
    "stdout_tail": "... last 2000 chars of subprocess stdout ...",
    "stderr_tail": "... last 2000 chars of subprocess stderr ...",
    "record": { /* full parent record as written by the scraper */ },
    "extracted_best": { /* chosen best-variant `extracted` dict */ },
    "error": null
}
```

- `record` mirrors the per-MPN-per-source folder's `<safe_mpn>.json`: `query`, `channel` (internal field on disk), `scraped_at_utc`, `source`, `search_url`, `attempts[]`, `data_quality`, `status`, `variants[]` (for multi-variant sources) or `extracted` (for single-record sources).
- `extracted_best` is the chosen variant's normalized `extracted` dict: includes `stock_breakdown[]` (the array `batch_index.csv` is exploded from), plus `site_*` source-native fields, `prices`, `parameters`, top-level scalars `stock_now_qty` / `stock_future_qty` / `stock_now_ship_text` / `stock_future_ship_text`.
- For `--resume` rows, `subprocess_status` is `resume_skipped` and `stdout_tail`/`stderr_tail` are absent.

Use this when CSV explosion isn't enough — e.g. to access the full `parameters` list, the per-variant Playwright `attempts[]`, or the chip-level `stock_now_qty` aggregate (deduped across the warehouse rows above).

---

## Per-MPN-per-source folder (`Test_<safe_mpn>_<SOURCE>/`)

Contents depend on the source, but every folder always has the parent JSON:

```
Test_<safe_mpn>_<SOURCE>/
├── <safe_mpn>.json            ← canonical record (parent for multi-variant sources)
├── <safe_mpn>_summary.md      ← human-readable, rendered by common/_summary.py
└── ...                        ← per-scraper artefacts (HTML, screenshots, raw __NEXT_DATA__)
```

For multi-variant sources (LCSC, Future), one additional subfolder per returned MPN variant:

```
Test_<safe_mpn>_LCSC/
├── STM32G030F6P6.json         ← parent: `variants: [...]` + aggregates
├── parent_summary.md
├── _search.html / _search.png
└── STM32G030F6P6TR/           ← variant subfolder
    ├── STM32G030F6P6TR.json   ← variant record with full `extracted`
    ├── STM32G030F6P6TR_summary.md
    ├── STM32G030F6P6TR_raw_next_data.json
    ├── STM32G030F6P6TR_product.html
    └── STM32G030F6P6TR_product.png
```

HQEW, Digikey, RSONLINE, ONEYAC, ICKEY, Rochester are single-record sources — no variant subfolders. HQEW's `extracted.variants` lists MPN variants inline (different MPN strings the search returned), but each variant's listings live inside the same JSON, not in subfolders.

Per-variant grouping rule (never aggregate across MPN strings): see `MEMORY.md` / `feedback_mpn_variant_grouping.md`.

---

## Canonical `extracted` field reference

When loading a parent or variant JSON via `record["extracted"]` (or `batch_index.json[i]["extracted_best"]`), expect these keys on every source (`null`/`0`/`[]` when N/A):

- `manufacturer_part_number` — exact MPN of THIS variant (may differ from input MPN).
- `manufacturer` — source's manufacturer string.
- Source-specific SKU keys: `lcsc_part_number`, `digikey_part_number`, `rs_stock_no`.
- `stock_now_qty`, `stock_now_ship_text` — chip-level canonical 现货 quantity + delivery SLA.
- `stock_future_qty`, `stock_future_ship_text` — chip-level canonical 期货 / 在途 / factory-order. `stock_future_qty` may be `null` for unbounded factory orders.
- `stock_breakdown` — **list of warehouse rows** (the data that `batch_index.csv` is exploded from). Each row: `{label, warehouse, quantity, ship_text, [moq], [ships_from], [note]}`.
- `prices` — list of `{min_qty, unit_price, unit_price_float, currency, [to_qty]}`. Top-level price tiers.
- `parameters` — list of `{name, value}` (key names follow the source's native wording).
- `datasheet_url`, `image_url`, `product_url`, `category_name_en` / `category_name_cn`, `lifecycle_status`, `package` — source-specific availability.

Source/site-native fields (per the "site-native wording" rule) are namespaced with `site_*`:

- LCSC: `stock_gd_warehouse`, `stock_js_warehouse`, `stock_smt`, `stock_transit`, `min_buy_number`, `min_whole_number`.
- Future: `site_global_stock`, `site_region_label`, `site_region_stock`, `site_on_order`, `site_factory_stock`, `site_factory_lead_time`.
- ONEYAC: `site_order_min`, `min_pack_qty`, `site_detail_inventory`, `site_lead_time`.
- RSONLINE: `site_stock_status`.

The canonical `stock_now_*` / `stock_future_*` scalars and the `stock_breakdown[]` rows are the cross-source interpretation layer.

---

## How to load the batch in Python

```python
import csv
from pathlib import Path

batch = Path("test/scraper/BatchTest_20260519_XX_XX_XX")

# Quick analysis: long form (one row per warehouse)
with open(batch / "batch_index.csv", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

# Per-source pass rate — dedupe by (input_mpn, source) since warehouse rows repeat per cell
print("Pass rate per source:")
seen: dict[tuple[str, str], dict] = {}
for r in rows:
    seen.setdefault((r["input_mpn"], r["source"]), r)
for src in ("LCSC", "DIGIKEY", "HQEW", "FUTURE", "RSONLINE", "ONEYAC", "ICKEY", "ROCHESTER"):
    cells = [r for r in seen.values() if r["source"] == src]
    ok = sum(1 for r in cells if r["status"] == "ok")
    print(f"  {src}: {ok}/{len(cells)}")

# Where can I buy BT168GW,115, in-stock, MOQ ≤ 100, sorted by price?
candidates = [
    r for r in rows
    if r["input_mpn"] == "BT168GW,115"
    and r["status"] == "ok"
    and r["stockpool_qty"] and int(r["stockpool_qty"]) > 0
    and (not r["moq"] or int(r["moq"]) <= 100)
]
candidates.sort(key=lambda r: float(r["price_at_min_qty"]) if r["price_at_min_qty"] else float("inf"))
for r in candidates[:10]:
    print(f"  {r['source']:9s}  {r['warehouse'] or '(no warehouse name)':40s}  "
          f"qty={r['stockpool_qty']:>7s}  @ {r['price_at_min_qty']:>7s} {r['currency']}  "
          f"moq={r['moq']}")

# Pull full record for one (MPN × source) cell
import json
mpn, src = "BTA12-800BWRG", "FUTURE"
safe = mpn.replace("/", "_").replace(",", "_")  # use the sanitisation rule
rec = json.loads((batch / f"Test_{safe}_{src}" / f"{safe}.json").read_text(encoding="utf-8"))
for v in rec.get("variants", []):
    ex = v.get("extracted") or {}
    print(v["status"], ex.get("manufacturer_part_number"), ex.get("stock_now_qty"))
```

For the JSON-rich path use `batch_index.json` directly; it carries the full `record` plus `extracted_best` per `(MPN × source)`, including the unexploded `stock_breakdown[]` and `site_*` fields.

---

## Cross-track joins with the API `batch_index.csv`

Columns 1–24 align with `api/doc/batch_output_schema.md`. To union the two tracks:

```python
import pandas as pd
scraper = pd.read_csv("test/scraper/BatchTest_<ts>/batch_index.csv", encoding="utf-8-sig")
api     = pd.read_csv("test/api/BatchTest_<ts>/batch_index.csv",     encoding="utf-8-sig")
# Drop scraper-only extras for a strict 24-col union; or pd.concat with sort=False to keep them
common  = pd.concat([scraper.drop(columns=["elapsed_sec", "num_variants"]), api],
                    ignore_index=True)
```

`status` enums differ between tracks (`blocked`/`timeout` on the scraper side; `auth_failed`/`http_error` on the API side). Filter on `status == 'ok'` for happy-path cross-track comparison.

---

## Version history

| Version | Date | Schema |
|---|---|---|
| v1 | 2026-05-17 | Per `(MPN × channel)`, 20 columns. Field names `channel`, `price_at_qty_1`, `lowest_unit_price`, `stock_now_qty`, `stock_future_qty`, `stock_future_ship_text`. Wide-form `batch_compare.csv` / `.xlsx` also emitted. |
| **v2** | **2026-05-19** | Warehouse-exploded per `(MPN × source × warehouse)`, 26 columns (24 API-aligned + 2 scraper extras `elapsed_sec` / `num_variants`). Renamed `channel` → `source`. New columns `vendor_sku`, `warehouse`, `warehouse_idx`, `ships_from`, `stockpool_qty`, `ship_text`, `lead_time_days`, `moq`, `max_break_qty`. Renamed `price_at_qty_1` → `price_at_min_qty`, `lowest_unit_price` → `price_at_max_qty`. **`batch_compare.csv` / `.xlsx` removed.** |
| **v2.1** | **2026-05-27** | +1 column: `packaging_option` (col 22, between `currency` and `datasheet_url`). Mirrors API track position. Original site wording preserved; never translated. Per-warehouse for bom2buy, cell-level for the other 4 working sources. |
| **v2.2** | **2026-05-27** | +2 columns: `llm_mfr_verdict` + `llm_mfr_reason` (cols 26-27, between `error` and `elapsed_sec`). Populated by `common/_llm_mfr_normalize.py` post-bom2buy-merge: Deepseek-v4-pro classifies each `mfr_match=False` row as legitimate equivalence (`YES` / `WEAK_YES`) or real mismatch (`NO`). Skipped silently if `deepseek_api_key` absent. **Total: 29 columns.** |

v1 batch folders on disk (e.g. `BatchTest_20260518_19_58_04/`) are not retroactively rewritten. Tools that consume both must handle the version by inspecting the CSV header.
