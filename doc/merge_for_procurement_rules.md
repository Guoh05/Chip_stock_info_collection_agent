# Merge for procurement — rules & output contract

Procurement-facing merge of the two `batch_index.csv` snapshots (API track +
Scraper track) into a single Excel workbook with project-level shortage
metadata appended and a priority view of high-risk in-stock parts on
sheet 1.

- Script: `common/merge_batch_for_procurement.py`
- Output root: `<env_root>/merged/Merge_<api_ts>__<scr_ts>/`, where `<env_root>` is `test/` (default) or `production/` via `--env prod`.
- Output files: `Versuni_chip_stock_availability_check_<YYYYMMDD>.xlsx` (3 sheets) + `Versuni_chip_stock_availability_check_<YYYYMMDD>.csv` (= Sheet 2 contents). `<YYYYMMDD>` is the **run date** (when the merge is executed), formatted as `date.today().strftime("%Y%m%d")`. The folder name `Merge_<api_ts>__<scr_ts>/` stays unchanged.
- Fonts: every `openpyxl.styles.Font(...)` call uses `name="Calibri"`. Body cells inherit the workbook default (also Calibri).

## Inputs

- API CSV: `<env_root>/api/BatchTest_<ts>/batch_index.csv`
- Scraper CSV: `<env_root>/scraper/BatchTest_<ts>/batch_index.csv`
- Chip metadata: `ref/Shortage Emergency Response List_v2.xlsx` (sheet `Part List Modify`)

Schemas: `api/doc/batch_output_schema.md`, `scraper/doc/batch_output_schema.md`. Both CSVs share 24 columns; scraper adds `elapsed_sec`, `num_variants` (dropped on merge).

Default: script auto-picks the newest standard `BatchTest_<YYYYMMDD>_<HH>_<MM>_<SS>/` folder in each track (probe folders with extra suffixes like `..._bom2buy` are skipped — their schemas may differ). Override with `--api` / `--scr` / `--out` / `--chip-list`.

## Merge rules (in order)

1. **Status filter.** Keep `status == "ok"` rows only on both tracks.
2. **HQEW filter.** Drop every row where `source` starts with `HQEW_` (per business decision — 华强电子网 not currently trusted as a procurement signal).
3. **Returned-MPN match filter.** Drop every row where the upstream `returned_mpn` does NOT equal `input_mpn` after stripping punctuation and case (`re.sub(r"[^A-Za-z0-9]", "", s).upper()`). Catches fuzzy-search drift (e.g. LCSC returned `GD32E230K6T6` when we asked for `GD32E230K4T6`) and scraper rows with empty `returned_mpn`. Implemented via `returned_mpn_matches_input()`.
4. **API-wins per `(input_mpn, source)`.** For every `(mpn, source)` pair that has at least one status=ok row in the API CSV, **drop all scraper rows for the same pair** — even when the API rows are all qty=0 or null. Predictable provenance; the discrepancy goes to Sheet 3 instead of corrupting Sheet 2.
   Practical effect: scraper contributes only on sources the API track doesn't cover (Future, RSOnline, OneYac, ICKey, Rochester, …) plus any `(mpn, source)` where the API call itself failed.
5. **mfr_match kept.** Rows with `mfr_match=False` are kept; the flag is preserved (as `ref_mfr_match`) so procurement can filter in Excel.
6. **Mirror rows flagged, not dropped.** `ref_is_mirror=True` marks:
   - Arrow: `warehouse` contains `" — mirror"`.
   - Future scraper: `warehouse_idx > 1` AND warehouse contains `(global)`.
   - Element14 site-level: `warehouse` matches `Element14 (…)` shape.
7. **Chip-list enrichment.** Every output row is joined to the chip list on a normalized MPN key (`str.strip().replace("\xa0", "")`). The chip list has duplicates (one chip across multiple PCBAs); **only the first matching row** is used per MPN. Both A–D (Category / Project / EMS / 12NC_PCBA) and G–K (Quantity / Currency / Current Price / Type / risk) come from that first row.
8. **Lead time conversion.** Upstream `lead_time_days` (integer days) is converted to **`Lead Time (Week)` with 1 decimal** (`round(days / 7, 1)`).

## In-stock & "high-risk in-stock"

`in_stock` (bool column) is computed at row level: `(stockpool_qty is not None) AND (stockpool_qty > 0)`. `None`-qty rows are factory-lead-time only and **must not** be treated as 现货.

| qty value | meaning | `in_stock` |
|---|---|---|
| `> 0` | real warehouse stock | **TRUE** |
| `0` | out of stock | FALSE |
| `None` / empty | unbounded factory order — lead-time only | FALSE |

**Sheet 1 priority view** narrows that further: `risk == "high"` (case-insensitive on chip-list `risk`) AND `Available Quantity > 0`. Rows without a chip-list match (`risk` is null) are excluded from Sheet 1.

## Cross-validation side-channel (Sheet 3)

For every `(mpn, source)` where BOTH tracks have status=ok rows (= LCSC, DIGIKEY, WEEN overlaps in practice), compare the qty sets:

- `match` — some API warehouse qty equals scraper qty → silent.
- `one_side_null` — one side has `qty=None` → silent (different shape, not a conflict).
- `mismatch` — both sides numeric and no overlap → **copy scraper rows to Sheet 3** with a `note` column like `"API max qty 582295 vs scraper max 547550"`.

Sheet 3 is for QA; not for procurement.

## Output workbook

| Sheet name | Filter | Highlight |
|---|---|---|
| `高风险有货` | `risk == "high"` AND `Available Quantity > 0` | **None** — every row is in-stock by definition, so a uniform green fill would be uninformative |
| `全量数据` | All merged rows (post-filter) | Green (`FFC6EFCE`) if `in_stock`; light grey (`FFEEEEEE`) if `Available Quantity == 0`; no fill for `None`-qty (factory lead) |
| `scraper参考_库存不一致` | Mismatched scraper rows only (see above) | None |

**Unified sort across all three sheets**: `risk` (`high` → `low` → other → null) → `Manufacture Part Number` (asc) → `Broker name` (asc) → `Available Quantity` (desc). Encoded by `_sort_key()` in the merge script.

Shared styling:
- Header row: bold + `wrap_text` (for the `"Trade \nCurrency"` two-line header), colored by **column range**:
  - **A–K** (cols 1–11, chip-list metadata + MPN/Manufacture): dark blue `#1F4E78` background + white font.
  - **L–AC** (cols 12–29, distributor data + 3 business-fill cols): light orange `#FCE4D6` background + black font.
  - **AD onwards** (cols 30+, `ref_*` audit fields): dark grey `#595959` background + white font.
- Freeze panes at A2.
- AutoFilter dropdowns on the full data range (`A1:<last_col><last_row>`) — procurement can filter in Excel.
- Column widths: hardcoded from `ref/merged_header_example.xlsx` (sheet `header`), captured in the `COLUMN_WIDTHS` dict in the script. Anything not in the dict falls back to auto-fit (capped 10–50 chars).

### Column list (Sheet 1 & Sheet 2 — 38 cols, in order)

```
Category, Project, EMS/Finish Goods, 12NC_PCBA,
Manufacture Part Number, Manufacture,
Quantity, Currency, Current Price, Type, risk,
Broker name, Data collect method, in_stock,
Warehouse/vender, Stock Location, Available Quantity,
ship infor after order placed, Lead Time (Week),
MOQ, Minimum order qty, Unit price (min qty),
Maximum order qty, Unit price (max qty), Number of price tiers,
Trade \nCurrency,
Date of Code, Reel/Cut Reel, Certificate of Conformity(Yes/No),
ref_Warehouse/vender ID, ref_returned_mpn, ref_vendor_sku,
ref_returned_mfr, ref_mfr_match, ref_is_mirror, ref_datasheet_url,
ref_status, ref_error
```

Header → source mapping is defined by `COLUMN_SOURCE_MAP` in the script. Each entry is one of:

- `("merge", <csv_field>)` — copy from upstream CSV row (rename only).
- `("chip",  <chip_list_header>)` — first-row chip-list lookup.
- `("lead_time", "lead_time_days")` — days-to-weeks transform.
- `("blank", None)` — empty placeholder (procurement fills in later).

Three "blank" columns (`Date of Code`, `Reel/Cut Reel`, `Certificate of Conformity(Yes/No)`) are written empty for procurement to fill.

### Sheet 3 columns (23 cols, in order)

```
Category, Project, EMS/Finish Goods, 12NC_PCBA,
Manufacture Part Number, Manufacture,
Quantity, Currency, Current Price, Type, risk,
Broker name, in_stock,
ref_returned_mpn, Warehouse/vender, Available Quantity,
ship infor after order placed, Lead Time (Week),
MOQ, Unit price (min qty), Trade \nCurrency,
ref_datasheet_url, note
```

The `note` column is generated by the merge (`"API max qty X vs scraper max Y"`).

### Dropped from upstream

- `run_subdir` — marked DELETE in `ref/merged_output_fields_mapping.xlsx`.
- Upstream scraper-only `elapsed_sec`, `num_variants` — not in mapping.

## Currency

Two currency-shaped columns exist and **mean different things**:

| Column | Source | Meaning |
|---|---|---|
| `Currency` | chip list col H | the buying currency the EMS expects to pay in |
| `Trade \nCurrency` (literal newline) | upstream `currency` | the distributor's quoted currency on that row |

No FX normalisation in v1.

## CLI

```bash
python common/merge_batch_for_procurement.py \
    [--env {test,prod}] \
    [--api  <api_BatchTest_dir>] \
    [--scr  <scraper_BatchTest_dir>] \
    [--out  <output_dir>] \
    [--chip-list <xlsx_path>] \
    [--api-only | --scraper-only]
```

Run with no args = newest batches in each track under `test/` + default chip list. `--env prod` switches both the input batch roots (`production/api/`, `production/scraper/`) and the output root (`production/merged/`). Use explicit `--api` / `--scr` / `--out` to mix environments (rarely needed).

### Partial-merge modes

`--api-only` and `--scraper-only` are mutually exclusive. They let the pipeline produce a procurement xlsx when only one track has data (e.g., the orchestrator's scraper phase failed and the user chose to continue with API data only).

- `--api-only`: skip reading the scraper CSV; `scr_rows = []`; merge output contains API rows only. Output folder is `Merge_<api_ts>__none/`.
- `--scraper-only`: skip reading the API CSV; `api_rows = []`; merge output contains scraper rows only. Output folder is `Merge_none__<scr_ts>/`.
- Cross-validation (Sheet 3) requires both sides; under either partial flag Sheet 3 will be empty.
- All other merge rules (HQEW drop, returned_mpn match, mfr_match flag, chip-list enrichment, sort order, headers) apply unchanged to the present side.
- The `Versuni_chip_stock_availability_check_<YYYYMMDD>.xlsx` filename is unchanged in partial modes; the folder name (`Merge_<api_ts>__none/` or `Merge_none__<scr_ts>/`) is what signals the partial nature.

## Out of scope (v1)

- FX conversion / unified-currency column.
- Per-chip "best deal" aggregation across sources.
- Trend / delta vs previous batch.
- README auto-status block.
- Aggregating chip-list G–K across MPN dups (e.g. summing `Quantity` across projects) — first-row only by design.

## Known data quirks

- **LCSC scraper sometimes returns `status=ok` with empty `stockpool_qty`.** The scraper reached the product page but didn't parse a number. Merge treats this correctly as `one_side_null` against API; never lands on Sheet 3.
- **API DigiKey emits 3–5 rows per MPN** (US warehouse + Factory + per-SKU variants). Sheet 1 shows them adjacent.
- **API LCSC emits 2 rows per MPN** (广东仓 + 江苏仓). Either or both can be in stock.
- **Scraper LCSC / DIGIKEY emit 1 row per MPN with empty `warehouse`** — that's an aggregate, not a per-warehouse view.
- **Chip-list MPN normalization** — chip list has at least one row with leading `\xa0` (non-breaking space). Both sides are normalized with `.strip().replace("\xa0", "")` before matching.
- **High-risk chips with zero coverage** — if a chip-list MPN with `risk=high` doesn't appear anywhere in either batch (no `status=ok` row), it's simply absent from the merged xlsx. Check the run summary line `"no chip-list row"` count and cross-reference the chip list to find these.
