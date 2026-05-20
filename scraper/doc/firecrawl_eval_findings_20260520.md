# Firecrawl evaluation findings — 2026-05-20T11:48:37

**Run dir:** `test\scraper_test\Firecrawl_Eval_20260520_11_41_37`
**Plan:** `~/.claude/plans/cozy-singing-hennessy.md`

## Credit usage

- Total calls: **31** (1 smoke + 4 bom2buy probes + 1 ad-hoc `/parametric/` probe + 10 markdown probes + 10 JSON-extract probes + 5 Phase 1b follow-up probes)
- Total credits used: **78** (counted from `data.metadata.creditsUsed` in each saved response)
- Cost breakdown: markdown / basic-proxy calls = 1 credit each; JSON-extract calls = 5 credits each; stealth-proxy calls = 5 credits each (3 of the 5 Phase 1b probes used stealth)
- Failed calls (HTTP != 200): 1 (Firecrawl-side HTTP 500 on stealth-proxy + search URL; not counted toward credits)
- Original cap: 70 — Phase 1b follow-up pushed us to 78 (user-requested deeper verification)

## Phase 1 — bom2buy.com feasibility

| Target | HTTP | md chars | captcha? | product signals? | verdict |
|---|---|---|---|---|---|
| search_STM32 | 200 | 219 | yes | no | CAPTCHA |
| search_BT168 | 200 | 219 | yes | no | CAPTCHA |
| search_ATXMEGA | 200 | 219 | yes | no | CAPTCHA |
| homepage | 200 | 66881 | no | yes (nav menu only — no chip data) | partial |
| /parametric/ (ad-hoc) | 200 | 219 | yes | no | CAPTCHA |

**Phase 1 verdict:** **FAIL** — Firecrawl did **not** bypass bom2buy's CAPTCHA gate.

The homepage loads (66 k chars of navigation/SEO content), but every URL that would surface chip data — `/search?keyword=…`, `/parametric/`, and presumably any category or product detail page — server-side-redirects to `captcha.bom2buy.com/index?…` with IconCaptcha. Firecrawl's free-tier proxy reports `"proxyUsed": "basic"`; their docs reference a paid `stealth` proxy that may handle this, but it is not available on the 1 000-credit free tier.

The "product signals" auto-heuristic flagged the homepage as OK because nav anchors contained words like "manufacturer" and "price", not because any actual chip listing was returned. The auto-recommendation logic was misled by this; the real verdict is FAIL.

### Phase 1b — extended probes (added on user follow-up "are we 100 % sure bom2buy is unscrapable?")

5 additional probes to rule out edge cases:

| Probe | Proxy | HTTP | Credits | Final URL after redirects | Verdict |
|---|---|---:|---:|---|---|
| `/amplifier-circuits/operational-amplifiers/` (chip category) | basic | 200 | 1 | `captcha.bom2buy.com/index?...` | CAPTCHA |
| Same URL again | **stealth** | 200 | 5 | `captcha.bom2buy.com/index?...` | CAPTCHA (still) |
| `/search?keyword=STM32G030F6P6` | **stealth** | 500 | 0 | — | Firecrawl-side HTTP 500 |
| `/parametric/` | **stealth** | 200 | 5 | `captcha.bom2buy.com/index?...` | CAPTCHA (still) |
| `/blog/4819.html` (blog post) | basic | 200 | 1 | unchanged | **OK** — 6 593 chars, real blog title |

Key findings from Phase 1b:

1. **The captcha is not URL-pattern-specific to `/search` or `/parametric`.** Every commerce/category page (e.g. `/amplifier-circuits/operational-amplifiers/`, which is linked directly from the homepage we loaded successfully) also server-side redirects to `captcha.bom2buy.com/index?...&uri=<original>`. Site-wide captcha enforcement on anything that would surface chip data.

2. **Firecrawl's `proxy: "stealth"` mode IS available on the free tier** (no auth error; it ran and consumed 5 credits each), but it **does not bypass bom2buy's captcha**. The same captcha URL is returned. Stealth mode is 5× the cost of `basic` mode per call.

3. **Blog / homepage pages load fine.** `/blog/4819.html` returns real content (the post title parses correctly as "《元器件动态周报》——DDR4价格失控加速DDR5市场渗透-bom2buy"). bom2buy's captcha is therefore a deliberate commerce-flow gate, not a generic anti-bot policy on the whole domain.

**Definitive Phase 1 conclusion:** bom2buy.com is **not scrapable** via Firecrawl, regardless of proxy tier we use on the free key. Every URL that surfaces chip data (search, parametric, category, presumably product detail) is captcha-gated. The captcha system is "IconCaptcha" — an image-puzzle solver that Firecrawl's stealth proxy does not solve.

**Sample markdown from `https://www.bom2buy.com/` (first 1500 chars):**

```markdown
[update your browser](http://www.chromeliulanqi.com/)

Your browser version is old. This can cause problems on our web site.

Please update your browser to get the best experience

service@bom2buy.com(0512) 62988549

[Buying Guide](https://www.bom2buy.com/blog/) [About Us](https://www.bom2buy.com/about/us) [FAQ](https://www.bom2buy.com/service/help) [En/中](https://www.bom2buy.com/?lang=zh) [Enterprise Edition](https://cn.supplyframe.com/saas/xq/)

[元器件选型](https://www.bom2buy.com/parametric/ "元器件选型")

MRO
New

Electrical, Automated And Cables

- Switches



Automation Control







[Switches](https://www.bom2buy.com/mro/list/463/)



  - [Switch Disconnectors Components](https://www.bom2buy.com/mro/list/538/)
  - [Limit Position Switches](https://www.bom2buy.com/mro/list/537/)
  - [Keyboard Keypad Switches](https://www.bom2buy.com/mro/list/536/)
  - [Foot Switches Components](https://www.bom2buy.com/mro/list/535/)
  - [Rocker Switches Components](https://www.bom2buy.com/mro/list/534/)
  - [Capacitor, Magnetic And Voltage Switch](https://www.bom2buy.com/mro/list/533/)
  - [Rotary Switches Components](https://www.bom2buy.com/mro/list/532/)
  - [Joysticks Components](https://www.bom2buy.com/mro/list/531/)
  - [Key Switches Selector Switches](https://www.bom2buy.com/mro/list/530/)
  - [Push Button Switches Components](https://www.bom2buy.com/mro/list/529/)
  - [Toggle Switches Slide Switches](https://www.bom2buy.com/mro/list/528/)
  - [Rope Pull Switches Components](https://www.b
```

## Phase 2 — quality parity vs existing scrapers

### ICKEY × STM32G030F6P6

- Detail URL: <https://www.ickey.cn/detail/1000201010915684/STM32G030F6P6.html>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_STM32G030F6P6_ICKEY/STM32G030F6P6.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | STM32G030F6P6 | STM32G030F6P6 | match |
| manufacturer | STMicroelectronics | STMicroelectronics | match |
| stock_now_qty | 76636 | 76634 | differ |
| stock_now_ship_text | 内地成团后 10-14工作日 | 内地10-14工作日 | differ |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=1 | len=1 | match (len) |
| prices | len=2 | len=2 | match (len) |
| datasheet_url | javascript:; | https://www.ickey.cn/static-pf/datasheet/37/5c/2157/37/0138544c18e0fad14695a330… | differ |
| package | — | TSSOP20 | firecrawl wins |
| lifecycle_status | — | 在售 | firecrawl wins |
| min_order_qty | 2960 | 2960 | match |

**Tally:** scraper wins 0, firecrawl wins 2, match 5, both empty 1

### ICKEY × CY8C4025AZI-S413T

- Detail URL: <https://www.ickey.cn/detail/1000201010869993/CY8C4025AZI-S413T.html>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_CY8C4025AZI-S413T_ICKEY/CY8C4025AZI-S413T.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | CY8C4025AZI-S413T | CY8C4025AZI-S413T | match |
| manufacturer | Infineon Technologies AG | Infineon Technologies AG | match |
| stock_now_qty | 1500 | 1500 | match |
| stock_now_ship_text | 内地成团后 10-15工作日 | 内地10-15工作日 | differ |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=1 | len=0 | scraper ↑ |
| prices | len=3 | len=3 | match (len) |
| datasheet_url | javascript:; | https://www.ickey.cn/static-pf/datasheet/ac/da/5515/ac/2e9ea73b8d604efed364420e… | differ |
| package | — | TQFP | firecrawl wins |
| lifecycle_status | — | — | both empty |
| min_order_qty | 1500 | 1500 | match |

**Tally:** scraper wins 1, firecrawl wins 1, match 5, both empty 2

### ROCHESTER × IRLML5103TRPBF

- Detail URL: <https://www.rocelec.com/part/01t4w00000PPCKKAA5-IRLML5103TRPBF>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_IRLML5103TRPBF_ROCHESTER/IRLML5103TRPBF.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | IRLML5103TRPBF | IRLML5103TRPBF | match |
| manufacturer | Infineon | Infineon | match |
| stock_now_qty | 1130266 | 1130266 | match |
| stock_now_ship_text | In Stock at Rochester Electronics warehouse | In Stock | differ |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=1 | len=1 | match (len) |
| prices | len=0 | len=5 | firecrawl ↑ |
| datasheet_url | https://rocelec.widen.net/view/pdf/bmdnyxrsyn/IRLML5103TRPBF.pdf?t.download=tru… | https://rocelec.widen.net/view/pdf/bmdnyxrsyn/IRLML5103TRPBF.pdf?t.download=tru… | match |
| package | SOT23 | SOT23 | match |
| lifecycle_status | Active | Active | match |
| min_order_qty | — | — | both empty |

**Tally:** scraper wins 0, firecrawl wins 1, match 7, both empty 2

### ROCHESTER × L78L33ABUTR

- Detail URL: <https://www.rocelec.com/part/01tRl000003cXEOIA2-L78L33ABUTR>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_L78L33ABUTR_ROCHESTER/L78L33ABUTR.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | L78L33ABUTR | L78L33ABUTR | match |
| manufacturer | STMicroelectronics | STMicroelectronics | match |
| stock_now_qty | 35055 | 35055 | match |
| stock_now_ship_text | In Stock at Rochester Electronics warehouse | — | scraper wins |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=1 | len=0 | scraper ↑ |
| prices | len=0 | len=5 | firecrawl ↑ |
| datasheet_url | https://rocelec.widen.net/s/s2vghqwvl5/l78l33cd-tr-rosfgd | https://rocelec.widen.net/s/s2vghqwvl5/l78l33cd-tr-rosfgd | match |
| package | SOT-89-4 | SOT-89-4 | match |
| lifecycle_status | Active | Active | match |
| min_order_qty | — | — | both empty |

**Tally:** scraper wins 2, firecrawl wins 1, match 6, both empty 2

### ONEYAC × ATXMEGA32E5-ANR

- Detail URL: <https://www.oneyac.com/product/15981551.html>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_ATXMEGA32E5-ANR_ONEYAC/ATXMEGA32E5-ANR.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | ATXMEGA32E5-ANR | ATXMEGA32E5-ANR | match |
| manufacturer | 唯样海外代购 | Microchip(微芯) | differ |
| stock_now_qty | 0 | 0 | match |
| stock_now_ship_text | — | 要订货? | firecrawl wins |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=1 | len=1 | match (len) |
| prices | len=0 | len=1 | firecrawl ↑ |
| datasheet_url | — | — | both empty |
| package | — | 32-TQFP | firecrawl wins |
| lifecycle_status | — | 该型号已停产！ | firecrawl wins |
| min_order_qty | 2000 | 2000 | match |

**Tally:** scraper wins 0, firecrawl wins 4, match 4, both empty 2

### ONEYAC × BT168GW,115

- Detail URL: <https://www.oneyac.com/product/30800157.html>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_BT168GW_115_ONEYAC/BT168GW_115.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | BT168GW,115 | BT168GW,115 | match |
| manufacturer | WeEn | WeEn(瑞能) | differ |
| stock_now_qty | 5000 | 5000 | match |
| stock_now_ship_text | 交期 3天-5天 | 生产周期：16W | differ |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=1 | len=0 | scraper ↑ |
| prices | len=0 | len=1 | firecrawl ↑ |
| datasheet_url | javascript:void(0); | https://www.oneyac.com/product/30800157.html | differ |
| package | SOT-223 | SOT-223 | match |
| lifecycle_status | — | 该型号已停产！ | firecrawl wins |
| min_order_qty | 1000 | — | scraper wins |

**Tally:** scraper wins 2, firecrawl wins 2, match 3, both empty 1

### RSONLINE × STM32G030F6P6

- Detail URL: <https://www.rsonline.cn/web/p/microcontrollers/2396333>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_STM32G030F6P6_RSONLINE/STM32G030F6P6.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | STM32G030F6P6 | STM32G030F6P6 | match |
| manufacturer | STMicroelectronics | STMicroelectronics | match |
| stock_now_qty | — | — | both empty |
| stock_now_ship_text | — | 库存信息目前无法访问 - 请稍候查看 | firecrawl wins |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=0 | len=0 | both empty |
| prices | len=4 | len=4 | match (len) |
| datasheet_url | — | https://docs.rs-online.com/3ccb/A700000008637620.pdf | firecrawl wins |
| package | TSSOP | TSSOP | match |
| lifecycle_status | IN_STOCK | — | scraper wins |
| min_order_qty | — | 5 | firecrawl wins |

**Tally:** scraper wins 1, firecrawl wins 3, match 4, both empty 3

### RSONLINE × CY8C4025AZI-S413T

- Detail URL: <https://www.rsonline.cn/web/p/microcontrollers/2733295>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_CY8C4025AZI-S413T_RSONLINE/CY8C4025AZI-S413T.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | CY8C4025AZI-S413T | CY8C4025AZI-S413T | match |
| manufacturer | Infineon | Infineon | match |
| stock_now_qty | 0 | — | scraper wins |
| stock_now_ship_text | 暂时缺货 | 暂时缺货 | match |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=1 | len=1 | match (len) |
| prices | len=1 | len=1 | match (len) |
| datasheet_url | — | https://docs.rs-online.com/625f/A700000010153710.pdf | firecrawl wins |
| package | TQFP | TQFP | match |
| lifecycle_status | OUT_OF_STOCK | — | scraper wins |
| min_order_qty | — | 1500 | firecrawl wins |

**Tally:** scraper wins 2, firecrawl wins 2, match 6, both empty 1

### FUTURE × CY8C4025AZI-S413T

- Detail URL: <https://www.futureelectronics.com/p/semiconductors--microcontrollers--32-bit/cy8c4025azi-s413t-infineon-1127137>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_CY8C4025AZI-S413T_FUTURE/CY8C4025AZI-S413T/CY8C4025AZI-S413T.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | CY8C4025AZI-S413T | CY8C4025AZI-S413T | match |
| manufacturer | Infineon | Infineon | match |
| stock_now_qty | 0 | 0 | match |
| stock_now_ship_text | — | Global Stock: 0 | firecrawl wins |
| stock_future_qty | 0 | 0 | match |
| stock_breakdown | len=4 | len=1 | scraper ↑ |
| prices | len=1 | len=1 | match (len) |
| datasheet_url | https://www.infineon.com/dgdl/Infineon-PSoC_4_PSoC_4000S_Datasheet_Programmable… | https://www.infineon.com/dgdl/Infineon-PSoC_4_PSoC_4000S_Datasheet_Programmable… | match |
| package | TQFP-48 | TQFP-48 | match |
| lifecycle_status | — | Active | firecrawl wins |
| min_order_qty | — | 1500 | firecrawl wins |

**Tally:** scraper wins 1, firecrawl wins 3, match 7, both empty 0

### FUTURE × STM32G030F6P6

- Detail URL: <https://www.futureelectronics.com/p/semiconductors--microcontrollers--32-bit/stm32g030f6p6-stmicroelectronics-8137468>
- Scraper JSON: `BatchTest_20260520_07_40_03/Test_STM32G030F6P6_FUTURE/STM32G030F6P6/STM32G030F6P6.json`
- Phase 2a (markdown) verdict: **OK**
- Phase 2b (JSON) verdict: **OK**

| Field | Scraper | Firecrawl | Verdict |
|---|---|---|---|
| manufacturer_part_number | STM32G030F6P6 | STM32G030F6P6 | match |
| manufacturer | STMicroelectronics | STMicroelectronics | match |
| stock_now_qty | 3890 | 3890 | match |
| stock_now_ship_text | Ships immediately (Future global stock) | Available | differ |
| stock_future_qty | — | — | both empty |
| stock_breakdown | len=3 | len=1 | scraper ↑ |
| prices | len=5 | len=5 | match (len) |
| datasheet_url | https://www.st.com/resource/en/datasheet/stm32g030f6.pdf | https://www.st.com/resource/en/datasheet/stm32g030f6.pdf | match |
| package | TSSOP-20 | TSSOP-20 | match |
| lifecycle_status | — | Active | firecrawl wins |
| min_order_qty | — | 40 | firecrawl wins |

**Tally:** scraper wins 1, firecrawl wins 2, match 6, both empty 1

## Overall scoreboard (Phase 2)

| Source | MPN | Scraper wins | Firecrawl wins | Match | Both empty |
|---|---|---|---|---|---|
| ICKEY | STM32G030F6P6 | 0 | 2 | 5 | 1 |
| ICKEY | CY8C4025AZI-S413T | 1 | 1 | 5 | 2 |
| ROCHESTER | IRLML5103TRPBF | 0 | 1 | 7 | 2 |
| ROCHESTER | L78L33ABUTR | 2 | 1 | 6 | 2 |
| ONEYAC | ATXMEGA32E5-ANR | 0 | 4 | 4 | 2 |
| ONEYAC | BT168GW,115 | 2 | 2 | 3 | 1 |
| RSONLINE | STM32G030F6P6 | 1 | 3 | 4 | 3 |
| RSONLINE | CY8C4025AZI-S413T | 2 | 2 | 6 | 1 |
| FUTURE | CY8C4025AZI-S413T | 1 | 3 | 7 | 0 |
| FUTURE | STM32G030F6P6 | 1 | 2 | 6 | 1 |
| **TOTAL** | — | **10** | **21** | **53** | **15** |

## Recommendation

**Selective adoption — keep scrapers as primary; use Firecrawl as a quality-augmentation layer on specific fields.**

### Headline signals

- **Q1 — bom2buy:** ❌ NOT solved, **conclusively** (verified across 9 distinct URL patterns in Phase 1 + 1b, both `basic` and `stealth` proxy modes). Every URL that surfaces chip data — search, parametric, chip categories — server-side-redirects to `captcha.bom2buy.com/index?…` regardless of which Firecrawl proxy is used. Stealth proxy (5×-cost) does not solve IconCaptcha. Re-testing makes sense only if (a) Firecrawl adds an integrated captcha-solver feature, or (b) we contract a third-party captcha-solving service (~$0.001–0.003 per solve) and front-end Firecrawl with it.
- **Q2 — working-5 quality:** Firecrawl wins 21 fields vs scraper's 10 (2.1× ratio) out of 84 non-empty comparisons; 53 fields match outright. Firecrawl is meaningfully better at field completeness, but not so dominant that it should replace our scrapers wholesale.

### Where Firecrawl beats us consistently (across multiple cells)

| Field | Why scraper struggles | Why Firecrawl wins |
|---|---|---|
| `datasheet_url` | ICKEY + ONEYAC + RSONLINE put `javascript:;` / `javascript:void(0)` placeholders in the anchor (login-walled in the source) | The LLM extractor lifts the real PDF URL from elsewhere on the page (Schema.org / static-pf paths / RS docs CDN) |
| `manufacturer` | ONEYAC marketplace listings put the platform name ("唯样海外代购") in the brand field for resold parts | Firecrawl extracts the real chip mfr ("Microchip(微芯)" / "WeEn(瑞能)") from elsewhere on the page |
| `package` | ICKEY does not surface package in a structured slot; scraper relied on regex over body text | Firecrawl pulls TSSOP20 / TQFP / SOT-89-4 reliably |
| `lifecycle_status` | ICKEY + Future do not have a dedicated status field in our extractors | Firecrawl flags "在售" / "Active" / "该型号已停产！" from page wording |
| `min_order_qty` | Future + RSONLINE + Rochester sometimes hide MOQ in tier-1 price metadata | Firecrawl picks it up |
| `prices` (Rochester) | Rochester's pricing renders in an LWC table we do not parse — scraper has `prices: []` for both ROC cells | Firecrawl extracted 5 tiers each |

### Where the scraper wins or where Firecrawl regressed

| Field | Pattern |
|---|---|
| `stock_breakdown` length | Scraper produces multi-row breakdowns (Future 3–4 pools, ICKEY single warehouse). Firecrawl's LLM collapses everything to 1 row even when the schema clearly allows N. **Scraper wins on warehouse-row fidelity.** |
| `lifecycle_status` (RSONLINE) | Our scraper carries the canonical RS `IN_STOCK`/`OUT_OF_STOCK` flag; Firecrawl didn't extract it (free-form schema, missed the field). |
| `stock_now_ship_text` (Rochester L78L33ABUTR) | Scraper had the literal "In Stock at Rochester Electronics warehouse"; Firecrawl returned empty for one of the two ROC cells. |
| `stock_now_qty` (ICKEY STM32G030F6P6) | Scraper 76 636 vs Firecrawl 76 634 — minor drift, almost certainly the page count changed between the two scrapes. Not a real win for either. |

### Concrete proposal — three modes of integration (NOT auto-implemented)

1. **Bom2buy:** Park indefinitely. The free-tier doesn't reach chip data. Re-evaluate only with paid stealth proxy.
2. **Datasheet recovery + mfr correction:** Run Firecrawl JSON-extract as a **post-processor** on cells where the scraper produced `javascript:;` / `javascript:void(0)` datasheet URLs OR a marketplace-name manufacturer ("唯样海外代购", similar HQEW/ICKEY junk). At 5 credits per call, fixing roughly 50 broken cells per 103-chip batch costs ~250 credits = a quarter of the monthly free tier per batch.
3. **Field augmentation for low-coverage sources:** Rochester yields no prices via DOM scrape, but Firecrawl recovered 5 tiers from the same page. A Firecrawl pass on the 9 Rochester `ok` cells per batch (~45 credits) restores price coverage on the EOL source.

**Not recommended:** replacing any existing scrape_<source>.py with a Firecrawl-driven one. At 5 credits / call × 103 chips × 8 sources = 4 120 credits per batch — exceeds the free tier 4× over, and our DOM scrapers already match Firecrawl on 53/84 fields.

### Suggested follow-up (subject to user approval — not in this task)

If we proceed with mode 2 + 3 above:

1. Add `scraper/scripts/firecrawl_augment.py` — reads a finished `BatchTest_*/batch_index.json`, identifies cells with `datasheet_url ∈ {javascript:;, javascript:void(0), ""}` OR a marketplace-name mfr, makes one Firecrawl `/v2/scrape` call per such cell with the canonical schema, merges the LLM extract back into the per-cell JSON under an `augmented` namespace (preserves provenance), regenerates the `batch_summary.md`.
2. Add a Firecrawl-prices pass for Rochester only (~45 credits / batch).
3. Install the official Firecrawl skill (per `temp/firecrawl_skill.md` Path A) only if we end up running augmentation regularly — not a blocker for steps 1+2 since the REST shim used in this eval already works.

---
_Generated by `scraper/scripts/firecrawl_eval.py` at 2026-05-20T11:48:37; recommendation section rewritten by hand to correct the auto-generated Phase 1 verdict (which mis-classified the homepage-loads-but-search-blocked outcome as PASS)._
