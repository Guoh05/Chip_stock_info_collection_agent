# Merge for procurement — rules & output contract

Procurement-facing merge of the two `batch_index.csv` snapshots (API track +
Scraper track) into a single Excel workbook with **现货 (in-stock) rows
visually highlighted**, so buyers can scan one file and see what's available.

- Script: `common/merge_batch_for_procurement.py`
- Output root: `test/merged/Merge_<api_ts>__<scr_ts>/`
- Output files: `merged_procurement.xlsx` (3 sheets), `merged_procurement.csv` (= Sheet 2 contents)

## Inputs

- API CSV: `test/api_test/BatchTest_<ts>/batch_index.csv`
- Scraper CSV: `test/scraper_test/BatchTest_<ts>/batch_index.csv`

Schemas: `api/doc/batch_output_schema.md`, `scraper/doc/batch_output_schema.md`.
Both CSVs share 24 columns; scraper adds `elapsed_sec`, `num_variants` (dropped on merge).

Default: script auto-picks the newest `BatchTest_*` in each track. Override with `--api` / `--scr` / `--out`.

## Merge rules (in order)

1. **Status filter.** Keep `status == "ok"` rows only. Drop every non-ok row from both tracks (timeouts, blockers, errors).
2. **HQEW filter.** Drop every row where `source` starts with `HQEW_` (per business decision — 华强电子网 not currently trusted as a procurement signal).
3. **API-wins per (input_mpn, source).** For every `(mpn, source)` pair that has at least one status=ok row in the API CSV, **drop all scraper rows for the same pair** — even when the API rows are all qty=0 or null. This is strict by design (predictable provenance); the discrepancy goes to Sheet 3 instead of corrupting Sheet 2.

   Practical effect: scraper contributes only on sources the API track doesn't cover (Future, RSOnline, OneYac, ICKey, Rochester, …) plus any `(mpn, source)` where the API call itself failed.
4. **mfr_match kept.** Rows with `mfr_match=False` (channel returned a different manufacturer than expected — common on the scraper side: ~47%) are **kept**, not silently dropped. The `mfr_match` column is preserved so procurement can filter in Excel.
5. **Mirror rows flagged, not dropped.** A new `is_mirror` column flags rows that represent the same physical inventory counted twice:
   - Arrow: `warehouse` contains `" — mirror"` (per `api/doc/batch_output_schema.md` §Arrow mirrors).
   - Future scraper: `warehouse_idx > 1` AND warehouse contains `(global)` (per scraper schema §Future mirrors).
   - Element14 site-level: `warehouse` matches `Element14 (…)` shape.

   They're kept because Sheet 1 sorts mirrors adjacent (visually obvious) and no row-wise sum is computed.

## In-stock definition (Sheet 1 / highlight rule)

```
in_stock = (stockpool_qty is not None) AND (stockpool_qty > 0)
```

Both schemas guarantee:

| qty value | meaning | in_stock? |
|---|---|---|
| `> 0` | real warehouse stock | **yes** |
| `0` | out of stock | no |
| `None` / empty | unbounded factory order — lead-time only | **no** |

No `ship_text` parsing is needed; the quantity column already encodes the
distinction. **Do NOT highlight `None`-qty rows as 现货** — they represent
"原厂标准交货期 N 周" / "Factory Lead Time", not committed inventory.

## Cross-validation side-channel (Sheet 3)

For every `(mpn, source)` where BOTH tracks have status=ok rows (= LCSC,
DIGIKEY, WEEN overlaps in practice), compare the qty sets:

- `match` — some API warehouse qty equals scraper qty → silent (good signal).
- `one_side_null` — one side has `qty=None` while the other has a number → silent (different shape, not a conflict).
- `mismatch` — both sides have numbers and no overlap → **copy scraper rows to Sheet 3** with a `note` column like `API max qty 582295 vs scraper max 547550`.

Sheet 3 is for QA, not for procurement. It surfaces:
- Scraper bugs (wrong number / aggregation across variants).
- Real-world drift if the two batches ran far apart in time.
- Inventory pulled between snapshots.

## Output workbook

| Sheet | Filter | Sort | Highlight |
|---|---|---|---|
| `现货优先` | `in_stock == True` | `input_mpn`, then `stockpool_qty desc`, then `source` | All rows green (`FFC6EFCE`) |
| `全量数据` | All merged rows (post-filter) | `input_mpn`, `source`, API before scraper, then `warehouse_idx` | Green if `in_stock`; light grey (`FFEEEEEE`) if `stockpool_qty == 0`; no fill for `None`-qty (factory lead) |
| `scraper参考_库存不一致` | Mismatched scraper rows only (see above) | `input_mpn`, `source` | None |

Shared styling: header bold + light-grey fill, freeze panes at A2, column widths auto-fit (capped 10–50 chars). Matches the convention used by `batch_api_test.py` / `batch_scraper_test.py`.

### Sheet 1 & 2 columns (in order)

`input_mpn, expected_mfr, source, track, in_stock, returned_mpn, vendor_sku, returned_mfr, mfr_match, warehouse, warehouse_idx, ships_from, stockpool_qty, ship_text, lead_time_days, moq, min_break_qty, price_at_min_qty, max_break_qty, price_at_max_qty, num_price_tiers, currency, is_mirror, datasheet_url, status, run_subdir, error`

New columns added by the merge (not in either input CSV):

| Column | Type | Meaning |
|---|---|---|
| `track` | str | `"api"` or `"scraper"` — which CSV the row originated from |
| `in_stock` | bool | per the rule above |
| `is_mirror` | bool | per the mirror-row flagging above |

### Sheet 3 columns

`input_mpn, expected_mfr, source, in_stock, returned_mpn, warehouse, stockpool_qty, ship_text, lead_time_days, moq, price_at_min_qty, currency, datasheet_url, note, run_subdir`

`note` is added by the merge: `"API max qty X vs scraper max Y"`.

## Currency

Left as-is per row (`USD`, `RMB`, `CNY`, `SGD`, …). No FX normalisation in v1.
Cross-source price comparison requires an external FX table downstream.

## CLI

```bash
python common/merge_batch_for_procurement.py \
    [--api <api_BatchTest_dir>] \
    [--scr <scraper_BatchTest_dir>] \
    [--out <output_dir>]
```

Run with no args = newest batches in each track.

## Out of scope (v1)

- FX conversion / unified-currency column.
- Per-chip "best deal" aggregation across sources.
- Trend / delta vs previous batch.
- README auto-status block (the merge is not yet part of the batch driver
  pipeline, so its status isn't auto-rendered into any README).

## Known data quirks worth knowing

- **LCSC scraper sometimes returns `status=ok` with empty `stockpool_qty`.** The scraper reached the product page but didn't parse a number. This is a real scraper bug; the merge treats it correctly as `one_side_null` against API, so these never end up on Sheet 3.
- **API DigiKey emits 3–5 rows per MPN** (US warehouse + Factory + per-SKU variants). Sheet 1 shows them adjacent, sorted by qty desc within the MPN.
- **API LCSC emits 2 rows per MPN** (广东仓 + 江苏仓). Either or both can be in stock.
- **Scraper LCSC / DIGIKEY emit 1 row per MPN with empty `warehouse`** — that's an aggregate, not a per-warehouse view. Sheet 1 sort-by-qty handles it fine.
