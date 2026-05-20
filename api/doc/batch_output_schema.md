# API batch output schema

This is the data contract for `batch_api_test.py` outputs. Any downstream tool (or future Claude session) parsing files under `test/api/BatchTest_<YYYYMMDD>_<HH_MM_SS>/` should rely on the column names and value rules below, not on column order.

Driver: `api/scripts/batch_api_test.py`. Sources: `MOUSER`, `DIGIKEY`, `ELEMENT14`, `ARROW` (extendable via `SOURCES_ALL` in the driver).

## Vocabulary

- **source** — the vendor (`MOUSER` / `DIGIKEY` / `ELEMENT14` / `ARROW`). Each source exposes its own native warehouse structure.
- **warehouse** — one row in the chosen variant's `extracted.stock_breakdown[]`. The human-readable name lives in `stock_breakdown[].warehouse` (e.g. `"Arrow / VERICAL — ships from Japan"`, `"Element14 / UK"`, `"DigiKey US warehouse"`, `"Mouser (in stock)"`). This is the unit of granularity for `batch_index`.
- **status** — *our* normalized outcome of the API call, not the API's raw HTTP code. Values: `ok` / `no_results` / `auth_failed` / `http_error` / `exception` / `missing_credentials` / `failed`. The literal HTTP status for any single attempt lives in the per-MPN JSON's `attempts[]` log.

## File manifest

A completed batch folder contains:

| File | Form | Rows per chip | Purpose |
|---|---|---|---|
| `batch_input.csv` | flat | 1 | Verbatim input from the xlsx |
| `batch_index.csv` / `.xlsx` | long | varies (≈ N_sources × ~3–10 warehouses) | Per `(MPN × source × warehouse)` row — primary analysis table |
| `batch_index.json` | nested | N_sources | Machine-readable: full record + chosen `extracted_best` per `(MPN × source)` |
| `failures.md` | markdown | varies | Non-ok `(chip × source)` pairs |
| `batch_summary.md` | markdown | — | TL;DR + per-source pass rate + highlights + manufacturer mismatches |
| `Test_<safe_mpn>_<SOURCE>/` | folder | — | Per-MPN-per-source run folder — one folder per `(chip, source)` pair |

`<safe_mpn>` = input MPN with non-`[A-Za-z0-9._-]` replaced by `_`. Examples: `PIC16F18446T-I/SS` → `PIC16F18446T-I_SS`; `BT168GW,115` → `BT168GW_115`.

For a 103-chip × 4-source sweep the (chip × source) pair count is 412. The warehouse-row count is typically 1.2–2× the pair count (Arrow contributes ~3–9 warehouses per chip; Mouser/Digikey ~1–3).

---

## `batch_index.csv` / `batch_index.xlsx` — long form (PRIMARY)

Granularity: **one row per `(input_mpn × source × warehouse)`**. When a chip is not found in a given source (or the call failed), exactly **one row** is emitted for that `(chip, source)` with `status` set and the warehouse-level columns empty.

**Encoding:** CSV is UTF-8 with BOM (`utf-8-sig`). XLSX is openpyxl Workbook, sheet `batch_index`, header row bold + frozen at A2.

**Column order is fixed** (column index meaningful for parsers that need it):

| # | Column | Type | Nullable | Description |
|---:|---|---|---|---|
| 1 | `input_mpn` | str | no | MPN exactly as read from the xlsx (preserve original case, slashes, commas, spaces). Repeated across all warehouse rows for a given chip. |
| 2 | `expected_mfr` | str | no | Manufacturer as read from xlsx col 2. Repeated. May be Chinese or English. |
| 3 | `source` | enum | no | One of `MOUSER`, `DIGIKEY`, `ELEMENT14`, `ARROW`. |
| 4 | `status` | enum | no | Our normalized outcome — see [Status values](#status-values). |
| 5 | `returned_mpn` | str | yes | MPN of the variant chosen by the source. Picked by exact case-insensitive match against `input_mpn`; if none match, by highest `stock_now_qty`. Empty when `status != ok`. |
| 6 | `vendor_sku` | str | yes | Vendor SKU of the chosen variant. Mouser `mouser_part_number`; Digikey `digikey_part_number`; Element14 `element14_sku`; Arrow `arrow_item_id`. |
| 7 | `returned_mfr` | str | yes | Manufacturer string returned by the API. May differ from `expected_mfr` (full names like "STMicroelectronics" vs. shorthand "STM"; relisters; clone brands). |
| 8 | `mfr_match` | bool | no | `True` iff one of `expected_mfr` / `returned_mfr` is a substring of the other after `[^A-Z0-9]` strip + uppercase. Always `False` when either side is empty. |
| 9 | `warehouse` | str | yes | The warehouse's human-readable name — `stock_breakdown[].warehouse`. e.g. `"Arrow / VERICAL — ships from Japan"`, `"Element14 / UK"`, `"Mouser (in stock)"`, `"DigiKey US warehouse"`. Empty for failed `(chip, source)` rows. |
| 10 | `warehouse_idx` | int | yes | 1-based position of this warehouse within the chip×source group (after the Element14 aggregate-row filter). Useful for reconstructing per-MPN ordering after CSV sort. Empty on failed rows. |
| 11 | `ships_from` | str | yes | Origin country of the physical pool. Arrow only (sourced from `site_sources[].ships_from`). Empty for other sources. |
| 12 | `stockpool_qty` | int | yes | Quantity in *this* warehouse — `stock_breakdown[].quantity`. `null` for "factory lead time" rows (unbounded factory order — no committed quantity). |
| 13 | `ship_text` | str | yes | Canonical 发货时间 string for this warehouse — `stock_breakdown[].ship_text`. e.g. `"在库 · lead 295 天"`, `"原厂标准交货期 30 weeks"`, `"Ships in 1 days · mfr lead 30 天"`. Channel-native wording is preserved verbatim. |
| 14 | `lead_time_days` | int | yes | Manufacturer lead time **in days** for *this* warehouse when the source exposes it (Element14 region, Arrow source); otherwise the top-level fallback. Mouser stores lead in days already; Digikey is `ManufacturerLeadWeeks × 7`. |
| 15 | `moq` | int | yes | Minimum order quantity for *this* warehouse when the source exposes per-warehouse MOQ (Arrow only); otherwise the top-level `extracted.min_order_qty`. |
| 16 | `min_break_qty` | int | yes | Smallest qty break in *this* warehouse's price-tier table (Arrow: per-warehouse tiers from `site_sources[].tiers`; others: top-level `extracted.prices`). |
| 17 | `price_at_min_qty` | float | yes | Unit price at `min_break_qty`. |
| 18 | `max_break_qty` | int | yes | Largest qty break (i.e. the volume-tier boundary). |
| 19 | `price_at_max_qty` | float | yes | Unit price at `max_break_qty` (typically the cheapest tier). |
| 20 | `num_price_tiers` | int | no | Total tier count for this warehouse's price list (0 when no prices). |
| 21 | `currency` | str | yes | Currency for the price tiers in this row. Arrow: per-warehouse `site_sources[].currency` (typically USD/EUR/JPY). Mouser .cn key always `RMB`. Digikey always `USD`. Element14 derives from `storeInfo.id` (Chinese store → `CNY`). |
| 22 | `datasheet_url` | str | yes | Direct PDF URL when the API exposes one. Repeated across warehouse rows. |
| 23 | `run_subdir` | str | no | Forward-slash relative path from PROJECT_ROOT to the per-MPN-per-source folder. Use to load `<safe_mpn>.json` for full detail. |
| 24 | `error` | str | yes | Truncated error message (≤500 chars). For `http_error`: short response body excerpt. For `exception`: `TypeName: message`. For `auth_failed`: cause from the auth round-trip. Empty on ok / no_results / missing_credentials. |

### Element14 aggregate row (kept; dedup before summing)

Element14's `stock_breakdown[]` carries a `"Stock level (total)"` row (warehouse name `"Element14 (cn.element14.com)"`) whose `quantity` equals the **sum** of the per-region rows. This row is **kept** in `batch_index` because it is what a buyer sees on cn.element14.com — the canonical site-level total + ship SLA (`"e络盟 在库,下单后立即发货"`). The per-region rows (`"Element14 / UK"`, `"Element14 / SG"`, `"Element14 / Shanghai"`) are tactical detail.

The trade-off is the same as Arrow mirrors: naive `SUM(stockpool_qty) GROUP BY input_mpn, source` will double-count this aggregate. Filter by `label` (or `warehouse`) before summing:

- To use the site-level total: `WHERE source = 'ELEMENT14' AND warehouse = 'Element14 (cn.element14.com)'`
- To sum the per-region rows: `WHERE source = 'ELEMENT14' AND warehouse LIKE 'Element14 / %'`

Pick one — never sum both.

### Arrow mirror rows

Arrow's inventory is published twice — the same physical pool appears under both the Verical (verical.com) source and the Arrow Americas/EUROPE (arrow.com) source. The driver detects these by `(fohQty, shipsFrom, shipsIn)` tuple and labels the duplicate with a `" — mirror"` suffix in `warehouse`. Mirror rows are emitted in `batch_index.csv` (with `stockpool_qty` preserved) so downstream queries can choose their aggregation policy:

- Naive `SUM(stockpool_qty) GROUP BY input_mpn, source` will **double-count** mirror rows.
- Either filter `WHERE warehouse NOT LIKE '% — mirror'` before summing, or take `MAX(stockpool_qty) GROUP BY input_mpn, source, ships_from`.

The canonical chip-level `stock_now_qty` in the per-MPN JSON already deduplicates mirrors — use that scalar for chip-level reporting and reserve the exploded warehouse rows for per-warehouse analysis.

### Status values

| Value | Meaning | When |
|---|---|---|
| `ok` | API call succeeded, at least one variant returned and normalized, at least one warehouse row emitted. | Happy path. |
| `no_results` | API ran fine but returned no matching products (or returned products with empty `stock_breakdown[]`). | Mouser: both `/search/partnumber` and the keyword fallback came back empty. Digikey: both `ExactMatches[]` and `Products[]` empty. Element14: both `manuPartNum` and `any` term searches empty. Arrow: both `useExact=true` and `useExact=false` empty. |
| `missing_credentials` | Required env vars not present in `api/.env`. | E.g. `ELEMENT14_API_KEY` empty; `ARROW_LOGIN` / `ARROW_API_KEY` missing. |
| `auth_failed` | OAuth or API-key auth was rejected. | Digikey token endpoint non-200 / empty `access_token`; Arrow `returnCode == 401`. |
| `http_error` | API returned a non-200 status. | Inspect `error` for the response body excerpt; check `attempts[]` in `batch_index.json` for retry detail. |
| `exception` | Driver raised an unhandled exception. | Inspect `error` for the head of the traceback (full trace is on stdout at run time). |
| `failed` | The API call did not throw but no usable payload was produced. | Rare; defensive fallthrough. |

### Type coercion notes (when reading the CSV)

The CSV is loose-typed — every cell is a string. Apply:

- `mfr_match`: parse as `bool('True'/'False')`. Empty → `False`.
- `stockpool_qty`, `warehouse_idx`, `lead_time_days`, `moq`, `min_break_qty`, `max_break_qty`, `num_price_tiers`: empty → `None` (or `0` for `num_*`); else `int()`. **`stockpool_qty` empty distinguishes "unbounded factory order" from `0` "out of stock"** — check the matching `ship_text` cell to disambiguate.
- `price_at_min_qty`, `price_at_max_qty`: empty → `None`; else `float()`.

Reading the XLSX with `openpyxl` returns the underlying Python types directly — no coercion needed.

### Currency caveat (do not skip)

`batch_index` is **multi-currency on a single row basis**. Mouser .cn key returns `RMB`, Digikey returns `USD`, Element14 (cn.element14.com) returns `CNY`, Arrow returns USD/EUR/JPY/etc. per warehouse. Any cross-source price math must reconcile units. There is **no FX-rate column** — cross-currency conversion is left to downstream tools.

Lead-time units are normalized to **days** in `lead_time_days`. The free-text `ship_text` may still carry the source-native unit (Digikey uses "weeks" in its English string, Mouser .cn returns "天" / 天数, Element14 uses "天" or "days").

---

## `batch_index.json` — machine-readable per-`(MPN × source)` records

Array of objects, one per `(MPN × source)` call (NOT per warehouse — for warehouse granularity, use `batch_index.csv`). Each object:

```json
{
    "source": "ARROW",
    "input_mpn": "STM32G030F6P6",
    "expected_mfr": "STM",
    "elapsed_sec": 2.13,
    "record": { /* full parent record as returned by api_<vendor>.call_api */ },
    "extracted_best": { /* the chosen best-variant `extracted` dict — includes stock_breakdown[] */ },
    "error": null
}
```

- `record` mirrors the per-MPN-per-source folder's `<safe_mpn>.json`: `query`, `channel`, `scraped_at_utc`, `source`, `search_url`, `output_dir`, `method`, `paywall`, `attempts[]`, `data_quality`, `status`, `variants_summary[]`.
- `extracted_best` is the chosen variant's normalized `extracted` dict (same shape as the per-variant JSON's `extracted` field). Includes the full `stock_breakdown[]` array that batch_index.csv is exploded from, plus `site_*` source-native fields, `prices`, `parameters`.
- For ok rows, both `record` and `extracted_best` are populated. For non-ok rows, `record` carries failure details (status, attempts, blocker/error fields) and `extracted_best` is `null`.

Use this when CSV explosion isn't enough — e.g. to access the Digikey OAuth round-trip in `attempts[]`, or the Arrow `site_sources[]` pipeline entries, or the full `parameters` list.

---

## Per-MPN-per-source folder (`Test_<safe_mpn>_<SOURCE>/`)

Identical shape to a single-MPN invocation of `api_mouser.py` / `api_digikey.py` / `api_element14.py` / `api_arrow.py` (the batch driver passes the per-MPN folder as `run_dir` to the same code path):

```
Test_<safe_mpn>_<SOURCE>/
├── <safe_mpn>.json           ← parent record (status, attempts, variants_summary)
├── parent_summary.md         ← rendered overview + per-source notes
├── raw_response.json         ← verbatim API payload (for audit)
└── <variant_mpn>/            ← one subfolder per distinct returned MPN string
    ├── <variant_mpn>.json    ← canonical record + full `extracted` for this variant
    ├── <variant_mpn>_raw_part.json (Mouser/Arrow) / _raw_product.json (Digikey/Element14)
    └── <variant_mpn>_summary.md   ← rendered by common/_summary.py
```

For unambiguous part-number lookups (e.g. `BT168GW,115` on Mouser), there's exactly one variant subfolder. For ambiguous keyword matches (e.g. `STM32G030F6P6` on Digikey), multiple variant subfolders appear — one per distinct returned MPN.

Per-variant grouping rule (never aggregate across MPN strings): see `MEMORY.md` / `feedback_mpn_variant_grouping.md`.

---

## Canonical `extracted` field reference

When loading a parent or variant JSON via `record["extracted"]` (or `batch_index.json[i]["extracted_best"]`), expect these keys on every source (`null`/`0`/`[]` when N/A):

- `manufacturer_part_number` — exact MPN of THIS variant (may differ from input MPN).
- `manufacturer` — vendor's manufacturer string. Almost always English; occasionally bilingual on Mouser .cn.
- Source-specific SKU keys: `mouser_part_number`, `digikey_part_number`, `element14_sku`, `arrow_item_id`.
- `stock_now_qty`, `stock_now_ship_text` — chip-level canonical 现货 quantity + delivery SLA. For Arrow this is mirror-deduplicated.
- `stock_future_qty`, `stock_future_ship_text` — chip-level canonical 期货 / 在途 / factory-order. `stock_future_qty` may be `null` for unbounded factory orders.
- `stock_breakdown` — **list of warehouse rows** (the data that `batch_index.csv` is exploded from). Each row: `{label, warehouse, quantity, ship_text, [moq], [note]}`.
- `prices` — list of `{min_qty, unit_price, unit_price_float, currency, [to_qty]}`. Top-level price tiers; for Arrow these come from the first in-stock source.
- `parameters` — list of `{name, value}` (key names follow the source's native wording).
- `datasheet_url`, `image_url`, `product_url`, `category_name_en`, `lifecycle_status`, `package`, `is_rohs`, `hts_code`, `eccn` — vendor-specific availability.

Site/API-native fields (per the "site-native wording" rule) are namespaced with `site_*`:

- Mouser: `site_availability`, `site_factory_stock`, `site_lead_time`, `site_availability_on_order`.
- Digikey: `site_quantity_available`, `site_manufacturer_lead_weeks`, `site_normally_stocking`, `site_back_order_not_allowed`, etc.
- Element14: `site_stock_level`, `site_lead_time_days`, `site_regional_breakdown`, `site_warehouse_breakdown`, `site_store_id`.
- Arrow: `site_sources` (the rich per-source pool data — currency, tiers, ships_from, mfr_lead_time_days, date_code, country_of_origin, pipeline, is_mirror_of_earlier).

The canonical `stock_now_*` / `stock_future_*` scalars are the cross-source interpretation layer.

---

## How to load the batch in Python

```python
import csv
from pathlib import Path

batch = Path("test/api/BatchTest_20260518_18_00_00")

# Quick analysis: long form
with open(batch / "batch_index.csv", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

# Pass rate per source (dedup by (mpn, source) — warehouse rows repeat per chip)
print("Pass rate per source:")
for src in ("MOUSER", "DIGIKEY", "ELEMENT14", "ARROW"):
    src_rows = [r for r in rows if r["source"] == src]
    pairs = {(r["input_mpn"], r["source"]) for r in src_rows}
    ok_pairs = {(r["input_mpn"], r["source"]) for r in src_rows if r["status"] == "ok"}
    print(f"  {src}: {len(ok_pairs)}/{len(pairs)}")

# Where can I buy STM32G030F6P6, in-stock, MOQ ≤ 10, sorted by price?
candidates = [
    r for r in rows
    if r["input_mpn"] == "STM32G030F6P6"
    and r["status"] == "ok"
    and r["stockpool_qty"] and int(r["stockpool_qty"]) > 0
    and r["moq"] and int(r["moq"]) <= 10
    and "mirror" not in r.get("warehouse", "").lower()
]
candidates.sort(key=lambda r: float(r["price_at_min_qty"]) if r["price_at_min_qty"] else float("inf"))
for r in candidates[:10]:
    print(f"  {r['source']:9s}  {r['warehouse']:55s}  qty={r['stockpool_qty']:>7s}  "
          f"@ {r['price_at_min_qty']:>7s} {r['currency']}  moq={r['moq']}")

# Pull full record for one (MPN, source) cell
import json
mpn, src = "STM32G030F6P6", "ARROW"
safe = mpn.replace("/", "_").replace(",", "_")  # use the sanitization rule
rec = json.loads((batch / f"Test_{safe}_{src}" / f"{safe}.json").read_text(encoding="utf-8"))
for v in rec.get("variants_summary", []):
    print(v.get("manufacturer_part_number"), v.get("stock_now_qty"))
```

For the JSON-rich path use `batch_index.json` directly; it carries the full `record` plus `extracted_best` per `(MPN × source)`, including the unexploded `stock_breakdown[]` and `site_*` fields.
