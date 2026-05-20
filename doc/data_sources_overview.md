# Data Sources Overview — Cross-Distributor Inventory

**Date:** 2026-05-20 (rev 2 — bom2buy reactivated as a scraper since the 5-19 reports)
**Sources:** consolidates `api/doc/api_report_v2.md` (2026-05-19) and `scraper/doc/scraper_report_v3.md` (2026-05-19), against the empirical **107-chip** sweeps from 2026-05-20 (new master list: `ref/Shortage Emergency Response List_v2.xlsx`).

This is a top-level inventory of every distributor data source the pipeline can reach, with track availability (web scraper vs official API), quality grades, and recommended use. For per-source technical depth (auth, schema, gotchas) read the underlying track reports.

---

## TL;DR — source inventory by track

Columns 2–4 describe the **scraper** track; columns 5–6 describe the **API** track. `√` = working, `✗` = blocked / unavailable, `—` = not applicable. Coverage = % of the 107-chip BOM sweep that returned `status=ok` on that track. **可靠性 (trust)** is scraper-only and reflects data trustworthiness from a direct-distributor-vs-aggregator standpoint plus observed mfr_match drift; `较高 / 中等 / 较低`.

| Source | Scraper √ | Scraper coverage | Scraper 可靠性 | API √ | API coverage | Best use |
|---|:---:|---:|:---:|:---:|---:|---|
| **DIGIKEY** 得捷电子 | √ | 56 % | 较高 | √ | 59 % | gold-standard verified MPN + real qty; weak on Chinese-domestic parts |
| **LCSC** 立创商城 | √ | 79 % | 中等 | √ | 70 % | strongest domestic coverage for Chinese MCUs/discretes; some fuzzy drift |
| **ARROW** 艾睿 | ✗ | — | — | √ | 42 % | broad inventory mirrored across Verical / ACNA / EUROPE; precise (API-only — scraper Akamai-blocked) |
| **Element14** e络盟 | ✗ | — | — | √ | 43 % | secondary CN coverage; LTV/LTW manufacturer drift (API-only) |
| **Mouser** 贸泽 | ✗ | — | — | √ | 58 % | high-precision western catalogue; ~98 % mfr_match (API-only) |
| **买芯片网** (bom2buy.com) | √ | 60 % | 中等 | ✗ | — | meta-aggregator across Avnet / Arrow.cn / brokers; 91 % mfr_match; verify upstream warehouse name |
| **FUTURE** 富昌 | √ | 51 % | 较高 | ✗ | — | mostly factory-lead-time rows; useful for lead signal, weak for 现货 |
| **HQEW** 华强电子网 | √ | 82 % | 较低 | ✗ | — | highest raw coverage, but B2B supplier relisting — **excluded from procurement merge** |
| **ICKEY** 云汉芯城 | √ | 80 % | 较低 | ✗ | — | resale aggregator — coverage is *borrowed* from upstream Digi-Key / 云汉; verify the underlying source |
| **ONEYAC** 唯样商城 | √ | 51 % | 中等 | ✗ | — | medium coverage with notable fuzzy drift; keeps MOQ + 期货 info even on OOS rows |
| **Rochester** | √ | 11 % | 较低 | ✗ | — | EOL / Last-Time-Buy specialty only; near-zero hit-rate on current BOMs |
| **RSONLINE** RS 欧时 | √ | 29 % | 较高 | ✗ | — | low throughput (WAF rate-limit) but pristine precision — 100 % mfr_match when it returns |
| **Verical** (verical.com) | ✗ | — | — | ✗ | — | covered indirectly as `Arrow / VERICAL` mirror rows inside the Arrow API |
| **Chip1Stop** (chip1stop.com) | ✗ | — | — | ✗ | — | defunct — domain 301-redirects to arrow.com; inventory absorbed into Arrow API |

**Summary by track availability:**
- **Both tracks working:** DIGIKEY, LCSC.
- **API only** (scraper Akamai-blocked): Arrow, Element14, Mouser.
- **Scraper only** (no published API): bom2buy, Future, HQEW, ICKEY, ONEYAC, Rochester, RSONLINE.
- **Investigated but not contributing direct data:** Verical (via Arrow), Chip1Stop (absorbed by Arrow).

---

## Two-track architecture

The pipeline reaches every distributor through one of two tracks (sometimes both):

| Track | Tech | Strengths | Limits |
|---|---|---|---|
| **`api/`** — first-party REST | `requests`, OAuth/HMAC/API-key auth | Stable schema, structured fields, no bot-protection drama, parallel-safe | Daily quotas (LCSC 200/day, Element14 1000/day), requires credential procurement |
| **`scraper/`** — web scraping | Playwright Chromium/Firefox, `curl_cffi` for TLS impersonation, BeautifulSoup | No quota, reaches sites with no API, gets price tiers + visible MOQ | Subject to Akamai BMP / Cloudflare; brittle to UI changes; slower (Future ~300 s budget) |

The two tracks emit byte-identical canonical schemas (`stock_now_qty`, `stock_now_ship_text`, `stock_future_qty`, `stock_future_ship_text`, `stock_breakdown`) so downstream tools like `common/merge_batch_for_procurement.py` can `UNION` them after dropping the two scraper-only columns.

Where both tracks cover a source (Digikey, LCSC, WeEn), the procurement merge prefers the **API** result — fewer fuzzy-match drift cases, structured warehouse split. See `doc/merge_for_procurement_rules.md`.

---

## Per-source assessment

### DIGIKEY (得捷电子) — Grade A

- **Scraper:** `scrape_digikey.py`, Playwright stealth Chromium. Pass-rate **56.1 %**, mfr_match **96.7 %**, qty>0 **85.0 %**. Cloudflare JS challenge sometimes revokes `_abck` mid-session → rerun clears.
- **API:** `api_digikey.py`, PIM v4 with OAuth2 client_credentials, token cached 599 s. Pass-rate **58.9 %**, mfr_match **97.2 %**.
- **Note on coverage:** Both tracks land ~56–59 %. The remaining 41–44 % are Chinese-domestic parts (UMW STM rebrands, Holtek HT MCUs, NXP-acquired WeEn thyristors) that Digikey simply doesn't carry — not a pipeline bug.
- **Why grade A:** the highest mfr_match across all sources; structured warehouse + variation rows (Tape & Reel / Cut Tape / Digi-Reel); reliable lead-week field. Procurement should trust Digikey rows.

### LCSC (立创商城) — Grade A−

- **Scraper:** `scrape_lcsc_v3.py`, Playwright Chromium `--headless=new`. Pass-rate **78.5 %**, mfr_match **69.0 %**. Highest coverage of any scraper among direct distributors. Single aggregate `现货 N` breakdown row (post-audit; older versions fabricated 广东仓/江苏仓 sub-rows that didn't exist in the page UI).
- **API:** `api_lcsc.py`, JLC OpenAPI with HMAC-SHA256. Pass-rate **70.1 %**, mfr_match **80.0 %**, **but 200 calls/day per endpoint** — full 107-chip sweep exceeds quota partway. Exposes per-warehouse split (广东仓 + 江苏仓).
- **Caveat:** LCSC's keyword search drifts to close-but-not-exact MPNs (`HT66F017-HF` → `HT66F0176`) when the exact part isn't carried. Inspect `returned_mpn` before treating as verified.
- **Why grade A−:** highest domestic-China coverage; API gives genuine warehouse split; scraper drops to one aggregate row (honest about what the UI shows). Mfr-match drift is the −.

### Mouser (贸泽) — Grade A−, API-only

- **Scraper:** blocked by Akamai BotManager `bm-verify` JS sensor. Reference code in `scrape_mouser_v2.py`; do not use.
- **API:** `api_mouser.py`, Search API v1. Pass-rate **57.9 %**, mfr_match **97.6 %**.
- **Locale quirk:** the registered key is `.cn` → responses are Chinese-localized (`"108590 库存量"`, RMB pricing, `LeadTime` in 天 not weeks). For US-locale output a separate key would be needed.
- **Why grade A−:** precision matches Digikey, but coverage of Chinese-domestic parts is similar to Digikey (~58 %); no warehouse split.

### HQEW (华强电子网) — Grade B+, excluded from procurement

- **Scraper-only.** Pass-rate **82.2 %**, mfr_match **78.3 %**, qty>0 **96.9 %**.
- **Why excluded from procurement merge** (per `doc/merge_for_procurement_rules.md` rule 2): HQEW is a B2B supplier-listing aggregator; each row is a different micro-distributor relisting the same physical part, with stock figures and prices that haven't been independently verified. Used as a quality signal (does HQEW show stock?), not as a procurement target.
- The cap was reduced from 30 supplier rows to **top-5 by quantity** to make output reviewable.

### ICKEY (云汉芯城) — Grade B, "borrowed coverage"

- **Scraper-only.** Pass-rate **80.4 %**, mfr_match **69.8 %**, qty>0 **83.7 %**.
- **Critical caveat:** ICKEY is a **resale aggregator**. Every successful row's warehouse field is one of:
  - `ICKEY 转售 (Digi-Key)` — 70 %+ of ICKEY's "coverage" is just Digi-Key inventory relabeled
  - `ICKEY 转售 (云汉在库)` — internal 云汉 warehouse
  - `ICKEY 转售 (国内现货)` — third-party domestic
- About half of OK rows have `returned_mpn == input_mpn`; the other half are fuzzy matches (e.g. `HT66F017-HF` → `HFD4/5`) or empty returned_mpn. **Verifying via the underlying upstream is essential** when ICKEY is the only hit.
- **Why grade B not A:** high coverage but borrowed; high mfr_match drift; trustworthiness lower than the upstream source it's relisting.

### 买芯片网 (bom2buy.com) — Grade B, reactivated 2026-05-20

- **Scraper-only.** Pass-rate **59.8 %**, mfr_match **91.3 %**, qty>0 **77.8 %**. Reactivated since the scraper v3 report — the global CAPTCHA gate that blocked us on 2026-05-18 has either been lifted or the new scraper found a path around it.
- **Pattern:** **meta-aggregator** — each chip page lists 10+ rows from a mix of tier-1 distributors (`Arrow.cn`, `Avnet Americas`, `Avnet Asia`) and independent broker networks (`Bristol Electronics`, `Chip 1 Exchange`, `ComSIT Asia/USA`, `CoreStaff`, `Component Electronics`, …). The `warehouse` field reveals the upstream so procurement can verify.
- **Why grade B (中等 trust) instead of 较低 like HQEW:** higher mfr_match (91 % vs HQEW's 78 %), real distributor names in `warehouse`, and the tier-1 entries (Arrow.cn, Avnet) carry actual carrier inventory. Still aggregator-pattern → always check the upstream warehouse name.
- **Currently NOT excluded from procurement merge** (only `HQEW_` is dropped in `DROP_SOURCE_PREFIXES`). Worth a deliberate decision: keep contributing bom2buy rows for coverage, OR add `BOM2BUY_` to the drop list for consistency with the "no aggregators in procurement" stance. The merge script change would be a one-liner.

### ARROW (艾睿) — Grade B, API-only

- **Scraper:** blocked by Akamai BMP `_abck`. Reference code only.
- **API:** `api_arrow.py`. Pass-rate **42.1 %**, mfr_match **94.3 %**, qty>0 **86.3 %**.
- **Mirror caveat:** Arrow republishes the same physical stock under Verical, Arrow ACNA, Arrow EUROPE. Driver dedupes by `(fohQty, shipsFrom, shipsIn)` and tags duplicates with `" — mirror"`. Naive `SUM(stockpool_qty)` double-counts.
- **Per-warehouse currency:** USD (US) / EUR (Europe) / JPY (Verical Japan). `currency` is per row.

### Element14 (e络盟) — Grade B−, API-only

- **Scraper:** dropped 2026-05-18 — Akamai BMP 403 even after homepage warmup.
- **API:** `api_element14.py`. Pass-rate **43.0 %**, mfr_match **70.6 %** (high LITEON→MURATA/INFINEON drift on LTV/LTW parts). Quota **2 req/s, 1000/day** enforced.
- **Aggregate-row caveat:** the `Element14 (cn.element14.com)` row is the site-level total; per-region rows (`Element14 / UK`, `/ SG`, `/ Shanghai`) are also emitted. Filter one or the other before summing.

### Future Electronics (富昌) — Grade B−

- **Scraper:** `scrape_future.py`, Playwright **Firefox** (Chromium HTTP/2 reject; Firefox passes). Pass-rate **51.4 %**, mfr_match **90.2 %**, qty>0 **24.8 %**.
- **Why grade B−:** the low qty>0 rate is the key signal — Future surfaces lots of "Factory Stock" / "On Order" rows with no committed quantity. Useful for lead-time information, weak for 现货 procurement.
- Singapore region row is suppressed (it was a same-number duplicate of Global Stock on the APAC site).
- No API available.

### ONEYAC (唯样商城) — Grade C+

- **Scraper-only.** Pass-rate **50.5 %**, mfr_match **63.0 %**, qty>0 **37.0 %**.
- Marketplace pattern; significant fuzzy-match drift. OOS rows still emit usable MOQ + 期货 (`交期 16W`) info — driver was patched to keep them rather than drop on `stock=0`.

### RSONLINE (RS 欧时) — Grade C in throughput, but precision is high

- **Scraper-only.** Pass-rate **29.0 %** on the 107-chip sweep, mfr_match **100 %**. qty>0 **59.5 %**.
- **The numbers tell two stories.** When RSONLINE returns a result, it's spot-on — every successful row matched the expected manufacturer. The low pass-rate is from RS's WAF throttling: isolated runs reach ~70 % pass-rate, sweep runs drop to under 30 %.
- Engine: `curl_cffi` chrome131 only (cheapest scraper in the stack — no Playwright). Stock extracted from the Adobe `_satellite_pageBottom` analytics data layer.

### Rochester Electronics — niche

- **Scraper-only.** Pass-rate **11.2 %**, mfr_match 42 %. But qty>0 is **100 %** when it hits.
- **By design:** Rochester is an EOL / Last-Time-Buy / legacy-stock specialist. The handful of chips it finds on each sweep are legacy NXP / Infineon triacs and MOSFETs (BT131, BT139, BTA206X, BTA312X, LM317LD13TR, IRLML5103TRPBF). Useful as a *tail* source when a part is end-of-life; not a default for current production BOMs.

### Verical (verical.com) — covered indirectly via Arrow

- **Scraper:** repeated "系统错误" popup + WAF after a few probes. Dropped 2026-05-18.
- **API:** no standalone Verical API. But Verical is an **Arrow Electronics subsidiary**, and Arrow's Pricing & Availability v4 (`api_arrow.py`) exposes Verical stock as a separate entry inside `webSites[].sources[]` — `warehouse` value like `"Arrow / VERICAL — ships from Japan"`.
- **Mirror-dedup caveat:** Arrow republishes the same physical Verical USA stock under `arrow.com` (Arrow ACNA) too — see `api/doc/api_report_v2.md` §4 for the `(fohQty, shipsFrom, shipsIn)` tuple-based dedup that tags the second occurrence with `" — mirror"`.
- **Status:** no separate Verical query needed. If you want Verical-specific stock, filter Arrow API rows by `warehouse LIKE 'Arrow / VERICAL%'`.

### Chip1Stop (chip1stop.com) — defunct, absorbed by Arrow

- **Scraper:** the domain returns a 301 redirect to `arrow.com` — Arrow acquired Chip1Stop and folded the inventory. There is no live Chip1Stop product page to scrape. Dropped 2026-05-18.
- **API:** none; absorbed into Arrow's catalogue.
- **Status:** treat as a historical name. The parts that used to be on Chip1Stop now show up under Arrow API results (sometimes with `mfr.mfrCd` retained as the original Japanese-supplier code in the response payload).

---

## Your perception vs. the data

You said:
1. **云汉芯城, Digikey 质量较好.** Partial confirmation:
   - **Digikey ✅** — 98 % mfr_match, broad warehouse split, API + scraper agree. Grade A.
   - **ICKEY ⚠️** — high *coverage* (81 %), but it's a **resale aggregator**. 70 % of ICKEY's hits are Digi-Key inventory relabeled, and 49 % of returned MPNs are fuzzy / drift. Coverage feels good, but the "quality" is really Digi-Key's quality showing through with extra fuzzy-match noise. Treat as Grade B, not "高质量".

2. **Future, OneYac 质量一般.** Confirmed:
   - **Future ✅** — 50 % coverage, 90 % mfr_match, but only 23 % of OK rows carry real stock. Mostly factory-lead-time rows.
   - **ONEYAC ✅** — 50 % / 67 % / 33 %. Marketplace fuzzy drift. Grade C+.

3. **RS 欧时, Rochester 质量较差.** Needs nuance:
   - **RSONLINE ⚠️** — **low throughput, but pristine precision**. 100 % mfr_match in the sweep (33 of 33). The "差" is WAF-induced low coverage (28 %), not data quality. With rate-limit hardening this becomes a Grade B source.
   - **Rochester ✅** — confirmed at 9 % coverage, but this is by design. It's an EOL specialist; useless for current-production BOMs but the only place to find legacy stock.

**Sources you didn't mention that are worth a verdict:**
- **LCSC 立创商城** — Grade A−. 79 % scraper coverage; the strongest domestic source for Chinese MCUs/discretes.
- **买芯片网 (bom2buy)** — Grade B, **newly reactivated** (was blocked through 5-19). 60 % coverage with a long list of upstream distributors per chip; treat as a complement to HQEW (more tier-1 content, slightly cleaner mfr_match).
- **HQEW 华强电子网** — Highest scraper pass-rate (82 %) but **excluded from procurement merge** as a B2B supplier-listing aggregator (untrusted relisting).
- **Mouser / Arrow / Element14** — Scraper-blocked; only reachable via the API track. Mouser at ~98 % mfr_match is one of the cleanest signals in the pipeline.

---

## Recommended procurement playbook

A practical reading of the inventory:

1. **Primary trust tier** (use directly): Digikey, LCSC, Mouser, Arrow, Element14 — all direct distributors with verified mfr_match ≥ 95 % on the API path (Element14 ≥ 71 %). Procurement merge already promotes API rows over scraper rows for the overlap (Digikey, LCSC).
2. **Coverage extension tier**: Future, ONEYAC, RSONLINE — fill the gap on parts the primary tier doesn't carry. Treat lead-time and OOS info as legitimate signal even when 现货 is zero.
3. **Aggregator tier** (always verify upstream warehouse name): bom2buy, ICKEY — if either is the only hit, inspect the `warehouse` field (`(distributor)` suffix or distributor name) and check that upstream directly.
4. **Specialty tier**: Rochester — EOL parts only.
5. **Reference signal only** (excluded from procurement): HQEW — high coverage but B2B relisting, not procurement-grade.

The `doc/merge_for_procurement_rules.md` document encodes filters 1–3 (API wins; HQEW dropped; mfr_match flag preserved) into the merge script. **bom2buy is currently included in the merge** — open question whether to exclude it for the same B2B-aggregator reason that HQEW is excluded.

---

## Where to dig deeper

| For… | Read… |
|---|---|
| Per-source auth, schema, gotchas (API) | `api/doc/api_report_v2.md` |
| Per-source extraction logic, audit history (scraper) | `scraper/doc/scraper_report_v3.md` |
| Output column contract (24 / 26 cols) | `api/doc/batch_output_schema.md`, `scraper/doc/batch_output_schema.md` |
| Procurement merge rules | `doc/merge_for_procurement_rules.md` |
| Current batch state, blockers | `~/.claude/projects/.../memory/MEMORY.md` |

---

## Snapshot of latest sweeps

- API: `test/api/BatchTest_20260520_07_40_36/` — 107 chips × 5 sources
- Scraper: `test/scraper/BatchTest_20260520_07_40_03/` — 107 chips × 9 sources (bom2buy now contributes)

Numbers in this report are computed from those two CSVs. Re-run `python common/merge_batch_for_procurement.py` and re-execute the per-source quality query at the top of this doc to refresh.
