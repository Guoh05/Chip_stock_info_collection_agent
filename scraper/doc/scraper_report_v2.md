# Web Scraper Test Report v2 — 6 Distributor Sources

**Date:** 2026-05-17 (supersedes v1)
**Test parts used:** STM32G030F6P6 (32-bit MCU), BT168GW,115 (SCR), ATXMEGA32E5-ANR (8-bit MCU), PIC16F18446T-I/SS (8-bit MCU). Earlier baseline: HT66F017-HF on LCSC.
**Stack:** Python 3.10.9, `curl_cffi` 0.15.0 (TLS impersonation), Playwright (Chromium **and Firefox**) + `playwright-stealth`, BeautifulSoup/lxml.

## TL;DR

| # | Source | Status | Method | Quality | Bot-protection encountered |
|---|---|---|---|---|---|
| 1 | **LCSC** (szlcsc.com — China site) | ✅ pass | Playwright Chromium `--headless=new` + `__NEXT_DATA__` SSR + DOM right-panel scrape | high | none |
| 2 | **Digikey** (digikey.cn) | ✅ pass | Playwright stealth Chromium + `__NEXT_DATA__` envelope parse | high | Cloudflare JS challenge (passable) |
| 3 | **HQEW** (华强电子网, hqew.com) | ✅ pass | Playwright Chromium `--headless=new` + `tr.ec-data` table scrape | high | jsjiami.com.v7 JS obfuscator (Playwright passes) |
| 4 | **Future Electronics** (futureelectronics.com) | ✅ pass | Playwright **Firefox** + label-driven text scrape | high | Akamai BMP on `/` (homepage); Chromium gets HTTP/2 reject — Firefox passes |
| 5 | **Mouser** (mouser.cn / .com) | ❌ blocked | n/a | none | Akamai BotManager `bm-verify` (JS sensor) |
| 6 | **Arrow** (arrow.com) | ❌ blocked | n/a | none | Akamai BotManager `_abck` (JS sensor) |

**4 / 6 working.** Two switched protection vendors between v1 and v2 attempts (Mouser was thought to be DataDome; turned out Akamai). The two stubborn failures are both Akamai BMP — same wall.

---

## Output schema (canonical, all channels)

Every channel's per-variant record MUST emit these five scalar/structure fields, regardless of whether the source uses 现货/期货 terminology natively. They are the **interpretation layer** that lets a buyer compare distributors at a glance.

| Field | Meaning | When source has no equivalent |
|---|---|---|
| `stock_now_qty` | 现货 quantity (immediately shippable) | `0` |
| `stock_now_ship_text` | 发货时间 string for 现货 (e.g. "最快4小时发货", "Ships immediately") | `null` |
| `stock_future_qty` | 期货/在途 quantity | `null` (unbounded factory order) or `0` |
| `stock_future_ship_text` | 发货时间 string for future stock (e.g. "Factory Lead Time: 4 Weeks") | `null` |
| `stock_breakdown` | `[{label, warehouse, quantity, ship_text, note?}, …]` covering every pool the site exposes | `[]` |

**Site-native fields are also preserved alongside** under `site_*` keys (`site_global_stock`, `site_factory_stock`, `gdWarehouseStockNumber`, `usableTransitNum`, etc.). The breakdown table uses the site's own labels (e.g. "Global Stock" / "Factory Stock" for Future, not "现货" / "期货"). The interpretive 现货/期货 framing belongs in the summary's "Note on stock model" block.

**MPN-variant grouping rule.** When a search returns multiple distinct MPN strings (e.g. `STM32G030F6P6` returns both base part and `STM32G030F6P6TR`), they are different products and are listed separately. Never aggregate across variant strings.

`_summary.py` consumes these fields directly to render the Stock section and per-variant breakdown table; adding a new channel = mapping its native model onto these fields.

---

## Detailed findings

### 1. LCSC (立创商城 / szlcsc.com) — ✅ working

**Switched to the China site (szlcsc.com) at user's request 2026-05-17.** The previous v1 implementation used `lcsc.com` (international Nuxt store); v2 uses `item.szlcsc.com` (Chinese Next.js stack).

- **Method:** Chromium launched with `--headless=new` + `--disable-blink-features=AutomationControlled`. Search at `so.szlcsc.com/global.html?k=<MPN>` — every result link tagged `s_s__` is scraped (covers all keyword matches; `from=kw` is dropped as recommendation/cross-sell). For each match, open `item.szlcsc.com/<id>.html` and read both:
  - **SSR `__NEXT_DATA__`** at `props.pageProps.webData` → stock numbers (`gdWarehouseStockNumber` 广东仓 = 现货, `productRecord.usableTransitNum` = 在途, `smtStockNumber` = SMT扩展库, gated by `isDisplayUsableTransitNum`), 12+ product parameters (`paramList` — keys are `parameterName` / `parameterValue`, NOT `paramName`/`paramValue` like the legacy Nuxt site).
  - **Rendered DOM right-panel** (locate by `梯度` and `库存总量` text anchors) → 6 price tiers + user-facing 发货时间 strings ("最快4小时发货", "3个工作日内发货"). These tiers are NOT in the SSR blob — they hydrate client-side and require `--headless=new` to render. Legacy headless leaves the panel as a Tailwind `animate-pulse` skeleton.
- **Folder layout:** `Test_<MPN>_LCSC_<ts>/parent_summary.md` + per-variant subfolders (each carries its own `<MPN>.json`, `<MPN>_summary.md`, raw `__NEXT_DATA__`, product HTML/screenshot).
- **Cross-channel mapping:** 现货 = `gdWarehouseStockNumber + jsWarehouseStockNumber`; 期货/在途 = `usableTransitNum`. Ship-text strings come from the DOM right-panel, not constants.

**Test results:**

| Query | Variants captured | Highest-stock variant |
|---|---|---|
| STM32G030F6P6 | 4 (TR, base, OSHWHUB community, LCYZB eval board) | STM32G030F6P6TR: 现货 33,839 + 在途 100,000 + SMT 39,886; 6 tiers ¥4.84→¥2.74 |
| BT168GW,115 | 1 | C256448: 现货 10,505 + SMT 10,509; 3 tiers ¥1.82→¥1.20 |

### 2. Digikey (得捷电子, digikey.cn) — ✅ working

- **Method:** Playwright stealth Chromium → `/zh/products/result?keywords=<MPN>`. Cloudflare interstitial ("请稍候" zh-CN, "Just a moment…" en) resolves automatically in ~10s under a real browser. Exact-match search redirects to `/zh/products/detail/<mfr>/<MPN>/<digikey-id>`. Parse `__NEXT_DATA__` → `props.pageProps.envelope.data` (`productOverview`, `priceQuantity`, `productAttributes.attributes`, `quantityTable`).
- **Stock model:** Digikey has no in-transit pool. Beyond `priceQuantity.qtyAvailable` (现货 from Digikey US warehouse), orders fall through to factory lead-time (`productOverview.standardLeadTime`, e.g. "30 周"). We map: 现货 row = `qtyAvailable` + "下单后立即发货"; 期货 row = quantity `null` (unbounded) + "原厂标准交货期 N 周" (when `isBackOrderAllowed`).
- **Cloudflare detection:** title check must accept both "Just a moment" (en) and "请稍候" (zh-CN); pair with `len(html) > 50_000` as the cleared-page signal. Don't trust "MPN in HTML" — Cloudflare embeds the original keyword in the challenge body.
- **Product page CF wait:** the search-results page clears CF quickly, but the subsequent product navigation may take 20–30s. The post-loop guard `cloudflare_persisted_on_product` blocks a silent fall-through when the wait isn't enough.
- **Why curl_cffi fails:** Cloudflare's challenge requires JS execution; curl_cffi cannot run JS, so it always sees the 403 challenge page.

**Test results:**

| Query | Captured | Detail |
|---|---|---|
| STM32G030F6P6 | 497-STM32G030F6P6-ND | 现货 76,636 + 期货 30 周; 9 tiers $1.58→$0.766; 23 specs |
| BT168GW,115 | 1740-1084-2-ND | 现货 4,424 + 期货 26 周; 4 tiers $0.66→$0.198; 19 specs |

### 3. HQEW (华强电子网, hqew.com) — ✅ working (B2B marketplace)

**Different site model from the rest.** HQEW aggregates listings from many independent suppliers — there is no central inventory. Each row is one supplier's own stock; per-supplier prices are gated behind login + 询价 (request quote). The only public price is the top-of-page aggregate **云价格**.

- **Method:** Playwright Chromium `--headless=new`. Search URL: `https://s.hqew.com/<MPN>.html`. Parse `tr.ec-data` rows for the supplier table (12-column layout: ad-flag / supplier+badge / — / listed-MPN / brand / batch-code / quantity / package / warehouse city / transaction note / listing date / —).
- **curl_cffi blocked** by `jsjiami.com.v7` obfuscated JS challenge (always a ~1KB JS shell). Recorded in `attempts`.
- **Stock mapping:** 现货 = SUM of `quantity` across top-N supplier listings (cap = 30); 期货 = `null` (no concept on hqew). Ship-text = "供应商现货 (具体发货请询价)". `stock_breakdown` has one row per supplier, with **supplier name as `warehouse`** and the listing's batch-code, MOQ, listing date, and 备注 (transaction note) as extra columns.
- **MPN variants:** hqew search is fuzzy. Querying `STM32G030F6P6` returns rows for the base part AND `STM32G030F6P6TR` (tape-and-reel). They are surfaced as separate variants. Different separators (`BT168GW,115` vs `BT168GW，115` full-width vs `BT168GW 115` space vs `BT168GW115`) are also kept distinct — don't normalize.

**Test results:**

| Query | Total listings | Variants | Highest-stock variant |
|---|---|---|---|
| STM32G030F6P6 | 386 (top 30 captured) | 4 distinct MPN strings | TR: 44 listings, sum 876,298 units (云价格 ¥5.7) |
| BT168GW,115 | 24 (all captured) | 5 distinct MPN strings | Canonical: 20 listings, sum 5,132,303 (one supplier alone holds 3.87M) |

### 4. Future Electronics (futureelectronics.com) — ✅ working

**The HTTP/2 fingerprint wall — Firefox bypasses it.** Future is fronted by Akamai BMP exactly like Arrow. The Akamai-rejected target is Playwright **Chromium's** HTTP/2 frame ordering (`ERR_HTTP2_PROTOCOL_ERROR`). Playwright **Firefox** has a different HTTP/2 fingerprint that is allow-listed → cleanly through.

- **Method:** `p.firefox.launch(headless=True)`. Search URL: `https://www.futureelectronics.com/search?text=<MPN>&q=<MPN>:searchRelevance`. Each search-result row anchor uses class `a.product__list--code` and links to `/p/<category>/<mpn-lower>-<mfr-lower>-<id>`. For each link, visit the detail page and scrape the rendered text body line-by-line.
- **Stock pools (Future's own labels):** `Global Stock` (Future's own warehouses globally — interpretation: 现货), `<Region>:` (e.g. `Singapore:` on the APAC site — a regional slice of Global Stock), `On Order:` (already reserved), `Factory Stock:` (the site's own definition: *"Inventory held at our manufacturer's warehouse. Subject to availability and transit time."* — interpretation: 期货/在途), `Factory Lead Time:` (e.g. "4 Weeks"). All four pools are surfaced as `stock_breakdown` rows using the site's English labels; the canonical 现货/期货 mapping happens in the scalars.
- **curl_cffi:** homepage `/` is blocked by Akamai sensor (`sec-if-cpt` interstitial), but other paths return SPA shells with no product data. Used only as a probe for the `attempts` log.
- **Folder layout:** parent + per-variant subfolders, same as LCSC v3.
- **Two parsing gotchas:** (1) `Factory Stock:` label appears TWICE in the rendered body (section heading + row label) — handled via "skip duplicate label" rule. (2) Lines around the Datasheet anchor and `<N> per <FORM>` use **non-breaking space (`\xa0`)**, not regular space — regex must use `\s` (which matches NBSP in JS) and label-based lookups can't assume the label has a value (some pages have empty `Date Code:` followed by a section header — guard with `isSectionHeaderLike` returning `null`).

**Test results:**

| Query | Variants | Highlight |
|---|---|---|
| ATXMEGA32E5-ANR | 2 (close-but-not-exact: AU + M4U) | AU: 现货 0 / Factory 4,500 (4 Weeks); SGD $4.03→$3.78 (5 tiers); 16 specs. M4U: Factory 46,060 |
| PIC16F18446T-I/SS | 1 (exact) | 现货 0 / Factory 0, lead time 5 Weeks; SGD $1.7223 @1600+; Reel of 1,600; HTS 8542.31.00, ECCN 3A991.a.2 |

### 5. Mouser (贸泽) — ❌ still blocked

- **Blocker:** Akamai BotManager `bm-verify`. All requests return 200 with a small (~7 KB) meta-refresh challenge page; following the meta refresh returns 403. (v1 memory said DataDome — turned out to be Akamai now, or possibly always was.)
- **Tried:** curl_cffi with 6 profiles/sessions (chrome131/146/safari260, both `.cn` and `.com`, with/without homepage warmup) — all `bm_verify_challenge`.
- **Path forward (not implemented):** CDP-attach to user's running Chrome, or Mouser Search API (free key, official). User scope: no APIs for now.

### 6. Arrow (艾睿电子) — ❌ still blocked

- **Blocker:** Akamai BMP `_abck` sensor. All product / search paths return HTTP 403 (`server: AkamaiGHost`). Homepage warmup seeds `_abck` cookies in the denied state (`…~-1~…`) and product paths stay 403.
- **Side observations:** `arrow.com/zh/` returns 404 with an 838 KB SPA shell (AEM/React) that hydrates via XHR after JS runs. `china.arrow.com` and `arrow.com.cn` fail at the TLS layer (`TLSV1_ALERT_INTERNAL_ERROR`) — geo routing issue from CN ISP.
- **Path forward:** CDP-attach to user's logged-in Chrome (Akamai allow-lists real Chrome), or Octopart API (Octopart aggregates Arrow stock).

---

## What worked, condensed

- **TLS impersonation (`curl_cffi` chrome131)** — useful as a cheap first probe. Defeats: nothing in this set on its own. Useful for: Future Electronics search-shell scrape (just to record the SPA-only outcome in `attempts`).
- **Playwright Chromium with `--headless=new`** — the workhorse. Required for LCSC (the szlcsc right-panel won't hydrate without it), Digikey (Cloudflare basic challenge), HQEW. Pair with `--disable-blink-features=AutomationControlled`.
- **Playwright Firefox** — the Akamai HTTP/2 bypass. Required for Future Electronics. Chromium gets `ERR_HTTP2_PROTOCOL_ERROR` on the homepage and all subsequent navigation; Firefox passes cleanly.
- **Read-the-SSR-state, not the DOM.** Once a page renders, `__NUXT__` (legacy LCSC), `__NEXT_DATA__` (Digikey, szlcsc) gives a clean fully-typed product envelope. The DOM is only the right answer when SSR doesn't have what you need (LCSC right-panel tier prices; Future's whole product page is DOM-only).
- **Per-variant subfolders.** LCSC v3, Future use a parent `parent_summary.md` (overview table) plus one subfolder per matched MPN (each with its own `<MPN>_summary.md`). HQEW uses a single parent with per-MPN-variant tables embedded (because HQEW listings are flat rows, not separate product pages).

---

## New since v1 — non-obvious lessons (read before adding a new channel)

1. **Modern headless ≠ legacy headless.** szlcsc's price panel is detected and suppressed under legacy `headless=True`. Always launch with `args=['--headless=new', '--disable-blink-features=AutomationControlled']` and verify the right panel hydrates before screenshotting.
2. **Chromium HTTP/2 fingerprint is on Akamai's bad list. Firefox isn't (yet).** Whenever a site front-loads with `ERR_HTTP2_PROTOCOL_ERROR` or `bm-verify`, try `p.firefox.launch(...)` before declaring it blocked.
3. **Anti-bot vendor swaps without notice.** v1 had Mouser on DataDome; v2 finds it on Akamai bm-verify. Always re-probe before assuming the prior strategy still applies.
4. **Cloudflare non-English titles.** A check like `'Just a moment' in title` misses zh-CN's `请稍候` (and others). Use a title-set + a length threshold (>50 KB = real page).
5. **NBSP in rendered text.** Lines like `1600 per Reel` use non-breaking spaces. JS `\s` matches NBSP, but a literal `' '` doesn't. Datasheet anchors on Future use ` Datasheet` (not space-Datasheet) — match on `/Datasheet$/`, not `' Datasheet'`.
6. **Section headers in label-driven scrapes.** Future's `Date Code:` may be followed immediately by a `… Section` divider with no value in between. Add an `isSectionHeaderLike` skip rule that **returns `null`** (not "keep walking") so the walker doesn't claim a later section's content as the value.
7. **Variant labels vary per page.** Future uses `Package Qty:` on some product pages and `Standard Pkg:` on others (sometimes neither). Prefer regex-on-body (e.g. `^<digits> per <Word>$`) over label-based lookup when the label is unstable.
8. **Don't normalize MPN variant strings.** Treat `BT168GW,115` (comma), `BT168GW，115` (full-width comma), `BT168GW 115` (space), `BT168GW115` (no separator) as distinct. They may be true variants or just supplier data-entry conventions — surface them all and let the user decide.
9. **Some sites have no spec table for some MPNs.** PIC16F18446T-I/SS on Future Electronics has zero spec parameters — the page just doesn't include a Technical Attributes section. `parameters: []` is a valid result, not a parser bug. Same applies to HQEW (never has specs).
10. **Site-native ≠ canonical.** Always store the site's own field names alongside the canonical 现货/期货 mapping (`site_global_stock`, `site_factory_stock`, `site_factory_lead_time` for Future; `gdWarehouseStockNumber`, `usableTransitNum` for LCSC). Adds auditability — when a buyer questions a number, you can show them the exact site label it came from.
11. **B2B marketplaces shift the schema.** HQEW (`stock_breakdown` rows = suppliers, not warehouses) and any future Octopart-style aggregator won't fit the "single distributor with N stock pools" model. Keep the canonical schema permissive enough to accept either shape.
12. **Cap supplier lists.** HQEW returns hundreds of listings per popular MPN. Cap `stock_breakdown` at top-N (currently 30) and expose `total_listings_count` so the consumer knows there's more.

---

## Future work / next-iteration backlog

- **Mouser / Arrow:** Try CDP-attach to user's real Chrome (`chromium.connect_over_cdp("http://localhost:9222")`). Real Chrome is allow-listed by Akamai. Requires user to start Chrome with `--remote-debugging-port=9222`.
- **Future currency:** Currently driven by APAC site default (SGD). For batch USD comparison, switch landing URL or set the country/currency cookie.
- **LCSC tier prices via XHR capture:** v3 reads tiers from rendered DOM. If LCSC ever ships them via a public `/get/price/V2` XHR, capturing that response would be cleaner than DOM scraping.
- **Rate limiting:** Not yet implemented. Volume scraping (>10 parts/min) will need per-domain throttling regardless of bot-protection bypass.
- **Spec parameter unification:** Cross-channel comparison of specs (LCSC's `paramList`, Digikey's `productAttributes.attributes`, Future's table) is currently ad-hoc. A canonical spec-key dictionary would help BOM-level comparisons.

## Files of record

- `scraper/scripts/scrape_lcsc_v3.py` — szlcsc.com multi-variant, current LCSC.
- `scraper/scripts/scrape_digikey.py` — digikey.cn.
- `scraper/scripts/scrape_hqew.py` — hqew.com supplier listings, per-MPN-variant grouping.
- `scraper/scripts/scrape_future.py` — futureelectronics.com via Firefox engine.
- `scraper/scripts/scrape_mouser_v2.py` — cascade with attempts log; blocked.
- `scraper/scripts/scrape_arrow_v2.py` — cascade with attempts log; blocked.
- `common/_summary.py` — shared `<MPN>_summary.md` generator; renders dynamic extra columns (MOQ, 备注, batch_code, listing_date) when breakdown rows carry them. Used by both scraper and api tracks.
- `common/_backfill_summaries.py` — one-shot util to regenerate every summary under `test/scraper_test/` and `test/api_test/` against the current template (walks one level deep for per-variant subfolders).
- `scraper/requirements.txt` — pinned deps for the scraper track.
- `scraper/doc/scraper_report_v1.md` — superseded by this report.
- Memory: `MEMORY.md` indexes the three feedback rules — `feedback_test_output_folder.md` (folder convention), `feedback_stock_breakdown_fields.md` (canonical schema), `feedback_mpn_variant_grouping.md` (variant grouping), `feedback_site_native_fields.md` (preserve site-native wording).
