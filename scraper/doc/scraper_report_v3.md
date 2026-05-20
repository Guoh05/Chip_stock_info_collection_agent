# Web Scraper Test Report v3 — 9 Working Sources + Batch Driver + Audited Data

**Date:** 2026-05-20 (rev. since 2026-05-19 — bom2buy recovered via Opera-profile session reuse + master input swapped to Shortage Emergency Response List v2)
**Scope of changes since v2:** 4 → 9 working scrapers, 3 additional sources evaluated and dropped, batch driver + warehouse-exploded `batch_index.csv` schema aligned with the API track, comprehensive audit of every scraper to remove fabricated labels ("uncertain → blank, never invent"), **bom2buy default post-step in the batch driver**, **master input migrated to `ref/Shortage Emergency Response List_v2.xlsx` sheet `Part List Modify` (107 unique MPNs after dedup, MPN col `Manufacture Part Number`)**.
**Stack:** Python 3.10.9 (`.venv/`), `curl_cffi` 0.15.0 (TLS impersonation), Playwright Chromium **and** Firefox + `playwright-stealth`, **Playwright over user's Opera install (bom2buy)**, BeautifulSoup/lxml, openpyxl.

## TL;DR

| # | Source | Status | Engine | Pass-rate on 51-chip sweep (2026-05-19) | Bot-protection encountered |
|---|---|---|---|---|---|
| 1 | **LCSC** (立创商城, szlcsc.com) | ✅ | Playwright Chromium `--headless=new` | 76.5 % (3 transient `_tmp_*` race exceptions) | none |
| 2 | **Digikey** (得捷, digikey.cn) | ✅ | Playwright stealth Chromium | 52.9 % (24 cells: 22 `failed` = part not carried, 2 intermittent Cloudflare) | Cloudflare JS challenge |
| 3 | **HQEW** (华强电子网, hqew.com) | ✅ | Playwright Chromium | 82.4 % | jsjiami.com.v7 JS obfuscator |
| 4 | **Future Electronics** | ✅ | Playwright **Firefox** | 47.1 % | Akamai BMP on `/`; Chromium HTTP/2 reject — Firefox passes |
| 5 | **RSONLINE** (RS 欧时, rsonline.cn) | ✅ | curl_cffi + Next.js `__NEXT_DATA__` | 5.9 % (WAF-rate-limited during sweep; ~70 % on isolated runs) | Akamai (occasional rate cap) |
| 6 | **ONEYAC** (唯样商城, oneyac.com) | ✅ | Playwright Firefox | 51.0 % | none |
| 7 | **ICKEY** (云汉芯城, ickey.cn) | ✅ | Playwright Chromium | 82.4 % | none — but is a marketplace **aggregator** (resale from Digi-Key / 云汉) |
| 8 | **Rochester Electronics** (rocelec.com) | ✅ | Playwright Firefox | 9.8 % | none — but is **EOL-only**; modern parts rarely hit |
| 9 | **bom2buy** (买芯片网, bom2buy.com) | ✅ (**session-dependent**) | Playwright + **user's Opera install** (user-data-dir reuse) | 80 % on 10-chip pilot (2026-05-20) | IconCaptcha — bypassed by reusing user-passed session |
| 10 | **Mouser** (贸泽, mouser.cn / .com) | ❌ | n/a | n/a | Akamai BotManager `bm-verify` |
| 11 | **Arrow** (艾睿电子, arrow.com) | ❌ | n/a | n/a | Akamai BotManager `_abck` |

**9 / 11 working web sources.** Mouser + Arrow remain blocked (Akamai BMP); both are accessible via the parallel `api/` track using official keys.

Sources **evaluated and dropped 2026-05-18:**
- `cn.element14.com` (e络盟) — Akamai BMP 403 even after homepage warmup
- `verical.com` (Arrow legacy) — "系统错误" popup + WAF on repeated probes
- `chip1stop.com` — dead / 301 to `arrow.com` after acquisition

**`bom2buy.com` was dropped 2026-05-18 (IconCaptcha gate)** but recovered 2026-05-20 via Playwright + the user's Opera install: the user manually passes the IconCaptcha once in Opera, then we drive Opera through Playwright (`executable_path` + `user_data_dir` pointing at the real Opera profile) so we inherit the captcha-cleared session cookies. See § "bom2buy via Opera session reuse" below for the recipe and operational caveats.

---

## What's new since v2

### A. Four new working sources

| Source | Native concept | What makes it work |
|---|---|---|
| **RSONLINE** | RS 欧时 — standalone distributor with full Schema.org JSON-LD | `curl_cffi` chrome131 → no Playwright needed. Stock from Adobe analytics data layer `"stockinfo":{"date":"…","quantity":"…","status":"IN_STOCK"}` — pipe-separated for "ships from another location" + future-ship-date pairs. |
| **ONEYAC** | 唯样商城 — marketplace, B2C | Playwright Firefox. Stock at `<span id="detail_inventory">N</span>`. MOQ from `dynamicOrderMinNum` + `minPack` (effective MOQ = max of the two). Lead time `交期：N天-M天` (现货) or `交期 NW` (OOS / 期货). |
| **ICKEY** | 云汉芯城 — aggregator (resells Digi-Key / 云汉 listings) | Playwright Chromium + doT.js template hydration wait (poll until `¥N` numeric prices appear). Title format `<MPN>（<distributor>）采购_价格…` exposes the upstream distributor. MOQ + 货期 visible. |
| **Rochester** | EOL / Last-Time-Buy parts | Playwright Firefox; URL `/global-search/<MPN>` (red-box "product inventory" search); click first `span.productName` matching the input MPN, wait for Lightning Web Components hydration on `/part/<SalesforceID>-<MPN_no_punct>`. **Bails with `no_results` when no productName text contains the input MPN** — the page returns category-related products as fallback otherwise (silently scraping wrong parts was a real bug). |

### B. Batch driver — `scraper/scripts/batch_scraper_test.py`

A subprocess-per-call orchestrator across all 8 sources.

- **CLI:** `--mpns "MPN1:MFR1;MPN2:MFR2;…"` (semicolon-separated, since MPNs can contain `,`), or `--xlsx <path>` for the full master sheet. `--only` to subset channels, `--limit N` for dry runs, `--throttle Xs` politeness gap between chips, `--resume` to skip already-scraped cells under the most recent BatchTest folder.
- **Channel-parallel dispatch** (default) — `ThreadPoolExecutor(max_workers=len(channels_used))` fans out all 8 channels per chip; chip wallclock ≈ max(channel times) instead of sum. Each channel hits a different domain → no per-vendor rate-limit collision.
- **Hard isolation** — each (MPN × source) call is a separate `subprocess.run(...)` with per-channel timeout (LCSC 240s, Digikey 180s, HQEW 90s, Future 300s, RSONLINE 90s, ONEYAC 120s, ICKEY 150s, Rochester 180s). A hung browser dies cleanly via timeout; a crash in one subprocess can't take down the others.
- **Output** — see "v3 schema" below.

Reference run (51 chips × 8 sources, 2026-05-19):
- Wallclock: **57.9 min** (sequential would be ~5 hours).
- 408 (chip × source) cells → **585 warehouse rows** after the v3 schema explosion.
- Cross-source coverage histogram: 1 chip hit by all 8 sources, 3 by 7, 11 by 6, 7 by 5, 9 by 4, 9 by 3, 5 by 2, 5 by 1, 1 chip missing from every source.

### C. v3 schema — `batch_index.csv` warehouse-exploded, API-aligned

The v1 schema (per `(MPN × channel)`, 20 columns, plus a wide-form `batch_compare.csv`) has been replaced. v2 (the term "schema v2" referred to **scraper-only** revisions earlier; **v3 here** refers to **API-aligned scraper output**):

| Aspect | v1 (pre-batch) | v2 / v3 (now) |
|---|---|---|
| Granularity | 1 row per `(MPN, channel)` | 1 row per `(input_mpn, source, warehouse)` |
| Column count | 20 | **26** (24 API-aligned + 2 scraper extras: `elapsed_sec`, `num_variants`) |
| `source` cell value | `LCSC` | `LCSC_立创商城` (bilingual; English prefix matches API enum) |
| Per-warehouse fields | absent | `warehouse`, `warehouse_idx`, `ships_from`, `stockpool_qty`, `ship_text`, `lead_time_days`, `moq` |
| Tier-price columns | `price_at_qty_1`, `lowest_unit_price` | `price_at_min_qty`, `max_break_qty`, `price_at_max_qty` (renamed for API parity) |
| Wide-form file | `batch_compare.csv/.xlsx` (43 cols) | **removed** — downstream pivots from the long form |

First 24 columns are byte-identical to `api/doc/batch_output_schema.md`. Two tracks can `UNION ALL` after dropping the scraper extras. Full spec: `scraper/doc/batch_output_schema.md`.

### D. Data-quality audit — "uncertain → blank, never invent"

Every scraper was audited against the actual product-page HTML. Labels that turned out to be **scraper-fabricated rather than page truth** were removed. Concrete corrections shipped:

| Source | Symptom found | Fix |
|---|---|---|
| **LCSC** | `stock_breakdown` rows labelled `广东仓` / `江苏仓` / `SMT扩展库` invented from API field names (page UI shows aggregate `现货 N` only) | Single `现货` row with `warehouse=None`. Per-warehouse API values still kept as `site_*` fields. |
| **LCSC** | `min_buy_number=1` (API) didn't match visible `起订量：5 个` on the page | Added Playwright JS extract for visible `起订量：N 个` → `min_order_qty`; API field kept as `min_buy_number`. |
| **DIGIKEY** | Fabricated `warehouse="DigiKey 美国仓"` (only the CMS tooltip string `预计美国仓库发货日期` existed) | `warehouse=None`. |
| **DIGIKEY** | Fabricated `ship_text="下单后立即发货"` (only marketing meta-description) | `ship_text=None`. |
| **DIGIKEY** | Fabricated `期货 / 工厂期货` row with `ship_text="原厂标准交货期 N 周"` | Whole row deleted. Real lead-time value kept in `extracted.lead_time` site-native field. |
| **RSONLINE** | Hardcoded `"RS 欧时仓"` warehouse name (page does not name a warehouse to anonymous visitors) | `warehouse=None`. New OOS path reads `"暂时缺货 / 2026年9月1日 发货"` from the Adobe data-layer `stockinfo` blob (status=OUT_OF_STOCK + future date). |
| **RSONLINE** | When no exact MPN match, picker fell back to first card → scraped unrelated parts (e.g. EMW3080 → ST B-U585I-IOT02A Discovery kit) | Exact → fuzzy alphanumeric substring → else `no_results`. |
| **ONEYAC** | `stock_breakdown=[]` when `stock_now_qty=0`, dropping the visible MOQ + 交期 | When stock=0 + 交期/MOQ visible, emit `label="期货"` row with `quantity=0`, `ship_text="交期 16W"`, `moq=…`. |
| **ONEYAC** | `min_order_qty=1` from `dynamicOrderMinNum` while page shows `最小包：2,500` (the operative MOQ) | `min_order_qty = max(dynamicOrderMinNum, minPack)`; both raw values preserved. |
| **FUTURE** | Cookie banner overlays the Pricing/Stock panel on screenshots | Playwright dismisses `button:has-text('Allow all cookies')` / `#onetrust-accept-btn-handler` before navigation. Data extraction was unaffected (innerText reads through the overlay) — fix is purely for screenshot legibility. |
| **FUTURE** | `factory_stock=-1` (parse-failure default) emitted as a real "Factory Stock" row | `_parse_qty` returns `None` for negative values; breakdown rows only emit when `isinstance(qty, int) and qty >= 0`. |
| **FUTURE** | `Future Electronics (Singapore)` row was a same-number duplicate of `Future Electronics (global)` — naive `SUM(stockpool_qty)` double-counted | Singapore region row suppressed unconditionally; the global row alone represents Future's stock. |
| **Rochester** | First-row click fallback when no exact match → silent wrong-part scrape (e.g. ESP32-WROOM-32E-N4 → Skyworks Si532; BTA12-800BWRG → Analog AD9394) | Bail with `no_results` when no `span.productName` text contains the input MPN. |
| **ICKEY** | doT.js price template `{{= it.rmb_price[i] }}` not yet hydrated when HTML was captured → `prices: []` for chips that had real tier prices | Polling loop waits for `re.search(r"[￥¥]\s*\d", html_now)` to appear before scraping. |
| **HQEW** | Returning 30 supplier rows per chip (the cap) — too noisy for cross-channel review | Cap lowered to **5 per chip total** (across all MPN variants), ranked by quantity descending. |

The complete `extracted.stock_breakdown[]` and all `site_*` fields are still kept in the per-cell JSON; only **scraper-invented labels** were removed. Cross-track downstream tools that consume the canonical 现货/期货 scalars (`stock_now_qty`, `stock_future_qty`) keep working unchanged.

---

## Canonical schema (unchanged from v2)

Every channel's per-variant record MUST emit these five fields. They are the cross-channel comparison layer:

| Field | Meaning | When source has no equivalent |
|---|---|---|
| `stock_now_qty` | 现货 quantity (immediately shippable) | `0` or `null` |
| `stock_now_ship_text` | 发货时间 string for 现货 | `null` |
| `stock_future_qty` | 期货/在途 quantity | `null` (unbounded factory order) or `0` |
| `stock_future_ship_text` | 发货时间 string for future stock | `null` |
| `stock_breakdown` | `[{label, warehouse, quantity, ship_text, [moq], [ships_from], [note]}, …]` covering every pool the site exposes | `[]` |

**Site-native fields** continue to live under `site_*` keys (`site_global_stock`, `site_factory_stock`, `stock_gd_warehouse`, `usableTransitNum`, `site_lead_time`, `site_order_min`, …). The canonical scalars are the interpretation layer; `site_*` is the raw site truth.

**MPN-variant grouping** continues to apply — fuzzy searches that return multiple distinct MPN strings produce one variant entry per MPN string, never aggregated.

---

## Folder layout (v3)

```
test/scraper/BatchTest_<YYYYMMDD>_<HH_MM_SS>/
├── batch_input.csv                   ← N rows (verbatim input chips)
├── batch_index.csv / .xlsx           ← warehouse-exploded long form (26 cols)
├── batch_index.json                  ← per (chip × source) record incl. extracted_best
├── batch_summary.md                  ← TL;DR + per-source pass rate + top-5 + mfr mismatches
├── failures.md                       ← non-ok cells grouped by source
└── Test_<safe_mpn>_<SOURCE>/         ← per-cell run folder
    ├── <safe_mpn>.json               ← canonical record (or parent for multi-variant)
    ├── <safe_mpn>_summary.md         ← rendered by common/_summary.py
    └── <variant_mpn>/                ← only for LCSC v3 / Future / multi-variant sources
        ├── <variant_mpn>.json
        ├── <variant_mpn>_product.html
        └── <variant_mpn>_product.png
```

Sanitisation rule: input MPN → `re.sub(r"[^A-Za-z0-9._-]", "_", mpn)`. Examples: `PIC16F18446T-I/SS` → `PIC16F18446T-I_SS`; `BT168GW,115` → `BT168GW_115`.

Multi-variant sources (LCSC, Future) keep per-variant subfolders. Single-record sources (Digikey, HQEW, RSONLINE, ONEYAC, ICKEY, Rochester) write the artifacts at the cell root.

---

## Detailed per-source notes

### 1. LCSC (立创商城 / szlcsc.com)

Unchanged engine from v2 (Chromium `--headless=new`, SSR `__NEXT_DATA__` + DOM right-panel for tier prices). Audit changes:

- `stock_breakdown` now emits a single aggregate `现货 N` row (no fabricated 广东仓 / 江苏仓 / SMT扩展库 sub-rows).
- New field `min_order_qty` extracted from visible `<span>起订量：N 个</span>` via Playwright JS injection. Distinct from the API field `min_buy_number` (which is often 1 even when page-visible MOQ is higher).
- Known flaky exception: `[WinError 5] 拒绝访问.: '_tmp_NNN' → …` from a Playwright temp-cleanup race on Windows. ~5 % of cells on the 51-chip sweep. Re-run usually clears.

### 2. Digikey (得捷电子, digikey.cn)

Unchanged engine. Audit changes (all "label was scraper-invented, page didn't say so"):

- `warehouse` field on the 现货 row: was `DigiKey 美国仓` → now `None`.
- `ship_text` on the 现货 row: was `下单后立即发货` → now `None`.
- 期货 row entirely removed. The raw lead-time number is still preserved as `extracted.lead_time` ("30 周") and `site_*` fields — only the warehouse-row dressing is gone.

Failure modes observed on the 51-chip sweep:
- `blocked` (Cloudflare `_abck` revoked mid-session) — intermittent; rerun clears.
- `failed` (status=failed; Digikey's search returned no `/products/detail/` link) — most common; the part legitimately isn't carried by Digikey. Not a scraper bug.

### 3. HQEW (华强电子网, hqew.com)

Unchanged engine (Playwright Chromium, `tr.ec-data` table scrape). Audit change: top-N cap reduced from 30 → **5** per chip across all MPN variants, ranked by quantity descending.

Site-model reminders (unchanged):
- Aggregates supplier listings; one row per supplier. `warehouse` = supplier name. Per-supplier prices gated behind login + 询价.
- Public price = single 云价格 (aggregate). Stored as the only `prices[]` tier.
- Fuzzy search — keeps `STM32G030F6P6` vs `STM32G030F6P6TR` vs `STM32G030F6P6 TR` vs `STM32G030F6P6,TR` as separate MPN variants.

### 4. Future Electronics (futureelectronics.com)

Engine change: Playwright Firefox unchanged, but the JS extractor now:
- Dismisses cookie banner (`Allow all cookies` / `Accept All` / `#onetrust-accept-btn-handler`) twice — once after homepage warmup, once after each detail-page navigation.
- Treats negative parsed quantities as `None` (Future renders `-1` for "unknown / not loaded" in some cases — looked like real data otherwise).
- **Drops the Singapore region row** (it's a same-number duplicate of Global Stock on the APAC site; naive `SUM(stockpool_qty) GROUP BY` would double-count).

`stock_breakdown[]` now emits at most 3 rows per Future cell: Global Stock, On Order, Factory Stock — and only when the page actually publishes a non-negative quantity.

### 5. RSONLINE (RS 欧时, rsonline.cn) — NEW since v2

- **Engine:** `curl_cffi` chrome131 only — no Playwright. Cheapest scraper in the stack.
- **Stock extraction:** Adobe analytics `_satellite_pageBottom` data layer. Look for `"stockinfo":{"date":"…","quantity":"…","status":"IN_STOCK"}`. Both `date` and `quantity` can be pipe-separated for batched promises ("15 件将从其他地点发货 | 另外 100 件将于 2026年5月25日 发货").
- **OOS path:** `stockinfo.quantity=""` + `status="OUT_OF_STOCK"` → `stock_now_qty=0`, `stock_now_ship_text="暂时缺货"`, `stock_future_ship_text="<date> 发货"` (when date present). No fabricated warehouse name.
- **Variant matching:** Exact MPN → fuzzy alphanumeric-substring → else `no_results`. Without the fuzzy guard, RS's category-related fallback list silently returned unrelated parts (EMW3080 → ST Discovery kit).
- **Known caveat:** rate-limited by RS WAF if hit too fast. Sweep on 2026-05-19 showed 5.9 % pass (3/51) due to WAF; isolated runs typically reach 70 %+. Add throttling or back off if you see all calls returning 2-3 KB empty pages.

### 6. ONEYAC (唯样商城, oneyac.com) — NEW since v2

- **Engine:** Playwright Firefox.
- **Stock:** `<span id="detail_inventory">N</span>`. `0` is a meaningful value (in-catalog OOS) — do NOT treat as missing data.
- **MOQ:** `min_order_qty = max(dynamicOrderMinNum, minPack)`. The two often agree (650, 1000) but when they disagree (起订量=1, 最小包=2500), the effective minimum a buyer can purchase is `minPack`.
- **Lead time:** Two surface forms — `<span>交期：</span><span>5天-7天</span>` (现货 SLA) or `<span>交期：</span><span>16W</span>` (期货 / factory lead time when stock=0). The `16W` shorthand needed a custom lead-time regex (`交期\s*(\d+)\s*W\b` → ×7 → days).
- **Price tiers:** `<div class="detailPri">` ONLY (not `c-proPri_lst`, which is recommended-products carousel).
- **OOS handling:** when stock=0 + 交期/MOQ visible, emit `label="期货", quantity=0, ship_text="交期 16W", moq=…` so the buyer can still see the factory option.

### 7. ICKEY (云汉芯城, ickey.cn) — NEW since v2

- **Engine:** Playwright Chromium. Marketplace aggregator — every listing is a **resale** from an upstream distributor (typically Digi-Key or 云汉芯城). The upstream is exposed in the page title `<MPN>（<distributor>）采购_价格_数据手册-云汉芯城 ICkey.cn`.
- **Hydration wait:** doT.js template `{{= it.rmb_price[i] }}` populates after JS runs. Poll for `[￥¥]\s*\d` to appear before scraping; otherwise `prices: []`.
- **Stock + MOQ:** `<span id="proMoq">N</span>`. Stock + ship in `货期：内地 成团后<span>10-14工作日</span>`. Delivery location `内地` and delivery_time `10-14工作日` extracted as separate fields.
- **Manufacturer:** `<script type="application/ld+json">` Schema.org `Product.manufacturer.name`.
- **Highest coverage (8/8 on the 8-chip × 8-source mini-batch)** of any source, but every record is a marketplace resale — treat the stock as upstream snapshot, not ICKEY-warehouse truth.

### 8. Rochester Electronics (rocelec.com) — NEW since v2

- **Engine:** Playwright Firefox + homepage warmup. Search URL: `https://www.rocelec.com/global-search/<MPN>` (the red-box "product inventory" search, not the blue-box site search).
- **Click logic:** find `span.productName` whose alphanumeric-normalized text **contains** the input MPN; navigate to `/part/<SalesforceID>-<MPN_no_punct>` via that click.
- **No-match guard:** when no productName matches the input, bail with `no_results`. Without this, Rochester's `/global-search/` returns category-related products and clicking the first row silently scraped unrelated parts.
- **Coverage:** very low on contemporary BOMs (5/51 on the sweep). Rochester's specialty is EOL / Last-Time-Buy / legacy stock. Useful as a tail source for legacy parts, not a default.

### 9. bom2buy via Opera session reuse — NEW 2026-05-20

- **Engine:** Playwright with `chromium.launch_persistent_context(executable_path=<opera.exe>, user_data_dir=<Opera profile>)`. The user's Opera install is driven directly — we don't extract cookies (bom2buy uses Chrome v20 App-Bound Encryption which can't be decrypted by non-Opera processes). Opera runs the captcha-cleared session and we simply read the rendered DOM.
- **Search URL:** `https://www.bom2buy.com/search?part=<MPN>&qty=1` (the parameter name is `part`, NOT `keyword` — a wrong-guess `keyword=` returns an empty page).
- **DOM:** `.exact-part-group-list > .distributor-results` is one MPN variant; each variant's `tbody tr` is a distributor row with `.td-distri / .td-stock / .td-delivery-place / .td-price / .td-min-pack` cells.
- **"No results" detection:** there is a hidden `<div class="exact-no-result hide" style="display:none">` template on EVERY search page — substring match of `没有找到` is a false-positive. Authoritative check: exactly when `.exact-part-group-list .distributor-results` is empty.
- **Pre-flight session check:** on startup, hit the homepage; if `captcha.bom2buy.com` is in `page.url` OR title is `Captcha`, the session is expired. The script raises `CaptchaRequired` and exits with code **3** (distinct from generic failure 2). The batch driver SHOULD treat exit-3 as "skip this source, finish other channels" rather than fail the run.
- **Operational caveats:**
  - Opera must be FULLY closed before scraping (Playwright takes exclusive lock on user-data-dir). The script auto-detects running `opera.exe` and refuses to start.
  - The user's IconCaptcha pass is needed every few hours/days; the script does not solve it.
  - Rate-limited by a per-MPN delay (3 s) and a longer pause every 50 cells (30 s) to avoid the secondary slider-captcha that bom2buy applies under load.
  - Headless mode does NOT work for Opera (launch hangs at 180 s); the visible window pops up briefly per launch.
- **Data richness:** bom2buy is a BOM aggregator. One typical record returns 10–40 distributor rows per MPN (Digi-Key + element14 + Mouser + Wuhan P&S + STMicro direct + RS + TME + Farnell + Verical + Future + …), each with `stock_qty`, multi-currency tier prices, region + lead time, MOQ, distributor SKU, authorized flag. This is **more structured than any other source we have**.
- **Pilot batch (10 MPNs, 2026-05-20):** 8 ok / 2 no_results. **Distinct** distributors per part after dedup: 6 (ESP32-WROOM-32E-N4) to 24 (IRLML5103TRPBF EOL). Pre-dedup row counts were higher (10–40) — many distributors carry multiple packaging variants of the same chip (different SKUs for tape/reel vs tray vs cut tape); we keep only the first row per distributor name to avoid double-counting stock. No-results cells (HT66F017-HF Holtek, LTST-C191KFKT-PH2 LiteOn LED) are genuine: bom2buy carries primarily Western-authorized parts.
- **Distributor dedup + per-row prices:** the canonical `stock_breakdown[]` has one entry per DISTINCT distributor name (first row kept); each entry carries its OWN `prices` tier list. bom2buy is the only source where each warehouse row has an independent tier structure — Digi-Key may have 9 tiers, Wuhan P&S 4 tiers, Mouser a single break at min_qty=2000. Downstream warehouse-exploded batch_index.csv exporters must read `stock_breakdown[i].prices` (NOT the cell-level top-level `prices[]`) when emitting per-warehouse rows from bom2buy cells.
- **Lifecycle status:** bom2buy exposes `Active` / `Transferred` / `Contact Manufacturer` / `Obsolete` — the most explicit lifecycle signal across all our sources.
- **Datasheet anchor:** the header datasheet link is sometimes `javascript:void(0)` (login-walled) — extraction returns `null` in that case rather than fabricating.

### 10. Mouser — ❌ still blocked

Unchanged from v2. Akamai BotManager `bm-verify` JS sensor. Use the API track (`api/scripts/api_mouser.py`) with the Search API v1 key in `api/.env`.

### 11. Arrow — ❌ still blocked

Unchanged from v2. Akamai BMP `_abck` sensor. Use the API track (`api/scripts/api_arrow.py`) once an Arrow API key is provisioned (not done as of 2026-05-19).

---

## Verification workflow

`scraper/doc/verification_guidance.md` (new since v2) — guidance for an out-of-process verifier session that cross-checks JSON against `*_product.png` screenshots and produces `VERIFICATION_REPORT.md` + `.xlsx` in the batch folder. Key principles encoded there:

- Compare **JSON ↔ screenshot** (summary.md is only an index that lists which fields to look at).
- Per-cell `cell` column is `Test_<safe_mpn>_<SOURCE>` for flat / nested patterns; `Test_<safe_mpn>_<SOURCE>/<chosen_variant>` for multi-variant cells.
- Verify **only the variant the batch's `pick_best_extracted` chose** (look up `returned_mpn` in `batch_index.csv`), not all 7 variants of a multi-variant cell.
- Strict verdict enum: `match` / `json_missing` / `json_wrong` / `screenshot_unclear`. Bias toward `screenshot_unclear` when uncertain — a false `json_wrong` makes the human chase a non-bug; `screenshot_unclear` invites a manual re-check at a known asymmetric cost.
- Cost mitigations: downscale PNGs to ≤1200 px wide with PIL before reading; pre-filter rows with `status ≠ ok`; process 2–3 cells per batch, appending rows to disk between batches.

---

## Key lessons additive to v2

Continuing the v2 list (12 items) with new lessons surfaced 2026-05-18 — 2026-05-19:

13. **Audit before you ship.** Every Chinese marketplace scraper had at least one hardcoded label that looked like real data but wasn't (RS 欧时仓, DigiKey 美国仓, 工厂期货, 下单后立即发货). The right check is to grep the rendered product HTML for the literal label string — if it's not there, the scraper is fabricating.
14. **`stock=0` ≠ "no data".** ONEYAC, RSONLINE, Future routinely report stock=0 alongside still-useful info (MOQ, factory lead time, "暂时缺货"). Conditional-on-`stock_now_qty` row emission (`if extracted["stock_now_qty"]:`) silently drops that info — use `is None` checks instead.
15. **Marketplace-search "first row" is dangerous.** RS, LCSC, Rochester, ONEYAC, Future, Digikey ALL exhibit the pattern: when no exact MPN match exists, the search returns category-related products. Pickers must require alphanumeric substring containment, not fall back to first-row.
16. **HTTP-parser-friendly stock fields are gold.** RS's Adobe data layer (`"stockinfo":{"quantity":"15 | 100","date":"2026-05-18 | 2026-05-25"}`) is a single regex away from full canonical extraction — no Playwright, no DOM walking. Look for `analytics`, `tealium`, `adobeDataLayer`, `_satellite` blobs first.
17. **Per-channel `warehouse` field meaning differs.** For RS / Rochester / LCSC / Digikey it's an actual warehouse (or `None`). For HQEW + ICKEY it's a supplier name (B2B marketplace pattern). For Future it's a stock pool ("Global Stock", "On Order", "Factory Stock"). Naive `SUM(stockpool_qty) GROUP BY (input_mpn, source)` will misbehave per source — see the per-source aggregation rules in `scraper/doc/batch_output_schema.md`.
18. **Channels in parallel ≠ chips in parallel.** ThreadPoolExecutor across the 8 channels per chip gives 50–60 % wallclock speedup with no rate-limit collisions (each channel hits a different domain). But parallel across **chips** would saturate any single domain. Keep chips sequential.
19. **Subprocess per call is the right isolation level for Playwright at this scale.** A hung szlcsc panel or wedged Firefox doesn't take down the batch — the timeout kills the subprocess cleanly and the next chip starts fresh. The cost (Python startup ×8 channels ×51 chips) is dominated by the actual page-load times.
20. **Windows `_tmp_NNN` race on Playwright temp dirs.** Concurrent Chromium contexts race on `%TMP%/_tmp_NNN` cleanup, raising `[WinError 5] 拒绝访问.`. ~5 % of LCSC cells on a 51-chip sweep. Not yet fixed — `--resume` clears it on rerun.

---

## Future work / backlog

Carried from v2:
- Mouser / Arrow CDP-attach (real-Chrome attach via `chromium.connect_over_cdp("http://localhost:9222")`).
- Future currency switching (currently APAC default SGD).
- LCSC XHR capture for tier prices (would replace the DOM right-panel scrape).
- Rate limiting framework (>10 parts/min territory).
- Spec parameter unification across sources.

New for v3:
- **`ships_from` population** — currently empty across all scraper rows (only ARROW on the API track populates it from `site_sources[].shipsFrom`). Adding country-of-origin parsing for Future + LCSC would close this gap.
- **HQEW per-supplier prices** — currently `extracted.prices` is the top-level 云价格 only. Each supplier row's actual quoted price requires login + 询价; not feasible without credentials.
- **RSONLINE rate-limit hardening** — single-token bucket with backoff would lift the 5.9 % sweep pass-rate back to the ~70 % isolated-run baseline.
- **Auto-recovery of LCSC `_tmp_NNN` exceptions** — wrap the affected step in retry-once-after-cleanup.
- **Audit pass for HQEW / ICKEY / Rochester** — only LCSC / DIGIKEY / RSONLINE / ONEYAC / FUTURE have had the "no-fabricated-labels" audit. The remaining three should be audited the same way before declaring v3 complete.

---

## Files of record

Per-source scrapers (8 working):
- `scraper/scripts/scrape_lcsc_v3.py` — LCSC (szlcsc.com) multi-variant.
- `scraper/scripts/scrape_digikey.py` — Digikey (digikey.cn).
- `scraper/scripts/scrape_hqew.py` — HQEW (hqew.com) supplier listings, top-5 cap.
- `scraper/scripts/scrape_future.py` — Future (futureelectronics.com) via Firefox, no Singapore row.
- `scraper/scripts/scrape_rsonline.py` — RSONLINE (rsonline.cn) via curl_cffi, Adobe data-layer parser.
- `scraper/scripts/scrape_oneyac.py` — ONEYAC (oneyac.com) via Firefox, OOS-aware breakdown.
- `scraper/scripts/scrape_ickey.py` — ICKEY (ickey.cn) marketplace aggregator.
- `scraper/scripts/scrape_rochester.py` — Rochester (rocelec.com), EOL-focused.

Per-source scrapers (blocked):
- `scraper/scripts/scrape_mouser_v2.py` — Akamai bm-verify; reference only.
- `scraper/scripts/scrape_arrow_v2.py` — Akamai _abck; reference only.

Batch driver + shared utilities:
- `scraper/scripts/batch_scraper_test.py` — orchestrator with `--mpns` / `--xlsx` / `--only` / `--resume` / `--sequential`, parallel channel dispatch by default.
- `scraper/scripts/_update_readme_status.py` — refreshes the `<!-- BEGIN AUTO:status -->` block in `scraper/README.md` **and** the `<!-- BEGIN AUTO:source_status -->` block in `scraper/doc/source_technical_reference.md` after a batch. Fires from three places: end of `batch_scraper_test.py`, the `.claude/hooks/readme_postupdate.py` PostToolUse hook, and manual invocation.
- `common/_summary.py` — `<MPN>_summary.md` renderer; dynamic extra columns (MOQ, batch_code, listing_date, …). Used by both scraper and api tracks.
- `common/_backfill_summaries.py` — one-shot util to regenerate every `*_summary.md` under `test/scraper/` and `test/api/`.

Documentation:
- `scraper/doc/scraper_report_v3.md` — **this file** (supersedes v2).
- `scraper/doc/scraper_report_v2.md` — superseded.
- `scraper/doc/scraper_report_v1.md` — superseded.
- `scraper/doc/batch_output_schema.md` — v2 schema for `batch_index.csv` (26 cols, warehouse-exploded, API-aligned).
- `scraper/doc/verification_guidance.md` — guidance for a follow-up session that audits JSON ↔ screenshot.
- `api/doc/batch_output_schema.md` — sibling reference; first 24 columns mirror this track.

Memory:
- `MEMORY.md` indexes the persistent feedback rules — `feedback_test_output_folder.md`, `feedback_stock_breakdown_fields.md`, `feedback_mpn_variant_grouping.md`, `feedback_site_native_fields.md`, `feedback_test_must_reach_detail_page.md` (new since v2 — every new-source feasibility probe must navigate to the product detail page, not stop at search).
- `project_scrape_state.md` — per-source channel status with method + gotchas + bug-fix history.
- `project_scrape_batch_state.md` — `batch_scraper_test.py` driver state + per-batch pass rates.
