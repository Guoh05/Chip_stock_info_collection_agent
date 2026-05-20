# Distributor API Test Report v2 — 5 distributors, warehouse-granular batch

**Date:** 2026-05-19 (supersedes `api_report_v1.md` from 2026-05-17, which only covered Mouser + Digikey)
**Scope:** 5 working distributor APIs + batch driver + per-warehouse output schema
**Stack:** Python 3.10.9, `requests`, `python-dotenv`, `openpyxl`, `concurrent.futures` (thread pool). No browser, no TLS impersonation.

---

## TL;DR

| # | Source (display name) | Endpoint | Auth | OK% on 103-chip sweep | Notes |
|---|---|---|---|---|---|
| 1 | **Mouser_贸泽** Search API v1 | `POST api.mouser.com/api/v1/search/partnumber` (+ `/search/keyword` fallback) | API key in querystring | 61.2 % | API key registered on .cn → returns zh-CN locale + RMB |
| 2 | **DIGIKEY_得捷电子** Product Information API v4 | `POST api.digikey.com/products/v4/search/keyword` | OAuth2 client_credentials → bearer | 58.3 % | Token cached in-process for 599 s |
| 3 | **ELEMENT14_e络盟** Catalog Search | `GET api.element14.com/catalog/products` | API key in querystring | 41.7 % | Quota: 2 req/s, 1000/day. Store ID `cn.element14.com`. `manuPartNum:<MPN>` term |
| 4 | **ARROW_艾睿** Pricing & Availability v4 | `GET api.arrow.com/itemservice/v4/en/search/list` | `login` + `apikey` querystring (+ same pair in `req` JSON) | 40.8 % | Inventory republished across Verical / Arrow ACNA / Arrow EUROPE — dedup by `(fohQty, shipsFrom, shipsIn)` |
| 5 | **LCSC_立创商城** Mall OpenAPI | `POST open-api.jlc.com/lcsc/openapi/product/search/global` | HMAC-SHA256 + `Authorization: JOP …` | 74.5 % (41/55 — quota-limited subset) | Quota: **200/day per endpoint**. Daily cap hit during the 103-chip sweep |

Latest full sweep: **`test/api_test/BatchTest_20260519_17_54_29/`** — 103 chips × 5 sources, 5.36 min wall clock (4-source parallelism per chip), 980 warehouse rows in `batch_index`. LCSC contributed 55 of the 103 chips (other 48 hit the 200/day quota; their rows were filtered out — re-run after quota reset to fill in).

---

## Output schema (warehouse-granular since 2026-05-18)

The data contract is one row per **(input_mpn × source × warehouse)**. Full column reference: `api/doc/batch_output_schema.md`. Five-line minimum contract per per-variant record (carried in the per-MPN JSON's `extracted` field):

| Field | Meaning |
|---|---|
| `stock_now_qty` | 现货 quantity — distributor's own warehouse |
| `stock_now_ship_text` | 发货时间 string for 现货 |
| `stock_future_qty` | 期货/在途 quantity (`null` for unbounded factory order) |
| `stock_future_ship_text` | Lead-time string for the future pool |
| `stock_breakdown` | `[{label, warehouse, quantity, ship_text, [moq], [note]}, …]` — **explosion source for batch_index** |

Site-native fields are preserved verbatim under `site_*` keys per the "site-native wording" rule. The canonical scalars (`stock_now_*` / `stock_future_*`) are an interpretation layer above them.

**MPN-variant grouping rule** stays the same: a search returning multiple distinct `manufacturer_part_number` strings → one per-variant subfolder; never aggregate across MPN strings.

## Folder layout (per chip × source)

```
test/api_test/Test_<MPN>_<SOURCE>_<YYYYMMDD>_<HH>_<MM>_<SS>/    # single-call shape
└── (inside a BatchTest_<ts>/)                                  # batch driver writes here
    Test_<MPN>_<SOURCE>/                                        # no inner timestamp inside a batch
    ├── parent_summary.md                                       # Markdown overview + variant index
    ├── <MPN>.json                                              # parent run record + variants_summary[]
    ├── raw_response.json                                       # full API payload (for audit)
    └── <variant_mpn>/                                          # one per distinct returned MPN string
        ├── <variant_mpn>.json                                  # normalized canonical-schema record
        ├── <variant_mpn>_raw_part.json (Mouser/Arrow)
        │ / _raw_product.json (Digikey/Element14)
        │ / _raw_product.json (LCSC)
        └── <variant_mpn>_summary.md                            # rendered by common/_summary.py
```

Identical shape across all 5 sources by design — `common/_summary.py` is single-source-agnostic, and a buyer reading `<MPN>_summary.md` shouldn't be able to tell which API produced it.

---

## 1. Mouser_贸泽 Search API v1 — working

**Script:** `api/scripts/api_mouser.py`
**Base URL:** `https://api.mouser.com/api/v1`
**Auth:** API key in querystring (`?apiKey=<KEY>`). Free; register at https://www.mouser.com/api-hub/.

**Primary endpoint:** `POST /search/partnumber` with body
```json
{"SearchByPartRequest": {"mouserPartNumber": "<MPN>", "partSearchOptions": "Exact"}}
```
**Fallback endpoint:** `POST /search/keyword` (used if partnumber search returns 0 results).

### Response essentials

Each item in `SearchResults.Parts[]` carries:
- Identity: `ManufacturerPartNumber`, `MouserPartNumber`, `Manufacturer`, `Description`, `Category`, `LifecycleStatus`, `ROHSStatus`, `ProductDetailUrl`, `DataSheetUrl`, `ImagePath`.
- Stock: `Availability` (zh-CN display string like `"108590 库存量"`), `AvailabilityInStock` (numeric string), `FactoryStock` (numeric string), `AvailabilityOnOrder[]` (`{Quantity, Date}` pending PO batches), `LeadTime`.
- Ordering: `Min`, `Mult`, `PriceBreaks[].{Quantity, Price, Currency}`.
- Specs: `ProductAttributes[].{AttributeName, AttributeValue}`.
- Compliance: `ProductCompliance[]` (HTS / ECCN / TARIC).

### Canonical mapping

| Canonical | Mouser source |
|---|---|
| `stock_now_qty` | `AvailabilityInStock` — fallback: leading int from `Availability` |
| `stock_future_qty` | `FactoryStock` if > 0; otherwise `null` (unbounded factory order) when `LeadTime` is set |
| `stock_breakdown` rows | `Availability` → in-stock row; `FactoryStock`+`LeadTime` → factory row; each `AvailabilityOnOrder[]` entry → its own `OnOrder` row with expected arrival date |

### Gotchas

1. **API key is locale-bound.** This key is registered on Mouser China → every response is localized: `Availability` is `"<N> 库存量"`, parameter names are Chinese (`封装`, `标准包装数量`), currency is `RMB`, `LeadTime` is in **days** suffixed `"天数"` (NOT weeks). For "lead time in weeks" output we'd need a US-registered key.
2. **`Availability` mixes value + unit** — parse leading integer.
3. **`AvailabilityOnOrder` is real signal.** For chips with 0 现货 but committed PO batches arriving in 1-3 months, this is the only way to surface that — kept as its own row in `stock_breakdown`.
4. **`partSearchOptions: "Exact"` is intolerant of separators.** MPNs with commas/slashes can fall through to 0 — the keyword fallback covers those.
5. Free tier 1000 calls/day; no per-second throttle observed.

---

## 2. DIGIKEY_得捷电子 Product Information API v4 — working

**Script:** `api/scripts/api_digikey.py`
**Base URL:** `https://api.digikey.com`
**Auth:** OAuth2 `client_credentials` flow. Register at https://developer.digikey.com/ → create Production app → get `DIGIKEY_CLIENT_ID` + `DIGIKEY_CLIENT_SECRET`.

**Token endpoint:** `POST /v1/oauth2/token` (form body) → `{access_token, expires_in: 599, token_type: "Bearer"}`. **Token cache** is implemented module-level in `fetch_token()` keyed by `client_id`, with 30 s refresh margin. 103 chips → 1 token fetch + 102 cached lookups per batch.

**Search endpoint:** `POST /products/v4/search/keyword` with headers `Authorization: Bearer <token>`, `X-DIGIKEY-Client-Id`, `X-DIGIKEY-Locale-Site: US`, `X-DIGIKEY-Locale-Language: en`, `X-DIGIKEY-Locale-Currency: USD`, `X-DIGIKEY-Customer-Id: 0`. Body: `{"Keywords": "<MPN>", "Limit": 50, "Offset": 0}`.

### Response essentials

Two parallel arrays — `ExactMatches[]` and `Products[]`. Deduped by `ManufacturerProductNumber`, prefer exact-match order.

Each Product:
- Identity: `ManufacturerProductNumber`, `Manufacturer.Name`, `Description.*`, `Category.Name`, `DatasheetUrl`, `PhotoUrl`, `ProductUrl`, `ProductStatus.Status`.
- Stock: `QuantityAvailable` (DK warehouse total), `ManufacturerLeadWeeks` (bare integer — **assumed weeks**), `NormallyStocking`, `BackOrderNotAllowed`, `NonStock`, `Discontinued`, `EndOfLife`, `DateLastBuyChance`.
- Variations: `ProductVariations[]` — one per packaging form. Each has `DigiKeyProductNumber`, `PackageType.Name` (Tube / Tape & Reel / Cut Tape / Digi-Reel®), `QuantityAvailableforPackageType`, `StandardPricing[].{BreakQuantity, UnitPrice, TotalPrice}`, `MinimumOrderQuantity`, `StandardPackage`.
- Specs: `Parameters[].{ParameterText, ValueText}`.
- Classifications (v4 names): `Classifications.HtsusCode`, `Classifications.ExportControlClassNumber`, `Classifications.RohsStatus`, `Classifications.ReachStatus`, `Classifications.MoistureSensitivityLevel`. **Not** `HtsCode`/`EccnCode` (those were v3).

### Canonical mapping

| Canonical | Digikey source |
|---|---|
| `stock_now_qty` | `QuantityAvailable` |
| `stock_future_qty` | `null` (unbounded factory order) when `NormallyStocking` is true or `ManufacturerLeadWeeks` is set |
| `stock_breakdown` rows | One `QuantityAvailable` warehouse row, one `Factory Stock (ManufacturerLeadWeeks)` row, plus one `Packaging — <PackageType>` row per `ProductVariations[]` entry |

### Gotchas

1. **`ManufacturerLeadWeeks` is a bare integer** — convention is weeks, verified against `digikey.cn`'s rendering.
2. **Classification keys differ from v3.** Use `HtsusCode` / `ExportControlClassNumber` (not `HtsCode` / `EccnCode`).
3. **Keyword search returns noise** — `STM32G030F6P6` returned 4 unrelated SKUs alongside the actual MPN. Kept as separate variants per the MPN-variant rule; the batch driver's best-variant picker (exact MPN > highest stock) filters at batch-index time.
4. **`BackOrderNotAllowed` is the **inverted** name** — `false` means back-order IS allowed. Read carefully.
5. Locale picks currency. `USD` for cross-batch consistency.

---

## 3. ELEMENT14_e络盟 Catalog Search API — working

**Script:** `api/scripts/api_element14.py`
**Base URL:** `https://api.element14.com/catalog/products`
**Auth:** API key in querystring (`callInfo.apiKey=<KEY>`).
**Quota:** **2 req/s, 1000/day** (per the user's plan). The batch driver enforces a minimum 0.6 s gap between successive Element14 calls (configurable via `ELEMENT14_CALLS_PER_SECOND` or legacy `Calls_per_second_limit` env vars).

**Primary:** `GET /catalog/products?term=manuPartNum:<MPN>&storeInfo.id=cn.element14.com&resultsSettings.responseGroup=large&callInfo.responseDataFormat=json&callInfo.apiKey=<KEY>`
**Fallback:** Same with `term=any:<MPN>` (keyword search).

### Response essentials

Payload root key depends on the term type — one of `keywordSearchReturn`, `manufacturerPartNumberSearchReturn`, `premierFarnellPartNumberReturn`. Each carries `products[]` with:
- Identity: `manufacturerPartNumber` / `translatedManufacturerPartNumber`, `vendorName` / `translatedManufacturer`, `sku`, `displayName`, `productOverviewUrl`, `productStatus`, `rohsStatusCode`.
- Stock: `stock.level` (total across regions), `stock.leastLeadTime` (shortest lead time in **days** — verified against actual values like 218 for STM32F030; weeks would be implausible), `stock.regionalBreakdown[]` (per-region totals with their own `leastLeadTime`).
- Prices: `prices[].{from, cost, to}`. No currency in payload — inferred from `storeInfo.id` (`cn.element14.com` → CNY, `uk.farnell.com` → GBP, etc.).
- Specs: `attributes[].{attributeLabel, attributeValue}`.
- Datasheets: `datasheets[].{url, type, language}` (pick English PDF if present).

### Canonical mapping

| Canonical | Element14 source |
|---|---|
| `stock_now_qty` | `stock.level` |
| `stock_future_qty` | `null` (unbounded) when `stock.leastLeadTime` is set |
| `stock_future_ship_text` | `"原厂标准交货期 <N> 天 (Element14 leastLeadTime, days)"` |
| `stock_breakdown` rows | `"Stock level (total)"` aggregate row (warehouse `Element14 (cn.element14.com)`) + one per `stock.regionalBreakdown[]` entry + factory-lead row |

### Gotchas

1. **Three docs-trap params.** Field is `manuPartNum` (NOT `manuPartNumber` with -er). Store ID is `cn.element14.com` (NOT `cn.farnell.com` — that hostname doesn't exist on the API). `versionNumber` is NOT a valid query param — including it makes the upstream return a generic 400.
2. **`leastLeadTime` is in days, not weeks.** Verified empirically (218 for STM32F030 = 31 weeks, not 218 weeks). Documented unit ambiguity in the doc itself — we name the field `site_lead_time_days` explicitly.
3. **Aggregate row** (`"Stock level (total)"`) is kept in `stock_breakdown` as `warehouse="Element14 (cn.element14.com)"` because it carries the buyer-facing total + canonical ship SLA (`"e络盟 在库,下单后立即发货"`). The per-region rows (`Element14 / UK`, etc.) are the tactical breakdown. **Naive `SUM(stockpool_qty)` will double-count** — dedup by `warehouse LIKE 'Element14 / %'` to use regions only, or `warehouse = 'Element14 (cn.element14.com)'` to use the total.

---

## 4. ARROW_艾睿 Pricing & Availability v4 — working

**Script:** `api/scripts/api_arrow.py`
**Base URL:** `https://api.arrow.com/itemservice/v4`
**Auth:** **Both** `login` AND `apikey` required, **in querystring AND nested in the `req` JSON payload**:
```
GET /itemservice/v4/en/search/list?login=<LOGIN>&apikey=<KEY>&req=<URL-encoded-JSON>
```
where `<JSON>` includes a duplicate `login` + `apikey` inside the body.

### Response essentials

Each part record:
- Identity: `mfrPart`, `mfr.mfrName` / `mfr.mfrCd`, `itemId`, `partDescription`, `productCategory`, `productStatus`.
- **Distribution sources** (the richest data): `webSites[].sources[].sourceParts[]` — Arrow republishes the same physical inventory across `Verical.com` (Verical), `arrow.com` (Arrow Americas ACNA), and `arrow.com` (Arrow EUROPE). Each `sourcePart` carries:
  - `fohQty` (qty), `shipsFrom` (country), `shipsIn` (SLA string), `mfrLeadTime` (days), `moq`, `packSize`, `dateCode`, `eccnCode`, `htsCode`, `countryOfOrigin`.
  - Per-source `tiers[]` with `minQuantity`, `unitPrice`, `currency`. **Currency varies per warehouse** (USD / EUR / JPY).
  - `pipeline[]` — committed future shipments (qty + ETA).
- Misc: `partImage`, `datasheetUrl`.

### Canonical mapping

| Canonical | Arrow source |
|---|---|
| `stock_now_qty` | sum of `fohQty` across `sources_flat[]` **after mirror dedup** by `(fohQty, shipsFrom, shipsIn)` |
| `stock_future_qty` | sum of `pipeline[]` qty if any, else `null` when any `mfrLeadTime > 0`, else `0` |
| `stock_breakdown` rows | one per source-part — `warehouse` like `"Arrow / VERICAL — ships from Japan"`. Duplicates have `" — mirror"` appended. Pipeline entries appended as additional rows. |

### Gotchas

1. **Auth pair in TWO places.** Querystring `login=...&apikey=...` AND nested inside `req` JSON. Skipping either = 401.
2. **Inventory mirrors.** The same physical stock (e.g. 31,781 USA STM32G030F6P6 units) appears under both Verical and Arrow ACNA. Without dedup, `SUM(fohQty)` doubles. The driver dedupes by `(fohQty, shipsFrom, shipsIn)` tuple and tags the second occurrence with `is_mirror_of_earlier=True` (label suffix `" — mirror"`). Mirror rows are still emitted to `batch_index` so downstream can choose `SUM` (filter mirrors first) vs `MAX-by-(ships_from, qty)`.
3. **Currency varies per warehouse.** USD for ACNA/Verical USA, EUR for Arrow EUROPE, JPY for Verical Japan. `currency` column in batch_index is per-row.
4. **MOQ + price tiers are per-warehouse.** Unlike Mouser/Digikey/Element14 (top-level only), Arrow exposes a distinct MOQ + tier set for every `sourcePart`. The batch driver pulls these via index-matched `site_sources[i]` (which is 1:1 with the first N entries of `stock_breakdown[]` — pipeline rows come after).

---

## 5. LCSC_立创商城 Mall OpenAPI — working (since 2026-05-19)

**Script:** `api/scripts/api_lcsc.py`
**Base URL:** `https://open-api.jlc.com`
**Auth:** HMAC-SHA256 signature over a 5-line canonical string, packed into an `Authorization: JOP …` header. **Verified offline** against the doc's worked example before any live calls (module-import self-test refuses to call the API if the signing pipeline is broken).

Auth header format:
```
Authorization: JOP appid="…",accesskey="…",nonce="…",timestamp="…",signature="…"
```
where
```
string_to_sign = f"{METHOD}\n{path[?query]}\n{timestamp}\n{nonce_32}\n{body_compact_json}\n"
signature      = base64( HMAC-SHA256(string_to_sign, SecretKey) )
```

Credentials in `.env`: `lcsc_AppID`, `lcsc_AccessKey`, `lcsc_SecretKey` (lowercase prefix, matching the user-provided names).

**Primary endpoint:** `POST /lcsc/openapi/product/search/global` with `{"keyword": "<MPN>"}`.

### Response essentials

`data[]` array of products with:
- Identity: `productId`, `productCode` (LCSC C-number like `C60568`), `productModel` (manufacturer P/N), `productName`, `brandName` (often `"ST(意法半导体)"` style bilingual), `standard` (package), `description`.
- Stock: `gdStockNum` (广东仓), `jsStockNum` (江苏仓) — both integers.
- Prices: `priceList[]` of `{startStep, originPrice, discountedPrice}`. Currency is implicitly **CNY** (LCSC is China-domestic).

The search endpoint does NOT expose factory lead time or detailed spec parameters — those would require follow-up `sku/product/basic` calls (by productId) for each variant.

### Canonical mapping

| Canonical | LCSC source |
|---|---|
| `stock_now_qty` | `gdStockNum + jsStockNum` |
| `stock_future_qty` | `0` (no future-stock field exposed by this endpoint) |
| `stock_breakdown` rows | Two — `"LCSC / 广东仓"` and `"LCSC / 江苏仓"` |
| `prices` tier | `discountedPrice` as canonical `unit_price_float`, `originPrice` kept as `site_origin_price` |

### Gotchas

1. **200 calls/day per endpoint.** Hit during the 5-19 full sweep (103 chips fell within the daily window, but combined with earlier probes + dry-runs we exceeded the cap around chip 55). Driver records 429s as `http_error` rows with the body excerpt `{"code":429,"success":false,"message":"接口请求已超过API每天额度限制"}`. Quota resets at 00:00.
2. **Body bytes must match the signing input exactly.** Don't pass `json=` to `requests.post()` (re-serializes with default separators that differ from compact form). Use `data=body.encode("utf-8")` with explicit `Content-Type: application/json` header.
3. **Multiple success codes in docs.** Some endpoints return `code:0` for success, others `code:200`. We accept both; check `successful: true` as the canonical signal.
4. **MPN drift via fuzzy match.** Input `HT66F017-HF` returns `HT66F0176` — LCSC's keyword search picks the closest catalog entry when an exact match isn't carried. The batch driver's best-variant picker prefers exact MPN match, but when none exists it falls back to highest stock and `returned_mpn ≠ input_mpn`. Downstream `mfr_match` check stays meaningful (manufacturer is usually consistent across the variant family); `returned_mpn` column should be inspected before trusting.
5. **Provisioning gotcha (now resolved).** When the account was first provisioned, `search/global` + `price/by/code` + `stock/by/id` all returned `data:[]` for every input. The `sku/product/basic` endpoint worked. Reported to LCSC support with J-Trace-IDs; vendor-side fix took ~2 hours. See `memory/project_lcsc_api_blocked.md` for the resume protocol if it ever recurs.

---

## Batch driver (`api/scripts/batch_api_test.py`)

Runs every chip in `ref/Chip_DataSource_Master.xlsx` through any subset of the 5 sources and produces a consolidated, warehouse-granular result set under `test/api_test/BatchTest_<YYYYMMDD>_<HH_MM_SS>/`.

### Parallelism model

- **Within a chip:** the N selected sources run concurrently in a `ThreadPoolExecutor` (default `max_workers = len(sources_to_run)`, capped automatically). Per-chip wall clock is dominated by the slowest source (typically Digikey ~3-9 s).
- **Across chips:** serial. Chip K+1 only starts after all sources for chip K complete. This keeps per-source rate limits trivially satisfied — Element14's 2 req/s guard becomes a no-op since each Element14 call is naturally ~5-7 s apart.
- `--max-workers 1` forces serial mode for debugging.
- `--throttle 0.3` (default) is now an **inter-chip** pause, not per-call.

Measured speedup on the 103-chip × 5-source sweep: **5.36 min parallel vs ~23-25 min if serial** (serial estimate based on summed mean latencies).

### CLI

```
.venv/Scripts/python.exe api/scripts/batch_api_test.py                       # all 5 sources, full sweep
.venv/Scripts/python.exe api/scripts/batch_api_test.py --limit 3             # dry-run on first 3 chips
.venv/Scripts/python.exe api/scripts/batch_api_test.py --only LCSC --only ARROW
.venv/Scripts/python.exe api/scripts/batch_api_test.py --throttle 0.5 --max-workers 2
```

### Display names

User-facing artifacts (`batch_index.csv/.xlsx/.json`, `batch_summary.md`, `failures.md`) write the long form for the source column:

| Short (internal) | Display |
|---|---|
| MOUSER | Mouser_贸泽 |
| DIGIKEY | DIGIKEY_得捷电子 |
| ELEMENT14 | ELEMENT14_e络盟 |
| ARROW | ARROW_艾睿 |
| LCSC | LCSC_立创商城 |

Defined in `SOURCE_DISPLAY_NAME` at the top of `batch_api_test.py`. Internal code (SOURCES_ALL, SOURCE_RUNNERS, dispatch keys, dict keys in main loop) stays on the short codes. The mapping is also mirrored in `api/scripts/_update_readme_status.py` for the auto-refreshed README block.

### Output schema (24 columns in batch_index)

See `api/doc/batch_output_schema.md` for the full column reference + type coercion notes. Quick reminder of the granularity: one row per `(input_mpn × source × warehouse)`. Non-ok `(chip, source)` pairs emit a single empty-warehouse row with `status` set.

### Latest sweep highlights — 2026-05-19 17:54:29 → 17:59:50

| Source | OK | No results | Failed | Total | OK % |
|---|---|---|---|---|---|
| Mouser_贸泽 | 63 | 40 | 0 | 103 | 61.2% |
| DIGIKEY_得捷电子 | 60 | 43 | 0 | 103 | 58.3% |
| ELEMENT14_e络盟 | 43 | 60 | 0 | 103 | 41.7% |
| ARROW_艾睿 | 42 | 61 | 0 | 103 | 40.8% |
| LCSC_立创商城 | 41 | 14 | 0 | 55 | 74.5% |

Note: LCSC denominator is 55 because 48 chips hit the 200/day quota (HTTP 429); those rows were filtered out of the snapshot. Pass-rate on the available subset is the highest of any source — domestic distributors cover Chinese-popular MCUs / discretes better.

Top single-warehouse stock pools:
- ARROW: `LM317LD13TR` 6,375,000 pcs at Verical USA
- DIGIKEY: `LTST-C190KGKT` 2,118,100 pcs at DigiKey US warehouse
- MOUSER: `LTST-C190KGKT` 1,302,540 pcs
- ELEMENT14: `LTV817B-V-G` 389,596 pcs (cn.element14.com aggregate)
- LCSC: `STM32F030F4P6TR` 29,878 pcs at 广东仓

---

## Cross-source data caveats

| Concern | Status |
|---|---|
| **Currency** | Mouser RMB, Digikey USD, Element14 CNY, Arrow per-warehouse (USD/EUR/JPY), LCSC CNY. `batch_index` is multi-currency on a single row basis. No FX-rate column — downstream cross-currency math is out of scope. |
| **Lead time units** | Normalized to days in `lead_time_days`. Free-text `ship_text` keeps source-native unit (Digikey "weeks", Mouser .cn "天数", Element14 "天", Arrow "days"). |
| **Mirror / aggregate rows** | Arrow's `— mirror` rows and Element14's `Stock level (total)` aggregate row are KEPT in `batch_index` for buyer-side visibility. Naive `SUM(stockpool_qty) GROUP BY input_mpn, source` will double-count both — filter rules documented in `batch_output_schema.md`. |
| **MPN drift** | LCSC and Digikey's keyword search occasionally pick a close-but-not-exact MPN (e.g. `HT66F017-HF` → `HT66F0176`). Inspect `returned_mpn` column before treating the row as a verified match. `mfr_match` is the cross-check. |
| **Manufacturer mismatches** | 18 surfaced on the 5-19 sweep — recurring LITEON → MURATA/INFINEON drift on Element14 LTV*/LTW* parts; ARROW returns NXP for WEEN-branded thyristors (NXP acquired the WEEN line, so technically correct but flagged by substring rule). |

---

## Auth + secrets handling

- All credentials in `api/.env` (gitignored). Scripts read via `python-dotenv → os.environ.get(...)` only — never write keys to logs or output files.
- `api/.env.example` documents the env-var names without values.
- The `attempts[]` log per call records `status` / `len` / `outcome` but never includes request bodies (which carry credentials) or response bodies past 500 chars.
- LCSC adds: signature `_selftest()` runs at module import so a broken signing pipeline cannot burn quota with bad auth.

---

## Future work / backlog

- **Octopart / Nexar** — multi-vendor aggregator. `.env` keys (`NEXAR_CLIENT_ID` / `NEXAR_CLIENT_SECRET`) reserved, not yet acquired.
- **LCSC quota increase** — 200/day per endpoint is tight for 100+ chip sweeps. Ask LCSC support for a production tier; alternatively cache LCSC results across days and only re-fetch chips that changed.
- **Cross-currency conversion** — opt-in FX-rate stamp at run time (CNY/USD/EUR), kept as a separate `batch_index_usd.csv` for BOM-level comparison without polluting the source-native rows.
- **Better noise filtering on Digikey keyword search** — score returned MPNs by Levenshtein similarity to the input; demote obvious misfires to a "fuzzy" section.
- **Sandbox URLs** — Digikey has `sandbox-api.digikey.com`. Useful for development without burning the daily quota.
- **429 retry-with-backoff** — currently a 429 is recorded and the chip moves on. A short backoff + retry (especially for LCSC's once-per-day quota burst at midnight) could recover transient rate-limit hits.
- **Cross-track integration** — the scraper track and API track produce identical canonical schemas. A `common/compare_<source>.py` family of scripts (one for each source where we have both an API client and a scraper) generates a diff report. Currently only Digikey is implemented.

---

## Files of record

- `api/scripts/api_mouser.py` — Mouser Search API v1 client
- `api/scripts/api_digikey.py` — Digikey PIM v4 client (with module-level OAuth token cache)
- `api/scripts/api_element14.py` — Element14 Catalog Search client
- `api/scripts/api_arrow.py` — Arrow Pricing & Availability v4 client (with inventory-mirror dedup)
- `api/scripts/api_lcsc.py` — LCSC Mall OpenAPI client (with signature self-test on import)
- `api/scripts/batch_api_test.py` — 5-source-parallel batch driver
- `api/scripts/_update_readme_status.py` — auto-refresh for `api/README.md` status block
- `api/doc/batch_output_schema.md` — full 24-column data contract
- `api/requirements.txt` — `requests`, `python-dotenv`, `openpyxl`
- `api/.env.example` — credential placeholders
- `api/README.md` — track-level overview + live status snapshot
- `common/_summary.py` — shared per-MPN summary renderer
- Counterpart scraper-track report: `scraper/doc/scraper_report_v2.md`
- Memory pointers: `MEMORY.md` indexes the cross-track feedback rules (output folder convention, stock breakdown fields, MPN-variant grouping, site-native wording) — all apply equally to this track. `project_batch_state.md` carries the live batch state.
