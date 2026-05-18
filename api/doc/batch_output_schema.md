# API batch output schema

This is the data contract for `batch_api_test.py` outputs. Any downstream tool (or future Claude session) parsing files under `test/api_test/BatchTest_<YYYYMMDD>_<HH_MM_SS>/` should rely on the column names and value rules below, not on column order.

Driver: `api/scripts/batch_api_test.py`. Channels: `MOUSER`, `DIGIKEY` (more vendors can be added by extending `channels_to_run`).

## File manifest

A completed batch folder contains:

| File | Form | Rows per chip | Purpose |
|---|---|---|---|
| `batch_input.csv` | flat | 1 | Verbatim input from the xlsx |
| `batch_index.csv` / `.xlsx` | long | N (one per channel) | Per (MPN × channel) summary — primary analysis table |
| `batch_index.json` | nested | N | Machine-readable, includes full record + chosen `extracted_best` |
| `failures.md` | markdown | varies | Non-ok rows |
| `batch_summary.md` | markdown | — | TL;DR + per-channel pass rate + highlights + manufacturer mismatches |
| `Test_<safe_mpn>_<CHANNEL>/` | folder | — | Per-MPN-per-channel run folder (one folder per row of batch_index) |

`<safe_mpn>` = input MPN with non-alphanumeric (except `.`, `_`, `-`) replaced by `_`. Examples: `PIC16F18446T-I/SS` → `PIC16F18446T-I_SS`; `BT168GW,115` → `BT168GW_115`.

For the most recent full run (103 chips × 2 channels = 206 calls), `batch_index.csv` has 206 data rows.

`batch_compare.csv` / `.xlsx` are still emitted by the driver for now but are NOT part of the supported contract — downstream tooling should treat them as deprecated and rely on `batch_index.csv` (long form) instead. Cross-channel comparisons can be derived from the long form with a `pivot` / `groupby input_mpn`.

---

## `batch_index.csv` / `batch_index.xlsx` — long form (PRIMARY)

One row per `(input_mpn × channel)`.

**Encoding:** CSV is UTF-8 with BOM (`utf-8-sig`). XLSX is openpyxl Workbook, sheet `batch_index`, header row bold + frozen at A2.

**Column order is fixed** (column index meaningful for parsers that need it):

| # | Column | Type | Nullable | Description |
|---:|---|---|---|---|
| 1 | `input_mpn` | str | no | MPN exactly as read from the xlsx (preserve original case, slashes, commas, spaces). |
| 2 | `expected_mfr` | str | no | Manufacturer name as read from the xlsx (col 2). May be Chinese or English. |
| 3 | `channel` | enum | no | One of `MOUSER`, `DIGIKEY`. |
| 4 | `status` | enum | no | Outcome — see [Status values](#status-values). |
| 5 | `num_variants` | int | no | Number of MPN variants the API returned. Mouser: `len(SearchResults.Parts)` after dedupe by `ManufacturerPartNumber`; Digikey: `ExactMatches[]` ⊕ `Products[]` deduped by `ManufacturerProductNumber`. Can be `0` on `no_results`. |
| 6 | `returned_mpn` | str | yes | MPN string of the "best" variant the channel returned. Picked by exact case-insensitive match against `input_mpn`; if none match, by highest `stock_now_qty`. Empty on non-ok. |
| 7 | `returned_mfr` | str | yes | Manufacturer string returned by the API. May differ from `expected_mfr` (full names like "STMicroelectronics" vs. shorthand "STM"; relisters; clone brands). |
| 8 | `mfr_match` | bool | no | `True` iff one of `expected_mfr` / `returned_mfr` is a substring of the other after `[^A-Z0-9]` strip + uppercase. Always `False` when either side is empty. |
| 9 | `stock_now_qty` | int | yes | 现货 / in-stock quantity from the best variant's `extracted.stock_now_qty`. Mouser: numeric prefix of `Availability`; Digikey: `QuantityAvailable`. |
| 10 | `stock_future_qty` | int / null | yes | 期货 / 在途 / factory-order quantity from `extracted.stock_future_qty`. **Can be `null` for unbounded factory orders** — Mouser non-empty `FactoryStock`; Digikey: `null` whenever `BackOrderAllowed` (which is most active SKUs) — distinguish from `0` (no future stock available). |
| 11 | `stock_future_ship_text` | str | yes | Human-readable shipping SLA for future stock. Mouser .cn key returns Chinese (e.g. `"原厂标准交货期 56 天数"` — note: "天数" means *days* on the .cn key, NOT weeks). Digikey: `f"Lead Time: {ManufacturerLeadWeeks} weeks"`. Channel-native wording is preserved verbatim. |
| 12 | `price_at_qty_1` | float | yes | Unit price at quantity break = 1. Falls back to the smallest break tier when no qty=1 tier exists. Currency follows column 16. |
| 13 | `min_break_qty` | int | yes | Smallest qty break in the channel's price-tier table. |
| 14 | `lowest_unit_price` | float | yes | Unit price at the LARGEST qty break (cheapest unit price). |
| 15 | `num_price_tiers` | int | no | Total count of price tiers returned by the channel (0 on non-ok). |
| 16 | `currency` | str | yes | Currency code. Mouser .cn key always returns `RMB`; Digikey returns `USD`. Empty on non-ok or when the channel omits a currency code. |
| 17 | `datasheet_url` | str | yes | Direct PDF URL when the API exposes one. Mouser's `DataSheetUrl`; Digikey's `DatasheetUrl`. |
| 18 | `run_subdir` | str | no | Forward-slash relative path from PROJECT_ROOT to the per-MPN-per-channel folder. Use to load `<safe_mpn>.json` for full detail. |
| 19 | `error` | str | yes | Truncated error message (≤500 chars). For `http_error`: short response body excerpt. For `exception`: `TypeName: message`. For `auth_failed`: cause from the OAuth round-trip. Empty on ok / no_results. |

### Status values

| Value | Meaning | When |
|---|---|---|
| `ok` | API call succeeded, at least one variant returned and normalized. | Happy path. |
| `no_results` | API ran fine but returned no matching products. | Mouser: both `/search/partnumber` and the `/search/keyword` fallback came back with `NumberOfResult: 0`. Digikey: both `ExactMatches[]` and `Products[]` empty. |
| `missing_credentials` | Required env vars not present in `api/.env`. | `MOUSER_API_KEY` empty (Mouser); `DIGIKEY_CLIENT_ID` / `DIGIKEY_CLIENT_SECRET` empty (Digikey). |
| `auth_failed` | OAuth or API-key auth was rejected. | Digikey only: token endpoint returned non-200 or empty `access_token`. |
| `http_error` | API returned a non-200 status. | Inspect `error` for the response body excerpt; check `attempts[]` in `batch_index.json` for retry detail. |
| `exception` | Driver raised an unhandled exception. | Inspect `error` for the traceback head; full trace was printed to stdout at run time. |
| `failed` | The API call did not throw but no usable payload was produced. | Rare; defensive fallthrough. |

### Type coercion notes (when reading the CSV)

The CSV is loose-typed — every cell is a string. Apply:

- `mfr_match`: parse as `bool('True'/'False')`. Empty → `False`.
- `stock_now_qty`, `stock_future_qty`, `min_break_qty`, `num_price_tiers`, `num_variants`: empty string → `None` (or `0` for `num_*`); else `int()`. Be careful — `stock_future_qty` empty means "unbounded factory order" semantically (not zero); compare with the matching `*_ship_text` column to disambiguate.
- `price_at_qty_1`, `lowest_unit_price`: empty → `None`; else `float()`.

Reading the XLSX with `openpyxl` returns the underlying Python types directly — no coercion needed.

### Currency caveat (do not skip)

The Mouser API key currently in use is registered against **mouser.cn**, so every Mouser response is returned in zh-CN locale with `currency=RMB ¥` and lead times measured in **days** (string `"<N> 天数"`). Digikey is `currency=USD` with lead times in **weeks** (string `"... <N> weeks"`). Any cross-channel price or lead-time math must reconcile the two units; see `api/doc/api_report_v1.md` for the full discussion. There is **no FX-rate column** in `batch_index` — cross-currency conversion is left to downstream tools.

---

## `batch_index.json` — machine-readable long form

Array of objects, one per (MPN × channel) call, in input order × channel order. Each object:

```json
{
    "channel": "MOUSER",
    "input_mpn": "STM32G030F6P6",
    "expected_mfr": "STM",
    "elapsed_sec": 2.13,
    "record": { /* full parent record as returned by api_<vendor>.call_api */ },
    "extracted_best": { /* the chosen best-variant `extracted` dict */ },
    "error": null
}
```

- `record` is the same structure the per-MPN-per-channel folder's `<safe_mpn>.json` carries: `query`, `channel`, `scraped_at_utc`, `source`, `search_url`, `output_dir`, `method`, `paywall`, `attempts[]`, `data_quality`, `status`, and `variants_summary[]` (a slim index of returned variants).
- `extracted_best` is the same dict as the chosen variant's per-variant JSON `extracted` field, normalized by `api_<vendor>.normalize_*()`.
- For ok rows, both `record` and `extracted_best` are populated. For non-ok rows, `record` carries the failure details (status, attempts, blocker/error fields) and `extracted_best` is `null`.

Use this when the long-form CSV summary isn't enough — for example, to access the full `attempts[]` array (including the Digikey OAuth round-trip), the `stock_breakdown[]` list, or per-channel `site_*` fields.

---

## Per-MPN-per-channel folder (`Test_<safe_mpn>_<CHANNEL>/`)

Identical shape to a single-MPN invocation of `api_mouser.py` / `api_digikey.py` (the batch driver passes the per-MPN folder as `run_dir` to the same code path):

```
Test_<safe_mpn>_<CHANNEL>/
├── <safe_mpn>.json           ← parent record (status, attempts, variants_summary)
├── parent_summary.md         ← rendered overview + per-channel notes
├── raw_response.json         ← verbatim API payload (for audit)
└── <variant_mpn>/            ← one subfolder per distinct returned MPN string
    ├── <variant_mpn>.json    ← canonical record + full `extracted` for this variant
    ├── <variant_mpn>_raw_part.json (Mouser) / _raw_product.json (Digikey)
    └── <variant_mpn>_summary.md   ← rendered by common/_summary.py
```

For unambiguous part-number lookups (e.g. `BT168GW,115` on Mouser), there's exactly one variant subfolder and it carries the same MPN string. For ambiguous keyword matches (e.g. `STM32G030F6P6` on Digikey), multiple variant subfolders appear — one per `ManufacturerProductNumber` returned.

Per-variant grouping rule (never aggregate across MPN strings): see `MEMORY.md` / `feedback_mpn_variant_grouping.md`.

---

## Canonical `extracted` field reference

When loading a parent or variant JSON via `record["extracted"]` (or `batch_index.json[i]["extracted_best"]`), expect these keys on every channel (`null`/`0`/`[]` when N/A):

- `manufacturer_part_number` — exact MPN of THIS variant (may differ from input MPN).
- `manufacturer` — vendor's manufacturer string (always English on Digikey; usually English on Mouser, occasionally bilingual).
- `mouser_part_number` (Mouser only) — Mouser SKU like `511-STM32G030F6P6`.
- `digikey_part_number` (Digikey only) — Digi-Key part number like `497-STM32G030F6P6-ND`.
- `stock_now_qty`, `stock_now_ship_text` — 现货 quantity + delivery SLA.
- `stock_future_qty`, `stock_future_ship_text` — 期货 / 在途 / factory-order quantity + SLA. `stock_future_qty` may be `null` for unbounded factory orders (Digikey active SKUs always; Mouser when `FactoryStock` is non-numeric).
- `stock_breakdown` — list of `{label, warehouse, quantity, ship_text, note}` rows. Mouser typically has one in-stock row + one factory row + N `OnOrder` rows (one per `AvailabilityOnOrder` entry). Digikey has one DigiKey-warehouse row + one factory row + one per `ProductVariations[]` packaging.
- `prices` — list of `{min_qty, unit_price, unit_price_float, currency}`. Mouser includes the currency code per tier; Digikey emits USD floats.
- `parameters` — list of `{name, value}`. Mouser uses Chinese parameter names on the .cn key (`封装`, `标准包装数量`); Digikey uses English (`Package / Case`, `Voltage - Off State`).
- `datasheet_url`, `image_url`, `product_url`, `category_name_en`, `lifecycle_status`, `package` — vendor-specific availability.
- `is_rohs`, `hts_code`, `eccn` — compliance / classification metadata. Digikey populates these; Mouser exposes them as `ProductCompliance` entries (not currently promoted to top-level).

Site/API-native fields (preserved per the "site-native wording" rule) are namespaced with `site_*` (Mouser: `site_availability`, `site_factory_stock`, `site_lead_time`, `site_availability_on_order`; Digikey: `site_quantity_available`, `site_manufacturer_lead_weeks`, `site_normally_stocking`, `site_back_order_not_allowed`, etc.). The canonical `stock_now_*` / `stock_future_*` scalars are the cross-channel interpretation layer.

---

## How to load the batch in Python

```python
import csv
from pathlib import Path

batch = Path("test/api_test/BatchTest_20260517_16_07_16")

# Quick analysis: long form
with open(batch / "batch_index.csv", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

print("Pass rate per channel:")
for ch in ("MOUSER", "DIGIKEY"):
    ch_rows = [r for r in rows if r["channel"] == ch]
    ch_ok = sum(1 for r in ch_rows if r["status"] == "ok")
    print(f"  {ch}: {ch_ok}/{len(ch_rows)}")

# Pull full record for one (MPN, channel) cell
import json
mpn, ch = "STM32G030F6P6", "DIGIKEY"
safe = mpn.replace("/", "_").replace(",", "_")  # use the sanitization rule
rec = json.loads((batch / f"Test_{safe}_{ch}" / f"{safe}.json").read_text(encoding="utf-8"))
for v in rec.get("variants_summary", []):
    print(v.get("manufacturer_part_number"), v.get("stock_now_qty"))

# For per-variant detail — open the per-variant JSON inside the per-MPN folder
for v_dir in (batch / f"Test_{safe}_{ch}").iterdir():
    if v_dir.is_dir():
        vj = json.loads((v_dir / f"{v_dir.name}.json").read_text(encoding="utf-8"))
        ex = vj.get("extracted") or {}
        print(ex.get("manufacturer_part_number"), ex.get("stock_now_qty"),
              ex.get("stock_future_ship_text"))
```

For the JSON-rich path use `batch_index.json` directly; it already carries the full `record` plus `extracted_best` (no need to re-load per-MPN files).
