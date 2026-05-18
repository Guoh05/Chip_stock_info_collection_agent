# Web Scraper Test Report v1 — 4 Distributor Sources

**Date:** 2026-05-17
**Test part:** STM32G030F6P6 (STMicroelectronics ARM Cortex-M0+ MCU, 20-TSSOP) — and HT66F017-HF for the original LCSC baseline.
**Stack:** Python 3.10.9, `curl_cffi` 0.15.0 (TLS impersonation), Playwright (Chromium) + `playwright-stealth`, BeautifulSoup/lxml.

## TL;DR

| Source | Status | Method that worked | Data quality | Bot-protection encountered |
|---|---|---|---|---|
| **LCSC** (szlcsc.com) | ✅ pass | Playwright `--headless=new` + `__NEXT_DATA__` SSR + DOM right-panel scrape | high | none |
| **Digikey** (digikey.cn) | ✅ pass | Playwright stealth `--headless=new` + `__NEXT_DATA__` parse | high | Cloudflare JS challenge (passable) |
| **Mouser** (mouser.cn / .com) | ❌ blocked | n/a | none | Akamai BotManager `bm-verify` (JS sensor) |
| **Arrow** (arrow.com) | ❌ blocked | n/a | none | Akamai BotManager `_abck` (JS sensor) + geo restriction on .cn variants |

Two of four channels yield structured product data (MPN, manufacturer, stock, full price tiers, parameters, datasheet, lifecycle). The two failures are both Akamai BotManager — the toughest commercial anti-bot, requires real browser JS to compute a sensor token.

---

## Detailed findings

### 1. LCSC (立创商城 / szlcsc.com) — ✅ working

- **Method:** Chromium launched with `--headless=new` + `--disable-blink-features=AutomationControlled`. Search at `so.szlcsc.com/global.html?k=<MPN>` — every search-result link tagged `s_s__` is scraped (covers all keyword matches, not just one). For each match, open `item.szlcsc.com/<id>.html` and read both:
  - **SSR `__NEXT_DATA__`** at `props.pageProps.webData` for stock numbers (`gdWarehouseStockNumber` = 现货, `productRecord.usableTransitNum` = 在途, `smtStockNumber` = SMT扩展库) and the 12 product parameters (`paramList`).
  - **Rendered DOM right-panel** (locate by `梯度` and `库存总量` text anchors) for the 6 price tiers and the user-facing 发货时间 SLA strings ("最快4小时发货", "3个工作日内发货"). These tiers are NOT in the SSR blob — they hydrate client-side and require `--headless=new` to render.
- **Folder layout:** parent `Test_<MPN>_LCSC_<ts>/` contains `parent_summary.md` (cross-variant table) + one subfolder per matched product (with its own `<MPN>_summary.md`, raw `__NEXT_DATA__`, product HTML/screenshot).
- **STM32G030F6P6 result (4 variants captured):**
  - STM32G030F6P6TR (C529330, 编带): 现货 33,839 + 在途 100,000 + SMT 39,886; 6 price tiers ¥4.84 → ¥2.74; 12 spec params.
  - STM32G030F6P6 (C724040, 管装): 0 现货, 738 SMT only.
  - OSHWHUB-STM32G030F6P6 (C5160686, community variant): out of stock.
  - LCYZB-127-V1 (C2887951, eval board): 123 现货.
- **Pitfall (resolved):** Legacy headless mode (`headless=True`) is detected — the right-panel stays as a Tailwind `animate-pulse` skeleton and tier prices/SLA text are unreachable. Always use `args=['--headless=new', '--disable-blink-features=AutomationControlled']`.

### 2. Digikey (得捷电子) — ✅ working

- **Method:** Playwright stealth headless Chromium → `/zh/products/result?keywords=<MPN>`. Digikey is fronted by Cloudflare Turnstile/Challenge ("请稍候" zh-CN, "Just a moment…" en); the JS interstitial resolves automatically in ~10s under a real browser. Exact-match search redirects to `/zh/products/detail/<mfr>/<MPN>/<digikey-id>`. We read `document.getElementById('__NEXT_DATA__')` and parse the JSON envelope at `props.pageProps.envelope.data`.
- **Why curl_cffi fails:** Cloudflare's challenge requires JS execution; curl_cffi cannot run JS, so it always sees the 403 challenge page.
- **Stock model:** Digikey doesn't have "in-transit" stock — beyond `priceQuantity.qtyAvailable` (现货 from Digikey USA warehouse), orders fall through to factory lead-time (`productOverview.standardLeadTime`, e.g. "30 周"). We map this onto the same 现货/期货 schema as LCSC: 现货 row = qtyAvailable + "下单后立即发货"; 期货 row = quantity null + "原厂标准交货期 30 周" (when `isBackOrderAllowed`).
- **STM32G030F6P6 result:** 497-STM32G030F6P6-ND, 现货 76,636 (DigiKey USA仓), 期货 lead-time 30 周, 9 price tiers ($1.58 @ 1pc → $0.76627 @ 5032pc), 23 spec parameters, package 20-TSSOP, lifecycle "在售". Run: `test/Test_STM32G030F6P6_DIGIKEY_20260517_01_13_35/`.
- **Cloudflare detection (resolved):** Title check must accept both "Just a moment" (en) and "请稍候" (zh-CN); pair with `len(html) > 50_000` as the cleared-page signal. Don't trust "MPN in HTML" — Cloudflare embeds the original keyword in the challenge body.

### 3. Mouser (贸泽) — ❌ blocked

- **Blocker:** Akamai BotManager `bm-verify`. Every HTTP request returns 200 with a small (~7 KB) meta-refresh challenge page containing a `bm-verify` token. Following the meta refresh returns HTTP 403.
- **Tried (all blocked, see `attempts` in run JSON):**
  - curl_cffi `chrome131` direct (mouser.cn) → bm_verify_challenge
  - curl_cffi `chrome131` with homepage warmup → bm_verify_challenge
  - curl_cffi `chrome146` warmup → bm_verify_challenge
  - curl_cffi `chrome131` on **mouser.com** (international) → bm_verify_challenge
  - curl_cffi `chrome131` mouser.com + warmup → bm_verify_challenge
  - curl_cffi `safari260` mouser.cn → bm_verify_challenge
- **Why every curl_cffi variant fails:** The bm-verify cookie value can only be computed by running an obfuscated JS sensor in a real DOM context. Pure TLS impersonation isn't enough. (Note: memory previously labeled this as DataDome — Mouser has switched to Akamai BMP, or always was on it.)
- **Run:** `test/Test_STM32G030F6P6_MOUSER_20260517_00_59_27/`.

### 4. Arrow (艾睿电子) — ❌ blocked

- **Blocker:** Akamai BotManager via `_abck` sensor cookie. Edge returns HTTP 403 (`server: AkamaiGHost`) on every product or search URL. After homepage warmup the session has `_abck` set to a denied-state value (`…~-1~…`), and product paths stay 403.
- **Tried:** 6 curl_cffi attempts across chrome131/safari260, zh/en variants, direct + search URLs, with/without homepage warmup — all 403.
- **Side observation:** `arrow.com/zh/` returns HTTP 404 with an 838 KB SPA shell (AEM/Adobe React); the shell does not contain product data — Arrow hydrates via XHR after JS runs. `china.arrow.com` and `arrow.com.cn` fail at the TLS layer (`TLSV1_ALERT_INTERNAL_ERROR`), likely geo routing.
- **Run:** `test/Test_STM32G030F6P6_ARROW_20260517_01_03_24/`.

---

## What worked, in one line each

- **TLS impersonation (`curl_cffi` chrome131):** Useful as a cheap first probe. Sufficient for sites that gate on TLS fingerprint only. Insufficient against Akamai BMP and Cloudflare JS challenges.
- **Playwright stealth (headless Chromium):** The reliable bypass for Cloudflare basic challenges. Stealth patches matter (`--disable-blink-features=AutomationControlled`, navigator-language overrides).
- **Read-the-state-instead-of-the-DOM:** Once a page renders, `window.__NUXT__` (LCSC) or `__NEXT_DATA__` (Digikey) gives a clean, fully-typed product record. Far more robust than CSS selectors.

## Output schema rule (mandatory)

Every successful scrape MUST populate these four canonical scalars plus a `stock_breakdown` row list, regardless of channel:

| Field | Meaning | When source doesn't have it |
|---|---|---|
| `stock_now_qty` | 现货 quantity (immediately shippable) | `0` |
| `stock_now_ship_text` | 发货时间 string for 现货 (e.g. "最快4小时发货") | `null` |
| `stock_future_qty` | 期货/在途 quantity | `null` (unbounded factory order) |
| `stock_future_ship_text` | 发货时间 string for future stock (e.g. "原厂标准交货期 30 周") | `null` |
| `stock_breakdown` | `[{label, warehouse, quantity, ship_text}, …]` covering every pool | `[]` |

`_summary.py` renders these into the **Stock** section of every per-variant summary. Adding a new distributor channel = mapping its native stock model onto these fields.

## Future considerations (read before next iteration)

1. **Akamai BotManager is the hard wall.** Defeating bm-verify / `_abck` without JS execution is not realistic with off-the-shelf libraries. If we must scrape Mouser/Arrow, the pragmatic options are:
   - Use Mouser's free Search API (`api.mouser.com`) — official, returns JSON. User said "no APIs for now" but reconsider.
   - CDP-attach Playwright to the user's already-running real Chrome (`chromium.connect_over_cdp("http://localhost:9222")`). Real Chrome is allow-listed by Akamai. Requires the user to start Chrome with `--remote-debugging-port=9222`.
   - Octopart API for Arrow stock (aggregator).
2. **Cloudflare detection must handle non-English challenge pages.** A check like `"Just a moment" in title` misses the zh-CN variant `请稍候`. The current Digikey script accepts both plus length threshold > 50 KB as the cleared-page signal.
3. **Length is the most reliable pass signal.** Bot-challenge pages are <40 KB; real product pages are 200–300 KB. A 200-status with 7 KB body is almost always a challenge, not a product.
4. **Anti-bot vendor can change without notice.** Memory had Mouser on DataDome; today it's Akamai bm-verify. Always re-probe before assuming the prior strategy still applies.
5. **`window.__NUXT__` and `__NEXT_DATA__` are the right extraction targets.** Don't waste time on CSS selectors when the SPA already ships a normalized data envelope.
6. **Hardcoded `lcsc_vid` URL params.** LCSC product URLs include a session token in the URL — fine for one-shot extraction but unsafe to cache long-term.
7. **Stock numbers are strings on Digikey** (`"76,636"`) — always parse with comma-stripping. Prices are dollar-prefixed strings (`"$1.58000"`) — strip `$` before any arithmetic.
8. **Per-run folder discipline:** Every scrape writes to `test/Test_<MPN>_<CHANNEL>_<YYYYMMDD>_<HH>_<MM>_<SS>/` including a `<MPN>_summary.md` human-readable view. Backfill helper: `scripts/_backfill_summaries.py`.
9. **Rate limiting / IP hygiene:** Not yet implemented. If we ever scrape at volume (>10 parts/minute) we must add per-domain throttling, otherwise we'll get IP-banned regardless of bot-protection bypass.
10. **Output schema is the web-scraper skill's standard:** `method`, `data_quality` (high/medium/low/none), `paywall` (always `none` for distributors), `attempts` (cascade log for forensics). Worth keeping when adding new channels.

## Files of record

- `scripts/scrape_lcsc_v2.py` (working)
- `scripts/scrape_digikey.py` (working)
- `scripts/scrape_mouser_v2.py` (cascade w/ attempts log; blocked)
- `scripts/scrape_arrow_v2.py` (cascade w/ attempts log; blocked)
- `scripts/_summary.py` (shared summary.md generator — called from every script)
- `scripts/_backfill_summaries.py` (one-shot util to regenerate summaries for existing runs)
- `requirements-scraper.txt` (pinned deps)
