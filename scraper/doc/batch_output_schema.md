# Scraper batch output schema

This is the data contract for `batch_scraper_test.py` outputs. Any downstream tool (or future Claude session) parsing files under `test/scraper_test/BatchTest_<YYYYMMDD>_<HH_MM_SS>/` should rely on the column names and value rules below, not on column order.

Driver: `scraper/scripts/batch_scraper_test.py`. Channels: `LCSC`, `DIGIKEY`, `HQEW`, `FUTURE`.

## File manifest

A completed batch folder contains:

| File | Form | Rows per chip | Purpose |
|---|---|---|---|
| `batch_input.csv` | flat | 1 | Verbatim input from the xlsx |
| `batch_index.csv` / `.xlsx` | long | 4 (one per channel) | Per (MPN × channel) summary — primary analysis table |
| `batch_compare.csv` / `.xlsx` | wide | 1 (4 channels side-by-side) | Cross-channel comparison |
| `batch_index.json` | nested | 4 | Machine-readable, includes record + subprocess tails |
| `failures.md` | markdown | varies | Non-ok rows grouped by channel |
| `batch_summary.md` | markdown | — | TL;DR + per-channel pass rate + highlights |
| `Test_<safe_mpn>_<CHANNEL>/` | folder | — | Per-MPN-per-channel raw run (one folder per row of batch_index) |

`<safe_mpn>` = input MPN with non-alphanumeric (except `.`, `_`, `-`) replaced by `_`. Examples: `PIC16F18446T-I/SS` → `PIC16F18446T-I_SS`; `BT168GW,115` → `BT168GW_115`.

---

## `batch_index.csv` / `batch_index.xlsx` — long form (PRIMARY)

One row per `(input_mpn × channel)`. For a full 103-chip × 4-channel run, this file has 412 data rows.

**Encoding:** CSV is UTF-8 with BOM (`utf-8-sig`). XLSX is openpyxl Workbook, sheet `batch_index`, header row frozen.

**Column order is fixed** (column index meaningful for parsers that need it):

| # | Column | Type | Nullable | Description |
|---:|---|---|---|---|
| 1 | `input_mpn` | str | no | MPN exactly as read from the xlsx (preserve original case, slashes, commas, spaces). |
| 2 | `expected_mfr` | str | no | Manufacturer name as read from the xlsx (col 2). May be Chinese or English. |
| 3 | `channel` | enum | no | One of `LCSC`, `DIGIKEY`, `HQEW`, `FUTURE`. |
| 4 | `status` | enum | no | Outcome — see [Status values](#status-values). |
| 5 | `elapsed_sec` | float | yes | Wallclock seconds for the subprocess call. Null when row was reloaded via `--resume`. |
| 6 | `num_variants` | int | no | Number of MPN variants the channel returned (≥1 when ok; can be 0 on `no_results`). For LCSC/Future this is the count of variant subfolders; for HQEW it counts MPN variants inside `extracted.variants`; for Digikey it's 1 when ok else 0. |
| 7 | `returned_mpn` | str | yes | MPN string of the "best" variant the channel returned. Picked by exact-input match if present, else by highest `stock_now_qty`. Empty on non-ok. |
| 8 | `returned_mfr` | str | yes | Manufacturer string returned by the channel. May differ from `expected_mfr` (clone brands, EPCOS-vs-TDK historic acquisitions, etc.). |
| 9 | `mfr_match` | bool | no | `True` iff `expected_mfr` and `returned_mfr` share a substring after `[^A-Z0-9]` strip + upper. Always `False` when either side is empty. |
| 10 | `stock_now_qty` | int | yes | "现货" / in-stock quantity from the best variant's `extracted.stock_now_qty`. Sum across warehouses where the channel splits stock. |
| 11 | `stock_future_qty` | int / null | yes | "期货" / "在途" / factory-order quantity from `extracted.stock_future_qty`. **Can be `null` for unbounded factory orders** (Digikey: `null` means "any quantity, ships in lead-time weeks") — distinguish from `0` (no future stock available). |
| 12 | `stock_future_ship_text` | str | yes | Human-readable shipping SLA for future stock (e.g. `原厂标准交货期 8 周`, `Factory Lead Time: 4 Weeks`, `3个工作日内发货`). Channel-native wording is preserved verbatim. |
| 13 | `price_at_qty_1` | float | yes | Unit price at quantity break = 1. Falls back to the smallest break tier when no qty=1 tier exists. Channel currency (see col 17). |
| 14 | `min_break_qty` | int | yes | Smallest qty break in the channel's price-tier table. |
| 15 | `lowest_unit_price` | float | yes | Unit price at the LARGEST qty break (cheapest unit price). |
| 16 | `num_price_tiers` | int | no | Total count of price tiers returned by the channel (0 on non-ok). |
| 17 | `currency` | str | yes | ISO-ish currency code. Channel defaults: LCSC=`CNY`, Digikey=usually empty (Digikey tiers omit currency code; treat as USD on .com / CNY on .cn), HQEW=empty (云价格 is CNY by convention), Future=`SGD` (APAC site). |
| 18 | `datasheet_url` | str | yes | Direct PDF URL when the channel exposes one. May be relative for HQEW/LCSC. |
| 19 | `run_subdir` | str | no | Forward-slash relative path from PROJECT_ROOT to the per-MPN-per-channel folder. Use to load `<safe_mpn>.json` for full detail. |
| 20 | `error` | str | yes | Truncated error message (≤300 chars). For `blocked`: blocker name (e.g. `cloudflare_just_a_moment`). For `exception`: `TypeName: message`. Empty on ok / no_results. |

### Status values

| Value | Meaning | When |
|---|---|---|
| `ok` | Scrape succeeded, `extracted` populated, at least one variant returned. | Happy path. |
| `no_results` | Channel ran fine but returned no matching products. | LCSC `no_matches`, HQEW `no_listings`, Future no `product__list--code` anchors. |
| `blocked` | Bot/Cloudflare interstitial did not clear within the poll budget. | Mostly Digikey Cloudflare; rerun usually clears. |
| `exception` | Scraper raised an unhandled exception. | Inspect `error` + `run_subdir/_*.html` for forensics. |
| `timeout` | Subprocess exceeded the channel's hard wallclock budget. | Per-channel timeouts: LCSC 240s, Digikey 180s, HQEW 90s, Future 300s. |
| `exit_<N>` | Subprocess exited with non-zero code and no usable record on disk. | Rare; usually a missing dependency or import error. Check `batch_index.json` for stderr_tail. |
| `failed` | Channel-specific "method = failed" (e.g. Digikey's search returned no `/products/detail/` link). | Treat as "not stocked by this distributor", not a scraper bug. |

### Type coercion notes (when reading the CSV)

The CSV is loose-typed — every cell is a string. Apply:

- `mfr_match`: parse as `bool('True'/'False')`. Empty → `False`.
- `stock_now_qty`, `stock_future_qty`, `min_break_qty`, `num_price_tiers`, `num_variants`: empty string → `None` (or `0` for `num_*`); else `int()`.
- `elapsed_sec`, `price_at_qty_1`, `lowest_unit_price`: empty → `None`; else `float()`.

Reading the XLSX with `openpyxl` returns the underlying Python types directly — no coercion needed.

---

## `batch_compare.csv` / `batch_compare.xlsx` — wide form

One row per `input_mpn`. 43 columns total: 2 input cols + 10 channel-fields × 4 channels + 1 cross-channel disagreement flag.

**Column naming:** `<channel>_<field>` in lowercase. Channel order in the file is fixed: `lcsc`, `digikey`, `hqew`, `future`.

For each channel, the 10 fields are:
```
status, num_variants, returned_mpn, returned_mfr, mfr_match,
stock_now_qty, stock_future_qty, stock_future_ship_text,
price_at_qty_1, datasheet_url
```

Plus:

| Column | Type | Description |
|---|---|---|
| `input_mpn` | str | Same as in batch_index. |
| `expected_mfr` | str | Same as in batch_index. |
| `stock_now_disagreement` | enum | One of `all_have`, `none_have`, `only_<ch1>+<ch2>+...` (lowercase channel names joined by `+`). Computed across channels where `stock_now_qty > 0`. |

Use `batch_index.csv` for cell-level joins; use `batch_compare.csv` when you want one row per chip with all four channels side-by-side.

---

## `batch_index.json` — machine-readable long form

Array of objects, one per (MPN × channel) call, in input order × channel order. Each object:

```json
{
    "channel": "LCSC",
    "input_mpn": "STM32G030F6P6",
    "expected_mfr": "STM",
    "elapsed_sec": 21.1,
    "subprocess_status": "ok",
    "stdout_tail": "... last 2000 chars of subprocess stdout ...",
    "stderr_tail": "... last 2000 chars of subprocess stderr ...",
    "record": { /* full parent record as written by the scraper */ },
    "extracted_best": { /* the chosen best-variant `extracted` dict */ },
    "error": null
}
```

`record` is the same structure the scraper writes to `<safe_mpn>.json` under the per-MPN-per-channel folder — it carries the full `extracted` (or `variants[]` for multi-variant channels), all `attempts[]`, `data_quality`, etc. Use this as the source of truth when CSV-level summaries are not enough.

For `--resume` rows, `subprocess_status` is `resume_skipped` and `stdout_tail`/`stderr_tail` are absent.

---

## Per-MPN-per-channel folder (`Test_<safe_mpn>_<CHANNEL>/`)

Contents depend on the channel, but every folder always has the parent JSON:

```
Test_<safe_mpn>_<CHANNEL>/
├── <safe_mpn>.json          ← canonical record (parent for multi-variant channels)
├── <safe_mpn>_summary.md    ← human-readable, rendered by common/_summary.py
└── ...                      ← per-scraper artefacts (HTML, screenshots, raw __NEXT_DATA__)
```

For multi-variant channels (LCSC, Future), one additional subfolder per returned MPN variant:

```
Test_<safe_mpn>_LCSC/
├── STM32G030F6P6.json       ← parent: `variants: [...]` + aggregates
├── parent_summary.md
├── _search.html / _search.png
├── STM32G030F6P6TR/         ← variant subfolder
│   ├── STM32G030F6P6TR.json     ← variant record with full `extracted`
│   ├── STM32G030F6P6TR_summary.md
│   ├── STM32G030F6P6TR_raw_next_data.json
│   ├── STM32G030F6P6TR_product.html
│   └── STM32G030F6P6TR_product.png
└── STM32G030F6P6/           ← another variant
    └── ...
```

HQEW and Digikey are single-record channels — no variant subfolders. HQEW's `extracted.variants` lists MPN variants inline (different MPN strings the search returned), but each variant's listings live inside the same JSON, not in subfolders.

---

## Canonical `extracted` field reference

When loading a parent or variant JSON via `record["extracted"]`, expect these keys on every channel (`null`/`0`/`[]` when N/A):

- `manufacturer_part_number` — exact MPN of THIS variant (may differ from input MPN).
- `manufacturer` — channel's manufacturer string.
- `stock_now_qty`, `stock_now_ship_text` — 现货 quantity + delivery SLA.
- `stock_future_qty`, `stock_future_ship_text` — 期货/在途/factory-order quantity + SLA. `stock_future_qty` may be `null` for unbounded factory orders.
- `stock_breakdown` — list of `{label, warehouse, quantity, ship_text, ...optional extras}`. The optional extras vary by channel: HQEW adds `mpn`, `moq`, `batch_code`, `listing_date`, `remark`; Future may add `region` for the country pool.
- `prices` — list of `{min_qty, unit_price, ...}`. Some channels use `unit_price_cny` (LCSC) or `unit_price_float` (Digikey quantityTable).
- `parameters` — list of `{name (or name_cn/name_en), value, ...}`. Empty `[]` on HQEW.
- `datasheet_url`, `package`, `category_name_cn`/`category_name_en`, `lifecycle_status` — channel-specific availability.

Channel-specific fields (preserved per the "site-native wording" rule) are namespaced with `site_*` (e.g. `site_global_stock`, `site_factory_stock`, `site_factory_lead_time` on Future). The canonical `stock_now_*` / `stock_future_*` scalars are the cross-channel interpretation layer.

---

## How to load the batch in Python

```python
import csv
from pathlib import Path

batch = Path("test/scraper_test/BatchTest_20260517_17_21_57")

# Quick analysis: long form
with open(batch / "batch_index.csv", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

ok_rows = [r for r in rows if r["status"] == "ok"]
print(f"Pass rate per channel:")
for ch in ("LCSC", "DIGIKEY", "HQEW", "FUTURE"):
    ch_rows = [r for r in rows if r["channel"] == ch]
    ch_ok = sum(1 for r in ch_rows if r["status"] == "ok")
    print(f"  {ch}: {ch_ok}/{len(ch_rows)}")

# Pull full record for one (MPN, channel) cell
import json
mpn, ch = "STM32G030F6P6", "LCSC"
safe = mpn.replace("/", "_").replace(",", "_")  # use the sanitization rule
rec = json.loads((batch / f"Test_{safe}_{ch}" / f"{safe}.json").read_text(encoding="utf-8"))
for v in rec.get("variants", []):
    ex = v.get("extracted") or {}
    print(v["status"], ex.get("manufacturer_part_number"), ex.get("stock_now_qty"))
```

For the JSON-rich path use `batch_index.json` directly; it already carries the full `record` plus `extracted_best` (no need to re-load per-MPN files).
