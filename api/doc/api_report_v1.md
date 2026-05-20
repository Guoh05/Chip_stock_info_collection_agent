# Distributor API Test Report v1 — Mouser + Digikey

**Date:** 2026-05-17 (first iteration of the API track)
**Test parts:** `BT168GW,115` (WeEn SCR, SC-73), `STM32G030F6P6` (ST 32-bit MCU, TSSOP-20)
**Stack:** Python 3.10.9, `requests` 2.34.2, `python-dotenv` 1.2.2. No browser, no TLS impersonation — these are first-party REST APIs.

## TL;DR

| # | Source | Status | Endpoint | Auth | Quality |
|---|---|---|---|---|---|
| 1 | **Mouser Search API v1** | working | `POST /api/v1/search/partnumber` (fallback `/keyword`) | API-key in query string | high |
| 2 | **Digikey Product Information API v4** | working | `POST /products/v4/search/keyword` | OAuth2 client_credentials → bearer | high |

Both APIs returned full canonical-schema records for both test parts on the very first call — no rate-limit retries, no CAPTCHAs, no `bm-verify` / Akamai walls. This is exactly what motivated the API track: Mouser is unreachable for scraping (blocked by Akamai BMP) but ships a free Search API; Digikey works for both but the API is one round-trip vs. ~30 s of Cloudflare wait under Playwright.

## Output schema (shared with the scraper track)

Every per-variant record emits the same five fields the scraper track uses (see `scraper/doc/scraper_report_v2.md` and `common/_summary.py`):

| Field | Meaning |
|---|---|
| `stock_now_qty` | 现货 quantity — distributor's own warehouse |
| `stock_now_ship_text` | 发货时间 string for 现货 |
| `stock_future_qty` | 期货/在途 quantity (`null` for unbounded factory order) |
| `stock_future_ship_text` | Lead-time string for the future pool |
| `stock_breakdown` | `[{label, warehouse, quantity, ship_text, note?}, …]` using the **API's own field names** |

Site-native fields are preserved verbatim under `site_*` keys (`site_availability`, `site_factory_stock`, `site_lead_time`, `site_availability_on_order` for Mouser; `site_quantity_available`, `site_manufacturer_lead_weeks`, `site_normally_stocking`, `site_back_order_not_allowed`, `site_non_stock`, `site_discontinued`, `site_end_of_life`, `site_date_last_buy_chance`, `site_product_status` for Digikey).

**MPN-variant grouping rule.** A keyword search that returns multiple distinct `ManufacturerPartNumber` strings → one subfolder per variant. Never aggregate across MPN strings (see `feedback_mpn_variant_grouping.md`).

## Folder layout (per run)

```
test/api/Test_<MPN>_<CHANNEL>_<YYYYMMDD>_<HH>_<MM>_<SS>/
├── parent_summary.md              # Markdown overview + variant index
├── <MPN>.json                     # Parent run record + variants_summary[]
├── raw_response.json              # Full API payload (for audit)
└── <variant_mpn>/                 # One per distinct returned MPN string
    ├── <variant_mpn>.json         # Normalized canonical-schema record
    ├── <variant_mpn>_raw_part.json (Mouser) / _raw_product.json (Digikey)
    └── <variant_mpn>_summary.md   # Rendered by common/_summary.py
```

Mirrors the scraper track's LCSC v3 / Future Electronics layout; the only differences are `test/api/` instead of `test/scraper/` and the per-variant raw file naming.

---

## 1. Mouser Search API v1 — working

**Script:** `api/scripts/api_mouser.py`
**Base URL:** `https://api.mouser.com/api/v1`
**Auth:** API key in query string (`?apiKey=<KEY>`). Free; register at https://www.mouser.com/api-hub/.
**Primary endpoint:** `POST /search/partnumber` with body
```json
{"SearchByPartRequest": {"mouserPartNumber": "<MPN>", "partSearchOptions": "Exact"}}
```
**Fallback endpoint:** `POST /search/keyword` (used if partnumber search returns 0). Body:
```json
{"SearchByKeywordRequest": {"keyword": "<MPN>", "records": 50, "startingRecord": 0,
                            "searchOptions": "", "searchWithYourSignUpLanguage": ""}}
```

### Response shape (per part)

Each item in `SearchResults.Parts[]` carries:
- Identity: `ManufacturerPartNumber`, `MouserPartNumber`, `Manufacturer`, `Description`, `Category`, `LifecycleStatus`, `ROHSStatus`, `ProductDetailUrl`, `DataSheetUrl`, `ImagePath`.
- Stock: `Availability` (display string like `"108590 库存量"`), `AvailabilityInStock` (numeric string), `FactoryStock` (numeric string), `AvailabilityOnOrder[]` (`{Quantity, Date}` pending PO batches), `LeadTime` (string).
- Ordering: `Min`, `Mult`, `PriceBreaks[].{Quantity, Price, Currency}`.
- Specs: `ProductAttributes[].{AttributeName, AttributeValue}`.
- Compliance: `ProductCompliance[].{ComplianceName, ComplianceValue}` (HTS / ECCN / TARIC etc.).
- Misc: `AlternatePackagings[].APMfrPN` (e.g. TR variant of base MPN), `UnitWeightKg.UnitWeight`.

### Canonical mapping

| Canonical | Mouser source |
|---|---|
| `stock_now_qty` | `AvailabilityInStock` (numeric) — fallback: leading integer from `Availability` |
| `stock_now_ship_text` | `"Mouser 在库,下单后立即发货"` (constant when in-stock) |
| `stock_future_qty` | `FactoryStock` if > 0; otherwise `null` (unbounded factory order) when `LeadTime` is set |
| `stock_future_ship_text` | `"原厂标准交货期 <LeadTime>"` |
| `stock_breakdown` rows | `Availability` → in-stock row; `FactoryStock`+`LeadTime` → factory-stock row; each `AvailabilityOnOrder[]` entry → its own `OnOrder` row with the expected arrival date |

### Test results

| Query | Variants | Highlight |
|---|---|---|
| `STM32G030F6P6` | 1 (`STM32G030F6P6`, Mouser P/N `511-STM32G030F6P6`) | 现货 **108,590** + `FactoryStock 0` (LeadTime `210 天数` → unbounded factory order); 8 price tiers ¥9.00 → ¥4.93 |
| `BT168GW,115` | 1 (`BT168GW,115`, Mouser P/N `771-BT168GW-T/R`) | 现货 **0** + on-order **13,694** arriving 2026-05-26 (LeadTime `112 天数`); 9 tiers ¥4.83 → ¥0.864 |

Runs: `test/api/Test_STM32G030F6P6_MOUSER_20260517_12_49_48/` and `test/api/Test_BT168GW_115_MOUSER_20260517_12_50_52/`.

### Notes / gotchas

1. **API key is locale-bound.** This key was registered on Mouser China — every response comes back localized to zh-CN: `Availability` is `"108590 库存量"`, parameter names are `"封装"` / `"标准包装数量"`, currency is `RMB` (¥), and `LeadTime` is in **days** suffixed with `"天数"` (NOT weeks). When the buyer asks for "lead time in weeks", we'd need a different key registered on the US site, or a post-processing conversion.
2. **`Availability` mixes value + unit.** Parse the leading integer (`re.match(r"-?\d+")`) — don't compare to literal strings. Sometimes `AvailabilityInStock` carries the bare number; prefer it when present.
3. **`AvailabilityOnOrder` is a real signal.** For BT168GW,115, Mouser had 0 in-stock but 13,694 already-ordered-from-factory units arriving 2026-05-26. This is buyer-relevant — surfaced as its own `OnOrder` row so it doesn't get hidden inside the abstract "factory order" pool.
4. **`partSearchOptions: "Exact"` is intolerant of separators.** A query containing commas / spaces matched fine in our two test parts, but ambiguous keywords can fall through to 0 results — the fallback `/search/keyword` covers those. Both attempts are recorded in `attempts`.
5. **No OAuth, no token refresh, no throttle yet.** Free tier allows 1000 calls/day. For batch jobs we'll want to add a simple per-second sleep and 429-retry-with-backoff.

---

## 2. Digikey Product Information API v4 — working

**Script:** `api/scripts/api_digikey.py`
**Base URL:** `https://api.digikey.com`
**Auth:** OAuth2 `client_credentials` flow. Register at https://developer.digikey.com/ → create a Production app → get `DIGIKEY_CLIENT_ID` + `DIGIKEY_CLIENT_SECRET`.

**Token endpoint:** `POST /v1/oauth2/token` with form body `client_id=…&client_secret=…&grant_type=client_credentials` → `{access_token, expires_in: 599, token_type: "Bearer"}`.

**Search endpoint:** `POST /products/v4/search/keyword` with:
- Headers: `Authorization: Bearer <token>`, `X-DIGIKEY-Client-Id: <id>`, `X-DIGIKEY-Locale-Site: US`, `X-DIGIKEY-Locale-Language: en`, `X-DIGIKEY-Locale-Currency: USD`, `X-DIGIKEY-Customer-Id: 0`.
- Body: `{"Keywords": "<MPN>", "Limit": 50, "Offset": 0}`.

### Response shape

Two parallel arrays — `ExactMatches[]` (MPN matches exactly) and `Products[]` (broader keyword match). We dedupe by `ManufacturerProductNumber` and prefer exact-match order.

Each Product carries:
- Identity: `ManufacturerProductNumber`, `Manufacturer.Name`, `Description.{ProductDescription, DetailedDescription}`, `Category.Name`, `DatasheetUrl`, `PhotoUrl`, `ProductUrl`, `ProductStatus.Status` (e.g. `"Active"`).
- Stock: `QuantityAvailable` (top-level, the DK warehouse inventory total), `ManufacturerLeadWeeks` (string like `"30"` — bare integer, **assumed weeks**), `NormallyStocking` (bool), `BackOrderNotAllowed` (bool), `NonStock`, `Discontinued`, `EndOfLife`, `DateLastBuyChance`.
- Variations: `ProductVariations[]` — one per packaging form. Each has `DigiKeyProductNumber`, `PackageType.Name` (Tube / Tape & Reel (TR) / Cut Tape (CT) / Digi-Reel®), `QuantityAvailableforPackageType`, `StandardPricing[].{BreakQuantity, UnitPrice, TotalPrice}`, `MinimumOrderQuantity`, `StandardPackage`.
- Specs: `Parameters[].{ParameterText, ValueText}` (24 specs for STM32G030F6P6, 14 for BT168GW,115).
- Classifications (CORRECTED keys for v4): `Classifications.HtsusCode` (US HTS), `Classifications.ExportControlClassNumber` (ECCN — sometimes `"EAR99"`), `Classifications.RohsStatus`, `Classifications.ReachStatus`, `Classifications.MoistureSensitivityLevel`. **Not** `HtsCode`/`EccnCode` — those don't exist on v4.

### Canonical mapping

| Canonical | Digikey source |
|---|---|
| `stock_now_qty` | `QuantityAvailable` |
| `stock_now_ship_text` | `"下单后立即发货"` (constant when stock > 0) |
| `stock_future_qty` | `null` (unbounded — factory order with no committed quantity) when `NormallyStocking` is true or `ManufacturerLeadWeeks` is set |
| `stock_future_ship_text` | `"原厂标准交货期 <N> weeks"` |
| `stock_breakdown` rows | One `QuantityAvailable` row, one `Factory Stock (ManufacturerLeadWeeks)` row, plus one `Packaging — <PackageType>` row per `ProductVariations[]` entry (so the buyer can see the tube vs. reel split inside the DK warehouse total) |

### Test results

| Query | Variants captured (exact + fuzzy) | Highlight |
|---|---|---|
| `STM32G030F6P6` | 6 (1 exact + 5 fuzzy) — `STM32G030F6P6` (497-STM32G030F6P6-ND, 现货 **76,636**, 30 weeks lead, 9 tiers $1.58→$0.766), `STM32G030F6P6TR` (497-STM32G030F6P6TR-ND, 现货 12,073), plus 4 unrelated keyword matches (`SM-I-010`, `U024-V2`, `M137`, `SM-I-005`) | Exact match returns the base MPN; the TR variant is also surfaced (separate folder). 4 stale keyword matches are noise (kept as variants so we don't accidentally drop a real alternative) |
| `BT168GW,115` | 2 — `BT168GW,115` (1740-1084-2-ND, 现货 **4,424**, 26 weeks lead, 6 tiers $0.178→$0.130) + `BT168GWF,115` (1740-1085-2-ND, 现货 37,597) | The base part and the "F" variant are clearly distinct SKUs — kept separate per the MPN-variant rule |

Runs: `test/api/Test_STM32G030F6P6_DIGIKEY_20260517_12_56_11/` and `test/api/Test_BT168GW_115_DIGIKEY_20260517_12_56_36/`.

### Notes / gotchas

1. **Lead time is a bare integer.** `ManufacturerLeadWeeks: "30"` — no unit suffix. The convention is **weeks** (verified against `digikey.cn`'s "标准交货期 30 周" rendering). Normalizer appends `" weeks"` in the summary; raw `site_manufacturer_lead_weeks` keeps the bare integer for audit.
2. **Classification keys are different from v3.** v4 uses `HtsusCode` (not `HtsCode`), `ExportControlClassNumber` (not `EccnCode`), and exposes `ReachStatus` + `MoistureSensitivityLevel`. Adjusted the normalizer to read these correctly; fallback to v3 names kept in case the API drift goes the other way.
3. **Keyword search returns noise.** A keyword like `STM32G030F6P6` returned 4 unrelated matches (relay boards, sensor kits) alongside the actual MPN. We keep them all as separate variants per the MPN-variant rule, but a future enhancement could score-rank by string similarity and demote obvious misfires (or use `productdetails` endpoint when we already know the canonical DK P/N).
4. **OAuth token caches 599 s.** We don't currently cache the token across runs — every script invocation pays one token round-trip (~150 ms). For high-volume batch runs we should cache it to a file (`.token_cache.json` with expiry timestamp).
5. **Locale picks currency.** Setting `X-DIGIKEY-Locale-Currency: USD` gives USD pricing; `CNY` would give CNY. We use USD for cross-batch consistency since the scraper-track Digikey scrape also reads from digikey.cn → CNY isn't apples-to-apples anyway.
6. **`isBackOrderAllowed` lives off `BackOrderNotAllowed`.** v4 uses the inverted name — `BackOrderNotAllowed: false` means back-order IS allowed. Read carefully.

---

## What this enables

- **Mouser:** previously a complete dead end (Akamai bm-verify wall in the scraper track) — now fully working with high data quality, including the on-order batch detail that the website itself displays.
- **Digikey:** previously needed ~30 s of Playwright + Cloudflare clear per part — now one OAuth round-trip + one search call (~500 ms total). For batch jobs across 100s of MPNs, this is two orders of magnitude faster.
- **Cross-track comparability:** both APIs map onto the same canonical schema as `scrape_lcsc_v3.py` / `scrape_digikey.py` / `scrape_hqew.py` / `scrape_future.py`. The same `common/_summary.py` renders all of them. A buyer reading `<MPN>_summary.md` cannot tell whether the data came from scraping or an API — by design.

## Auth + secrets handling

- API keys live in `api/.env` (gitignored). Scripts read via `python-dotenv → os.environ.get(...)` only — never write the keys to logs or output files.
- `api/.env.example` documents the env-var names without values.
- The `attempts[]` log records `status` / `len` / `outcome` per call but never includes request bodies (which carry credentials) or response bodies past 500 chars (Mouser auth errors come through in plain text; truncated to avoid leaking).

## Future work / backlog

- **Octopart / Nexar** — aggregator that covers Arrow (also scraper-blocked by Akamai). Free tier available via Nexar developer portal.
- **Element14 (Farnell) API** — another distributor for EU coverage.
- **Token cache for Digikey** — file-based or in-process; 599 s TTL.
- **429 / rate-limit handling** — exponential backoff + per-second throttling for batch sweeps.
- **Sandbox URLs** — Digikey has `sandbox-api.digikey.com`; useful for development without burning the daily quota. Not yet implemented.
- **Currency conversion** — Mouser key returns RMB, Digikey returns USD. For BOM-level price comparison, we'll want a single canonical currency (USD likely) with FX-rate metadata stamped on the run.
- **De-noising Digikey keyword results** — when 4 of 6 returned variants are unrelated to the queried MPN, the variants table gets crowded. Score by `manufacturer_part_number` prefix-similarity and surface the noise in a separate "unrelated keyword matches" section of the parent summary.

## Files of record

- `api/scripts/api_mouser.py` — Mouser Search API v1 client.
- `api/scripts/api_digikey.py` — Digikey Product Information API v4 client.
- `api/requirements.txt` — `requests`, `python-dotenv` (no Playwright / curl_cffi needed).
- `api/.env.example` — API-key placeholders. Real `.env` is gitignored.
- `api/README.md` — track-level overview + conventions.
- `common/_summary.py` — shared per-MPN summary renderer (scraper + API).
- Memory: `MEMORY.md` indexes the cross-track feedback rules (`feedback_test_output_folder.md`, `feedback_stock_breakdown_fields.md`, `feedback_mpn_variant_grouping.md`, `feedback_site_native_fields.md`) — all apply equally to this track.
- Counterpart scraper-track report: `scraper/doc/scraper_report_v2.md`.
