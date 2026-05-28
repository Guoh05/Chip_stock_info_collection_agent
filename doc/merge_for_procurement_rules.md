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
- Chip metadata: `ref/Raw_chip_list_20260520.xlsx` (sheet `Part List Modify`)

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
7. **Chip-list enrichment.** Every output row is joined to the chip list on a normalized MPN key (`str.strip().replace("\xa0", "")`). The chip list has duplicates (one chip across multiple PCBAs); **only the first matching row** is used per MPN. Both A–D (Category / Project / EMS / 12NC_PCBA) and G–K (Quantity / Currency / Current Price / Type / risk) come from that first row. **The join key (v1.10+) is the chip list's `MPN_cleaned` column when present** (an agent-produced column matching the cleaned MPN actually sent to sources, per CLAUDE.md Hard Rule #8); **falls back to `Manufacture Part Number`** for legacy chip lists without the cleaned column.
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

## v1.9 computed columns (Is_orig_manufacture, Is_cheapest, price_rank)

Three columns are computed post-merge (not directly from upstream):

### `Is_orig_manufacture` (bool)

True iff the row's `Warehouse/vender` looks like the manufacturer's own warehouse, computed against `Manufacture`. Three-step fuzzy match:

1. Special-case `"Manufacturer warehouse (X)"` → match `Manufacture` against `X` (the parens content IS the vendor identity).
2. Otherwise strip any trailing `" (...)"` distributor-SKU parenthetical to avoid SKU strings like `DigiKey (497-STM32C011...)` false-positive-ing on Mfr `STM`.
3. On the cleaned warehouse string:
   - Mfr length ≥ 3: substring match either direction. `"Nexperia 安世"` ↔ `"Nexperia"` ✓; `"STMicroelectronics"` ↔ `"STM"` ✓.
   - Mfr length 1–2 (TI, ON, ST, AD): match against runs of ≥2 consecutive uppercase letters in the **original** warehouse string. Filters out lowercase prepositions like `"on"` in `"Mouser (on order)"`; preserves real abbreviations like `"ON"` in `"ON Semiconductor"`.

Validation against the latest 1700-row merge: 10 unique `(Mfr, Warehouse)` true pairs — all legit; no false positives.

### `Is_cheapest` (bool) + `price_rank` (int)

Grouped by `MPN_cleaned_byAgent` (the agent-cleaned canonical MPN — not the raw `Manufacture Part Number`, which may differ across rows of the same chip), post-VAT-strip:

- `Is_cheapest = True` for the row(s) with the **minimum `Unit price w/o VAT (max qty)`** in that MPN group. Ties → all tied-min rows marked True.
- `price_rank`: dense rank (1, 2, 2, 3) ascending by `Unit price w/o VAT (max qty)`.
- Rows with null or 0 price are **excluded** from the comparison → `Is_cheapest = False`, `price_rank = blank`.
- `Unit price w/o VAT (max qty)` (not `(min qty)`) is the comparison key because the max-tier price is the procurement-relevant one.

## v1.12 packaging column

`packaging` (col 18, right after `Is_orig_manufacture`) is populated from the upstream `packaging_option` field on both tracks (API + scraper). Source-native wording is preserved verbatim — no translation:

- DigiKey API: `Tape & Reel (TR)` / `Cut Tape (CT)` / `Digi-Reel®`
- Element14 API: `Cut Tape` / `Full Reel` / `Re-Reel` / `Each`
- Mouser API: `Tape & Reel` / `Cut Tape` (heuristic)
- Arrow API: `Cut Strips` (sparse; mostly blank)
- LCSC scraper: `编带` / `管装` / `散料`
- DigiKey scraper: 中文 (e.g. `散料`, `卷带（TR）`)
- Future scraper: `Tray` / `Reel` / `Tube`
- Rochester scraper: from datasheet `Packaging Type` row
- bom2buy: **per-distributor** — same MPN may show different `packaging` values across its warehouse rows (Tray / Each / Bulk / Tube from different distributors). This is by design — do NOT aggregate.
- HQEW / ONEYAC / ICKEY / RSONLINE / LCSC API: blank (source doesn't expose).

Sheet 3 (`ref_scraper_api_diff`) does **not** carry this column — same precedent as v1.9 computed cols (extension columns stay Sheet-2-only).

## VAT-strip rule

For every row where `Trade Currency` ∈ {CNY, RMB, ¥} (case-insensitive), **both** unit-price columns are divided by `1.13` (Chinese VAT rate) and rounded to 6 decimals. Other currencies are left untouched. The constant `VAT_DIVISOR = 1.13` lives in the script; change it there if the rate changes.

This is why the columns are named `Unit price w/o VAT (...)` — for CNY rows they're already pre-VAT; for USD/EUR/JPY they're the as-quoted price.

## Output workbook

| Sheet name | Visibility | Filter | Highlight |
|---|---|---|---|
| `High_risk_positive_stock` | **hidden** | `risk == "high"` AND `Available Quantity > 0` | **None** — every row is in-stock by definition |
| `All_data` | **visible, default** | All merged rows (post-filter) | Green (`FFC6EFCE`) if `in_stock`; light grey (`FFEEEEEE`) if `Available Quantity == 0`; no fill for `None`-qty (factory lead) |
| `ref_scraper_api_diff` | **hidden** | Mismatched scraper rows only (see above) | None |
| `Data dictionary` | visible | One row per `All_data` column: `Column` + `Type` + `Description` (descriptions sourced from `ref/merged_output_fields_mapping_v5_20260527.xlsx`) | None |
| `Source Availability` | visible | Port of the TL;DR table in `doc/data_sources_overview.md` (sans `Best use`). `Scraper 可靠性` column is hidden by default. | None |

`Data dictionary` and `Source Availability` are reference sheets — content is hardcoded in the merge script (low churn, small data; auto-sync would over-engineer).

**Unified sort across the three data sheets**: `risk` (`high` → `low` → other → null) → `Manufacture Part Number` (asc) → `Broker name` (asc) → `Available Quantity` (desc). Encoded by `_sort_key()` in the merge script.

Shared styling:
- Header row: bold + `wrap_text`, colored by **column range** (v1.12 layout, 43 cols):
  - **A–L** (cols 1–12, chip-list metadata + raw MPN + cleaned MPN + Manufacture..risk): dark blue `#1F4E78` background + white font.
  - **M–AH** (cols 13–34, in_stock + distributor data + computed cols + packaging + 3 business-fill cols): light orange `#FCE4D6` background + black font.
  - **AI onwards** (cols 35+, `ref_*` audit fields): dark grey `#595959` background + white font.
- **v1.11 header highlight (overrides the zone palette above by column NAME)**: the following 8 procurement-key columns get **dark red `#C00000` + white font** — these are the columns procurement uses most when scanning the workbook:
  - `in_stock`
  - `Broker name`
  - `Warehouse/vender`
  - `Is_orig_manufacture`
  - `Is_cheapest`
  - `Available Quantity`
  - `ship infor after order placed`
  - `Unit price w/o VAT (max qty)`

  Encoded in `HIGHLIGHT_HEADER_COLUMNS` (set, by column NAME so it survives reorderings). Applies to all 3 data sheets that carry those columns (Sheet 3 only matches 5 of the 8).
- Freeze panes at A2.
- AutoFilter dropdowns on the full data range (`A1:<last_col><last_row>`) — procurement can filter in Excel.
- Column widths: hardcoded in `COLUMN_WIDTHS`; columns not listed fall back to auto-fit (capped 10–50 chars).

### Hidden columns (every sheet that carries them)

Default-hidden by `_apply_column_hides()` — can be unhidden manually in Excel:

- All `ref_*` columns (9 cols)
- `Minimum order qty`
- `Unit price w/o VAT (min qty)`
- `Number of price tiers`
- `price_rank`

That's **13 hidden** out of 43 total in Sheet 2 → **30 visible** by default.

### Column list (Sheet 1 & Sheet 2 — 43 cols, in order)

```
Category, Project, EMS/Finish Goods, 12NC_PCBA,
Manufacture Part Number, MPN_cleaned_byAgent,          ← v1.10: raw chip-list MPN preserved + agent-cleaned MPN as new traceability col (visible by default)
Manufacture,
Quantity, Currency, Current Price, Type, risk,
in_stock,                                              ← v1.9 A (moved right after risk)
Broker name, Data collect method,
Warehouse/vender, Is_orig_manufacture,                 ← v1.9 E (new computed col)
packaging,                                             ← v1.12 (new, after Is_orig_manufacture; from upstream packaging_option, source-native wording, visible by default)
Is_cheapest, price_rank,                               ← v1.9 F (new computed cols; price_rank hidden)
Stock Location, Available Quantity,
ship infor after order placed, Lead Time (Week),
MOQ, Maximum order qty, Unit price w/o VAT (max qty),  ← v1.9 D (max-qty pair before min) + B (rename)
Minimum order qty, Unit price w/o VAT (min qty),       ← min-qty pair hidden by default
Number of price tiers,                                 ← hidden by default
Trade Currency,                                        ← v1.9 (no literal \n)
Date of Code, Reel/Cut Reel, Certificate of Conformity(Yes/No),
ref_Warehouse/vender ID, ref_returned_mpn, ref_vendor_sku,
ref_returned_mfr, ref_mfr_match, ref_is_mirror, ref_datasheet_url,
ref_status, ref_error                                  ← all 9 ref_* hidden by default
```

**v1.10 — MPN dual-column convention** (固化于此，每次 merge 必须遵守):

- `Manufacture Part Number` shows the **raw MPN** as it appears in the chip list (preserves package suffix, parens, Chinese descriptors, etc.). For rows without a chip-list match, falls back to the agent-cleaned MPN (so the cell is never blank).
- `MPN_cleaned_byAgent` shows the **agent-cleaned MPN** actually sent to API/Scraper sources and used as the chip-list join key. Always equal to upstream `input_mpn`.
- The two columns let procurement see at a glance: "list says X, agent searched Y, got Y's data."
- All per-MPN aggregations (sort, group-by for `Is_cheapest`/`price_rank`, summary counts) use `MPN_cleaned_byAgent` — the raw col is for display only.
- Chip-list join uses the chip list's `MPN_cleaned` column when present; falls back to `Manufacture Part Number` for legacy chip lists.

Header → source mapping is defined by `COLUMN_SOURCE_MAP` in the script. Each entry is one of:

- `("merge", <csv_field>)` — copy from upstream CSV row (rename only).
- `("chip",  <chip_list_header>)` — first-row chip-list lookup.
- `("lead_time", "lead_time_days")` — days-to-weeks transform.
- `("computed", None)` — filled by post-processing pass (Is_orig_manufacture, Is_cheapest, price_rank).
- `("blank", None)` — empty placeholder (procurement fills in later).

Three "blank" columns (`Date of Code`, `Reel/Cut Reel`, `Certificate of Conformity(Yes/No)`) are written empty for procurement to fill.

### Sheet 3 columns (24 cols, in order)

```
Category, Project, EMS/Finish Goods, 12NC_PCBA,
Manufacture Part Number, MPN_cleaned_byAgent, Manufacture,    ← v1.10: dual-MPN convention applies to Sheet 3 too
Quantity, Currency, Current Price, Type, risk,
Broker name, in_stock,
ref_returned_mpn, Warehouse/vender, Available Quantity,
ship infor after order placed, Lead Time (Week),
MOQ, Unit price w/o VAT (min qty), Trade Currency,
ref_datasheet_url, note
```

Sheet 3 inherits the price-column rename + VAT-strip from v1.9 B+C but **does not** carry the three new computed columns (`Is_orig_manufacture` / `Is_cheapest` / `price_rank`) — those are meaningless on a QA-only subset where rows are not per-MPN-comparable.

The `note` column is generated by the merge (`"API max qty X vs scraper max Y"`).

### Dropped from upstream

- `run_subdir` — marked DELETE in `ref/merged_output_fields_mapping.xlsx`.
- Upstream scraper-only `elapsed_sec`, `num_variants` — not in mapping.

## Currency

Two currency-shaped columns exist and **mean different things**:

| Column | Source | Meaning |
|---|---|---|
| `Currency` | chip list col H | the buying currency the EMS expects to pay in |
| `Trade Currency` | upstream `currency` | the distributor's quoted currency on that row — drives the VAT-strip rule |

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
