# Scraper output verification guidance

This doc is for a Claude Code session whose job is to **manually verify** that the JSON records produced by the scrapers match what the corresponding product screenshots actually show. The goal is to catch:

1. **Fields the scraper missed** (information visible in the screenshot but absent / null in the JSON).
2. **Fields the scraper parsed wrong** (JSON has a value, but it disagrees with what the screenshot shows).
3. **Wrong-variant matches** (JSON describes a different MPN than the input — screenshot may already show this).

It is **not** for fixing the scrapers — only for producing a verification report that a human (or a follow-up coding session) can use to decide what to fix.

## Inputs

A batch folder, e.g.

```
test/scraper/BatchTest_20260518_19_58_04/
```

Inside it there is one subfolder per `(MPN × channel)` cell, e.g. `Test_BT168GW_115_LCSC/`. The user will tell you which batch to verify.

## What you are comparing — clarify before you start

| Artifact | Role |
|---|---|
| `<MPN>.json` (canonical record) | **Subject of verification.** This is what downstream code consumes. The `json_value` column in your report must come from here, verbatim. |
| `<MPN>_product.png` | **Ground truth.** What the website actually showed. The `screenshot_shows` column comes from here. |
| `<MPN>_summary.md` | **Reading aid only.** A 1–3 KB digest of the JSON. Use it to quickly see which JSON fields were populated and what the scraper labels them; it saves tokens vs. parsing raw JSON every time. **Never substitute its rendered value for the JSON's actual value.** If summary.md and the JSON disagree, that's its own bug — flag it as a separate row with `field = "<summary.md> vs <json>"`. |

In short: **JSON ↔ screenshot is the comparison. summary.md is the index that tells you what to look at.**

## Per-cell folder layout — three patterns

You will see three different shapes; handle each:

### Pattern A — flat (single-listing channels)

```
Test_BT168GW_115_ONEYAC/
├── BT168GW_115.json              ← canonical record
├── BT168GW_115_product.png       ← screenshot to compare against
├── BT168GW_115_product.html
├── BT168GW_115_search.png        ← ignore (search results page)
└── BT168GW_115_summary.md
```

The JSON and screenshot are in the cell root. Compare them directly.

### Pattern B — one nested level (LCSC v3, some Future / HQEW runs)

```
Test_BT168GW_115_LCSC/
└── BT168GW_115/
    ├── BT168GW_115.json
    ├── BT168GW_115_product.png
    └── ...
```

Descend one level. The inner folder name is the (sanitised) MPN.

### Pattern C — multiple variant subfolders (Future / LCSC multi-variant)

```
Test_ESP32-WROOM-32E-N4_FUTURE/
├── ESP32-WROOM-32E-N4/         ← input MPN
├── ESP32-WROOM-32E-N8/         ← sibling variant (8 MB flash)
├── ESP32-S3-WROOM-1-N8R8/      ← unrelated S3 variant
├── ESP32-D0WD-V3/              ← bare SoC variant
└── ...   (up to 7 subfolders)
```

This is the tricky case. The scraper saved one product page **per variant**, but downstream (`batch_index.csv`) only consumes **one** of them — the variant chosen by `pick_best_extracted`. **Verify only that chosen variant**, not all 7.

#### How to find the chosen variant

Read `batch_index.csv` in the batch root. For the matching `(input_mpn, channel)` row:

- `returned_mpn` column → the MPN of the chosen variant (e.g. `ESP32-D0WD-V3`).
- `run_subdir` column → the cell folder path.

The chosen variant's subfolder is the one whose name (after `_safe_folder` sanitisation) equals `returned_mpn`. Open that subfolder's `*_product.png` and `*.json` and verify those.

#### Two findings to record explicitly for Pattern C cells

1. **The chosen variant's data accuracy** — same field-by-field verification as Pattern A/B (compare `<chosen>/<chosen>.json` against `<chosen>/<chosen>_product.png`).
2. **Whether the chosen variant matches the input MPN** — if `returned_mpn ≠ input_mpn` (after normalisation), add a single dedicated row at the top of the cell's section:

| cell | field | json_value | screenshot_shows | verdict | disagreement |
|---|---|---|---|---|---|
| Test_ESP32-WROOM-32E-N4_FUTURE/ESP32-D0WD-V3 | _pick_best_variant_choice | ESP32-D0WD-V3 | n/a (this is a picker issue, not a screenshot one) | json_wrong | Picker chose ESP32-D0WD-V3, input MPN was ESP32-WROOM-32E-N4. Wrong-variant fallback bug. |

This single-row pattern flags the picker problem without dragging the verifier into reviewing 7 variant screenshots.

### Decide which file to use

```
for each Test_<MPN>_<CHANNEL>/ folder:
    if outer folder has *_product.png at root: Pattern A — use it.
    else:
        subs = subfolders containing a *_product.png
        if no subs: skip (record as "no product.png — skipped")
        if 1 sub: Pattern B — use it.
        if >1 subs: Pattern C — look up returned_mpn / run_subdir in batch_index.csv
                    and verify the matching variant only.
                    If returned_mpn ≠ input_mpn, also emit the _pick_best_variant_choice row.
```

### The `cell` column for multi-variant cases

For Pattern A and B the `cell` column is just the outer folder name: `Test_BT168GW_115_LCSC`.

For Pattern C the `cell` column is the **outer folder + chosen variant subfolder**, slash-separated:

- `Test_ESP32-WROOM-32E-N4_FUTURE/ESP32-D0WD-V3`
- `Test_LTV817B-V-G_LCSC/MMBT3904LT1G`

This way every row in the report is unambiguous about which on-disk artifact it refers to.

## Fields to verify (the canonical-schema essentials)

For each chosen JSON, compare these fields against what is visible in `*_product.png`:

| Field path in JSON | What it represents | Where to look in the screenshot |
|---|---|---|
| `extracted.manufacturer_part_number` | The MPN the site is listing | Big title near top of page |
| `extracted.manufacturer` | Brand / mfr name | Title line, "制造商:" / "Manufacturer:" row, or breadcrumb |
| `extracted.stock_now_qty` | Now-available quantity | "库存" / "In Stock" / "现货" + number near the price box |
| `extracted.stock_now_ship_text` | Free-text describing how / when the now-stock ships | The line near the stock number ("RS 欧时仓现货", "5–7 天", "Ships from another location"…) |
| `extracted.stock_future_qty` | Future/incoming batch quantity | "在途", "另外 N 件将于…发货", future-ship promise rows |
| `extracted.stock_future_ship_text` | Date / wording of the future batch | Same row |
| `extracted.prices` (array) | Tier-price ladder | Quantity-vs-unit-price table |
| `extracted.unit_price_cny` / `_usd` | Headline single-quantity price | The big bold price on the page |
| `extracted.min_order_qty` | Minimum order quantity / 起订量 | "MOQ", "起订量", "Min Qty" |
| `extracted.package` | Package code | "封装", "Package", "Case" |
| `extracted.lifecycle_status` | Active / EOL / NRND etc. | Status badge or "Product Life Cycle" row |
| `extracted.datasheet_url` | PDF link | "Datasheet" / "数据手册" button — only check **whether a link exists**, don't try to verify URL contents |
| `extracted.delivery_time` / `delivery_location` | Estimated delivery | ICKEY's "货期：内地 N–M 工作日" row |
| `extracted.parameters` | Spec table | Right-side or below-fold "参数 / Specifications" table |

**Fields you cannot see in the screenshot** (e.g. JSON-LD-derived flags, internal IDs, parsed `site_*` mirror fields): do **not** mark these wrong just because they don't appear on screen. Note them as `not visible in screenshot` and move on.

## How to record a verification

For each cell, write **one row per field you can evaluate**. Don't write rows for fields you can't see.

| Column | Meaning | Allowed values |
|---|---|---|
| `cell` | Folder name, e.g. `Test_BT168GW_115_LCSC` | string |
| `field` | JSON path, e.g. `extracted.stock_now_qty` | string |
| `json_value` | What the JSON says | string (truncate to ~80 chars) |
| `screenshot_shows` | What you see in the screenshot for the same field | string |
| `verdict` | One of: `match` / `json_missing` / `json_wrong` / `screenshot_unclear` | enum |
| `disagreement` | **One short sentence explaining what doesn't match.** REQUIRED whenever `verdict ≠ match`. Leave blank for `match` rows. The human reads this column to decide whether to re-open the screenshot, so write it tightly: subject + the actual mismatch, no filler. | string |
| `note` | Anything else (e.g. "popup blocked price area", "screenshot truncated at row 12") | optional |

### Example `disagreement` sentences

- `json_wrong` — `JSON says stock=0, screenshot shows "现货 8,000".`
- `json_missing` — `Screenshot shows MOQ=650 next to 起订量, JSON.min_order_qty is null.`
- `screenshot_unclear` — `Privacy popup covers the price block.`
- `screenshot_unclear` — `Screenshot truncates at the params table — stock area not visible.`
- `json_wrong` — `JSON manufacturer="UMW", screenshot title clearly says "STMicroelectronics".`

The rule: a human glancing only at the `disagreement` column should be able to decide "yes that's a real bug" or "no, the verifier misread the screenshot" without re-opening the PNG.

`verdict` definitions — **be conservative**:

- `match` — JSON value clearly equals what's on screen (within reasonable tolerance: 12,105 vs 12105 is a match; "STM" vs "STMicroelectronics" is a match).
- `json_missing` — Screenshot clearly shows the value; JSON is `null` / `""` / absent.
- `json_wrong` — Both are populated but they **clearly** disagree (e.g. screenshot says stock=8000, JSON says stock=0).
- `screenshot_unclear` — Screenshot is truncated, behind a popup, blurry, Chinese characters mojibake'd, or the relevant area is not visible. **Use this generously rather than guessing.**

### Never guess

If the popup blocks the price area, mark `screenshot_unclear`. Do not infer the price from anywhere else. If the JSON says 5000 stock and the screenshot shows "请咨询" with no number, mark `screenshot_unclear` for that field — not `json_wrong`.

This matches the project-wide principle: **when the source data is ambiguous, leave the verdict blank rather than fabricating one.**

## Output

Write **one markdown file** and **one xlsx** in the batch folder root:

```
test/scraper/BatchTest_<ts>/
├── VERIFICATION_REPORT.md
└── VERIFICATION_REPORT.xlsx
```

### `VERIFICATION_REPORT.md` structure

```markdown
# Verification report — BatchTest_<ts>

Generated 2026-MM-DD HH:MM by Claude Code.
Verified <N> cells out of <total>. Skipped <K> cells without product.png.

## Per-cell verdict summary

| cell | n_match | n_json_missing | n_json_wrong | n_unclear | overall |
|---|---|---|---|---|---|
| Test_BT168GW_115_LCSC | 6 | 1 | 0 | 0 | mostly ok |
| Test_ESP32-WROOM-32E-N4_ONEYAC | 3 | 2 | 1 | 1 | needs review |
| ...

## Discrepancy details (only rows with verdict ≠ match)

| cell | field | json_value | screenshot_shows | verdict | disagreement | note |
|---|---|---|---|---|---|---|
| ... |

## Possible root causes and fix suggestions

(One bullet per recurring problem pattern, not per row. Examples:
- "ONEYAC `prices` consistently empty for chips with stock > 0 → likely `detailPri` parser miss for X layout. Look at scrape_oneyac.py:182.")

## Skipped cells

| cell | reason |
|---|---|
| ... | no product.png present |
```

### `VERIFICATION_REPORT.xlsx`

Single sheet, same columns as the "Discrepancy details" table plus all the matching rows. This lets the user filter by verdict in Excel.

## Working method — handling many cells without blowing context

The batch has up to 64 cells. Reading 64 full-page PNGs sequentially will run out of context fast. Do this:

1. **First pass — enumerate.** List every cell folder, classify it as Pattern A/B/C, find its product.png (or note absence). Write a `_verification_plan.md` (in the batch dir) with one row per cell and the resolved path. **Don't open any images yet.**

2. **First pass — pre-filter.** Read `batch_index.csv` (in the batch dir) and **skip cells whose status is `no_results` / `blocked` / `failed` / `timeout`** — they have no `extracted` data to verify against. Record them in the plan as "skipped — no data to verify". This typically eliminates 30–50 % of cells before any image is opened.

3. **Second pass — verify 2–3 cells per batch.** Strict cap. After each batch, **append** the new rows to `VERIFICATION_REPORT.md` on disk. Do not hold them in conversation memory. Use the `Edit` tool to append; don't rewrite the whole file each time.

4. **Track progress.** After each batch, write a one-line `[checkpoint]` to `_verification_plan.md`: "completed cells 1–3 at 12:34". This way if you crash or run out of context, the next session can resume by reading the last checkpoint.

5. **One image per Read call.** Don't `Read` more than one image in the same tool-call block — they're big. Process the image, write the rows, then Read the next one.

6. **Skip aggressively at the cell level.** If a cell's product.png is < 50 KB, it's almost certainly a near-blank "404 / not found" page — record it as `screenshot_unclear` for all fields and move on without straining over individual fields.

7. **At the end, write the xlsx.** Use `.venv/Scripts/python.exe` (the project venv) with `openpyxl` (already installed) to convert the markdown table to xlsx.

## Token-cost mitigations — this verification is expensive

A naïve run of "read 64 full-resolution product.pngs and verify every canonical field" will burn tens of millions of tokens. Apply these in order:

### 1. Downscale every PNG before reading it

The Read tool charges tokens proportional to image pixel count. Most product.pngs are 1920×3000+ (full-page screenshots). Downscale to ≤1200 px wide first — the text is still legible for verification but token count drops 5–10×.

Do this once per cell, lazily, when you're about to Read the image:

```bash
.venv/Scripts/python.exe -c "
from PIL import Image
from pathlib import Path
src = Path(r'<path to *_product.png>')
dst = src.with_name(src.stem + '_small.png')
if not dst.exists():
    im = Image.open(src)
    if im.width > 1200:
        ratio = 1200 / im.width
        im = im.resize((1200, int(im.height * ratio)), Image.LANCZOS)
    im.save(dst, optimize=True)
print(dst)
"
```

Then `Read` the `_small.png` instead of the original. The downscaled PNGs are throwaway artifacts; you may delete them at the end (`*_small.png`) or leave them — the next run will reuse them.

### 2. Don't verify cells where there's nothing to compare

Pre-filter via `batch_index.csv`:

- `status` ∈ {`no_results`, `blocked`, `failed`, `timeout`, `exception`} → skip. No JSON data to verify against the screenshot.
- `num_variants = 0` AND `returned_mpn = ""` → skip. Same reason.

Record the skips in the plan but don't open the images.

### 3. Use `*_summary.md` as an index, but copy values FROM the JSON

Every cell has a `*_summary.md` written by `common/_summary.py`. It digests the JSON into a human-readable bullet list of the fields you're verifying. **Read summary.md first to see which fields are populated** — it's ~1–3 KB instead of 5–50 KB of raw JSON.

But the `json_value` column in your report must come from the **JSON file itself**, not from summary.md. Reason: summary.md formats / truncates / pretty-prints. If it disagrees with the JSON, that's its own renderer bug and a separate finding. The JSON is the artifact downstream consumes.

Workflow:
1. Read `*_summary.md` to see which canonical fields the scraper populated.
2. For each populated field, look up the actual value in `*.json` (`Read` the JSON once per cell, find the field via simple text search or `jq` via Bash).
3. Compare that JSON value against the screenshot.

This still saves tokens (summary.md tells you which fields to *bother* checking; un-populated fields can be skipped), without compromising correctness.

### 4. Prioritise high-value cells first

If the batch already has a curated issue list (e.g. `REPORT_8ch_8chips.md` in the same folder), verify those cells **first**. If you run low on context, you've at least covered the ones the previous session flagged.

For "channels that are 100% OK" (e.g. HQEW = 8/8 in the 20260518 batch), spot-check 1–2 cells instead of all 8. For "channels that are 0% OK" (e.g. ROCHESTER = 0/8), there's nothing to verify — they're all no_results.

### 5. One-line `disagreement` keeps the report itself cheap

Don't write paragraphs in the report — short sentences. Long verdicts make the markdown blow up and slow subsequent appends.

### Rough budget

After applying mitigations 1–4 to the `BatchTest_20260518_19_58_04` batch:

- 64 cells total
- ROCHESTER cells (0/8 ok) → all skipped → −8
- Other `status ≠ ok` cells → skipped → ~−15 more
- Pattern C cells: even with 7 variant subfolders, only 1 is verified per cell → no extra images
- Net: ~30 images to read, each downscaled to 1200 px wide
- + summary.md text reads (~1 KB × ~30) → negligible
- + JSON text reads (lookup specific fields, not full read) → negligible
- + report appends (markdown rows) → negligible

Without mitigations the same job is ≈ 5–10× more expensive and may run out of context before finishing.

## What is out of scope

- **Do not modify scraper code.** Your job is to report, not patch. The user (or a follow-up coding session) reads your report and decides what to do.
- **Do not re-run scrapers.** Trust the artifacts on disk. If a cell looks like it was scraped at a bad moment, note it but don't re-scrape.
- **Do not download anything external.** No fetching datasheet PDFs to verify, no opening URLs.
- **Do not aggregate across channels.** Each cell stands on its own. Cross-channel agreement is already in `batch_compare.csv`.
- **Do not edit `batch_index.csv` / `batch_compare.csv` / any of the scraper-emitted artifacts.** They are the source of truth from the scraper's perspective; your report is a separate parallel view.

## Quick worked example — one cell

Folder: `test/scraper/BatchTest_20260518_19_58_04/Test_BT168GW_115_LCSC/`

- Pattern: B (nested subfolder `BT168GW_115/`).
- JSON: `BT168GW_115/BT168GW_115.json` → has `extracted.manufacturer_part_number="BT168GW,115"`, `extracted.stock_now_qty=10505`, `extracted.prices=[…4 tiers…]`.
- Screenshot: `BT168GW_115/BT168GW_115_product.png`.

You open the PNG. You see: title "BT168GW,115", manufacturer "WeEn Semiconductors", stock badge "10,505 现货", price ladder with 4 rows.

You write:

| cell | field | json_value | screenshot_shows | verdict | disagreement |
|---|---|---|---|---|---|
| Test_BT168GW_115_LCSC | extracted.manufacturer_part_number | BT168GW,115 | BT168GW,115 | match | |
| Test_BT168GW_115_LCSC | extracted.manufacturer | WeEn Semiconductors | WeEn Semiconductors | match | |
| Test_BT168GW_115_LCSC | extracted.stock_now_qty | 10505 | 10,505 现货 | match | |
| Test_BT168GW_115_LCSC | extracted.prices | (4 tiers) | 4-row ladder visible | match | |

No discrepancy rows, so this cell contributes nothing to the "Discrepancy details" section, only one row to the per-cell summary.

A cell that did fail might look like:

| cell | field | json_value | screenshot_shows | verdict | disagreement |
|---|---|---|---|---|---|
| Test_LIS2DH12TR_ONEYAC | extracted.stock_now_qty | 0 | 现货 8,000 | json_wrong | JSON says 0, screenshot stock badge clearly shows 8,000. |
| Test_LIS2DH12TR_ONEYAC | extracted.min_order_qty | null | 起订量 100 | json_missing | Screenshot shows 起订量 100 row, JSON.min_order_qty is null. |
| Test_LIS2DH12TR_ONEYAC | extracted.prices | [] | tier table with 3 rows | json_missing | Screenshot shows 3-tier price table; JSON.prices is empty. |

## When in doubt

Lean toward `screenshot_unclear`, not `json_wrong`. A false-positive `json_wrong` makes the human chase a non-bug; a `screenshot_unclear` invites a manual re-check. The asymmetric cost favours under-claiming bugs.
