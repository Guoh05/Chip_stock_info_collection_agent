# Scraper Track — Web scraping

Track 1 of the chip-availability pipeline. Drives distributor websites with `curl_cffi` + Playwright to extract availability, pricing, and parameters.

<!-- BEGIN AUTO:status — managed by scraper/scripts/_update_readme_status.py (see "Auto-updating this README" at bottom) -->

## Status snapshot (2026-05-20)

| Channel | Method | Working? |
|---|---|---|
| **LCSC** (立创商城, szlcsc.com) | Playwright Chromium `--headless=new` + `__NEXT_DATA__` + DOM right-panel | ✅ |
| **Digikey** (得捷电子, digikey.cn) | Playwright stealth Chromium + `__NEXT_DATA__` envelope | ✅ |
| **HQEW** (华强电子网, hqew.com) | Playwright Chromium + supplier-table DOM scrape (top-5 per chip) | ✅ |
| **Future** (Future Electronics, futureelectronics.com) | Playwright **Firefox** (Akamai HTTP/2 bypass) + cookie-banner dismiss | ✅ |
| **RSONLINE** (RS 欧时, rsonline.cn) | curl_cffi + Next.js `__NEXT_DATA__` + Adobe data-layer `stockinfo` | ✅ |
| **ONEYAC** (唯样商城, oneyac.com) | Playwright Firefox + main-product card extraction (not recommended-carousel) | ✅ |
| **ICKEY** (云汉芯城, ickey.cn) | Playwright Chromium + doT.js template hydration wait (marketplace aggregator) | ✅ |
| **Rochester** (Rochester Electronics, rocelec.com) | Playwright Firefox + LWC hydration + exact-MPN guard (EOL-only) | ✅ |
| **bom2buy** (买芯片网, bom2buy.com) | Playwright + Opera user-data-dir reuse (requires user-managed IconCaptcha session; Opera must be fully closed when scraping) | ✅ |
| Mouser (贸泽, mouser.cn / .com) | Blocked by Akamai BotManager `bm-verify` — use api/scripts/api_mouser.py instead | ❌ |
| Arrow (艾睿, arrow.com) | Blocked by Akamai BotManager `_abck` — use api/scripts/api_arrow.py (key pending) | ❌ |

**Latest batch run:** `test/scraper/BatchTest_20260520_07_40_03/` — 107 MPNs × 9 source(s) = 963 cells.

| Channel | OK | No results | Blocked | Failed | OK % |
|---|---|---|---|---|---|
| LCSC | 84 | 23 | 0 | 0 | 78.5 % |
| Digikey | 60 | 0 | 3 | 44 | 56.1 % |
| HQEW | 88 | 19 | 0 | 0 | 82.2 % |
| Future | 55 | 52 | 0 | 0 | 51.4 % |
| RS Online | 31 | 76 | 0 | 0 | 29.0 % |
| Oneyac | 54 | 53 | 0 | 0 | 50.5 % |
| ICKEY | 86 | 21 | 0 | 0 | 80.4 % |
| Rochester | 12 | 95 | 0 | 0 | 11.2 % |
| Bom2buy | 64 | 43 | 0 | 0 | 59.8 % |

Cross-source coverage: **3** chip(s) returned ok on all 9 sources; 17 on 8; 20 on 7; 6 on 6; 11 on 5; 14 on 4; 21 on 3; 8 on 2; 5 on 1; 2 on none.

**Manufacturer-name mismatches surfaced:** 72 — `ATXMEGA32E5-ANR` (ONEYAC: MICROCHIP → 唯样海外代购), `HT66F017-HF` (ICKEY: HOLTEK → HONGFA/厦门宏发), `Z0103MN,135` (ONEYAC: WEEN → STMicro), `B32933A3334K3` (HQEW: TDK → EPCOS), `CY8C21434-24LQXIT` (HQEW: INFINEON → CYPRESS), and 67 more.

<!-- END AUTO:status -->

## Single-MPN run

```bash
# from the project root (02_work_chip_availability/)
.venv/Scripts/python.exe scraper/scripts/scrape_lcsc_v3.py STM32G030F6P6
.venv/Scripts/python.exe scraper/scripts/scrape_digikey.py STM32G030F6P6
.venv/Scripts/python.exe scraper/scripts/scrape_hqew.py STM32G030F6P6
.venv/Scripts/python.exe scraper/scripts/scrape_future.py "ATXMEGA32E5-ANR"
.venv/Scripts/python.exe scraper/scripts/scrape_rsonline.py LIS2DH12TR
.venv/Scripts/python.exe scraper/scripts/scrape_oneyac.py LIS2DH12TR
.venv/Scripts/python.exe scraper/scripts/scrape_ickey.py STM32F103C8T6
.venv/Scripts/python.exe scraper/scripts/scrape_bom2buy.py STM32G030F6P6     # Opera must be fully closed; pre-pass captcha in Opera first
.venv/Scripts/python.exe scraper/scripts/scrape_bom2buy.py --mpns "STM32G030F6P6;BT168GW,115;ATXMEGA32E5-ANR"  # batch mode
```

Each run writes a folder under `test/scraper/Test_<MPN>_<CHANNEL>_<YYYYMMDD>_<HH>_<MM>_<SS>/`.

Optional `argv[2]` overrides the output directory (used by the batch driver — see below). When omitted, the auto-timestamp path above is used.

## Batch run — all chips from the master sheet

**Before running with the default bom2buy step:** open Opera, navigate to `https://www.bom2buy.com/`, solve the IconCaptcha once, then fully close Opera. The captcha session persists for hours-to-days and the batch driver reuses it.

```bash
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py             # full sweep, 9 sources (8 parallel + bom2buy post-step)
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --limit 3   # dry-run
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --only LCSC,HQEW
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --resume    # top up most recent BatchTest_*
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --no-bom2buy  # skip the Opera post-step
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --mpns-file mpns.tsv  # tab-separated input
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --env prod  # write to production/scraper/
```

CLI flags:

| Flag | Default | Purpose |
|---|---|---|
| `--xlsx PATH` | `ref/Shortage Emergency Response List_v2.xlsx` | Input chip list. New schema since 2026-05-20: header row 1, sheet `Part List Modify`, MPN col `Manufacture Part Number`, mfr col `Manufacture`. MPNs are deduped (107 unique from 280 raw rows in v2). The legacy `Chip_DataSource_Master.xlsx` (header row 4, cols 1 & 2) is still supported via the loader's backward-compat path. |
| `--mpns CSV` | none | Semicolon-separated `MPN[:MFR]` list — overrides --xlsx. NB: MPNs containing `:` (e.g. typo'd `BTA206X-800CT:127`) get chopped — use `--mpns-file` instead. |
| `--mpns-file PATH` | none | Tab-separated file: one MPN per line, `MPN<TAB>MFR` (MFR optional). Lines starting with `#` ignored. Safe for MPNs containing `:`. |
| `--limit N` | none | Process only the first N valid MPNs (dry-run aid). |
| `--only CSV` | all 8 | Comma-separated subset of `LCSC,DIGIKEY,HQEW,FUTURE,RSONLINE,ONEYAC,ICKEY,ROCHESTER`. (bom2buy is the 9th channel, run via the post-step — toggle with `--with-bom2buy` / `--no-bom2buy`.) |
| `--throttle SEC` | `1.0` | Sleep between chips (politeness gap). |
| `--resume` | off | Reuse the most recent `BatchTest_*` folder and skip per-MPN-per-channel runs whose parent JSON already exists. |
| `--sequential` | off | Run channels one at a time per chip (old behavior). Default is to fan out all 8 main channels concurrently. |
| `--with-bom2buy` / `--no-bom2buy` | **on** | After the 8-source main sweep, run `scrape_bom2buy.py` for all MPNs and merge into `batch_index.csv`. If the Opera captcha session is expired the batch still completes; bom2buy can be backfilled later. |

Each (MPN × main-channel) call runs as its own **subprocess** with a hard wallclock timeout (LCSC 240s, Digikey 180s, HQEW 90s, Future 300s, RSONLINE 90s, ONEYAC 120s, ICKEY 150s, Rochester 180s), so a hung browser cannot stall the batch. UTF-8 is forced in the subprocess env so `¥` / Chinese prints don't crash Windows GBK stdout capture.

**Channel parallelism (default):** For each chip, all 8 selected main-channel subprocesses run concurrently via a `ThreadPoolExecutor`. Since each channel hits a different domain there's no per-vendor rate-limit collision, and the chip's wallclock drops from `sum(channel times)` to `max(channel times)`. Observed speedup on the 103-chip × 8-channel sweep: ~53% (chip wallclock 40–50 s vs ~90 s sequential; full batch ~75 min vs 208 min). RAM cost: 8 concurrent browsers, peak ~3–5 GB. Use `--sequential` to opt out if RAM-constrained or debugging a single channel.

**bom2buy post-step (default ON, sequential):** After the main sweep finishes, the driver invokes `scrape_bom2buy.py --mpns-file <auto-generated> --out <batch_dir>`. The script must drive Opera with a captcha-cleared user session, so it CANNOT run alongside other channels. Behavior:
- **Success (exit 0):** bom2buy cells get scraped into the batch folder; the merge step (`_merge_bom2buy_into_batch.py`) is invoked to add bom2buy rows to `batch_index.csv`.
- **Captcha expired (exit 3):** the driver prints a clear user-action block (refresh Opera + the manual recipe) and **the batch still completes** with all 8 other channels. bom2buy can be backfilled later.
- **Other failure:** warning printed, merge runs anyway over whichever cells were scraped, batch completes.

Output goes to `test/scraper/BatchTest_<YYYYMMDD>_<HH_MM_SS>/`:

- `batch_summary.md` — TL;DR, per-channel pass rate, cross-channel coverage histogram, top-5 stock per channel, manufacturer mismatches, skipped rows
- `batch_index.csv` / `.xlsx` — v3 long form (warehouse-exploded; one row per MPN × source × warehouse)
- `batch_index.json` — machine-readable, includes subprocess stdout/stderr tails
- `batch_input.csv` — verbatim (MPN, expected_mfr) input (deduped)
- `failures.md` — non-ok rows grouped by channel
- `Test_<sanitized_mpn>_<CHANNEL>/` — per-MPN-per-channel run folders (populated by each subprocess)

Sanitization: non-alphanumeric except `.`, `_`, `-` → `_` (e.g. `PIC16F18446T-I/SS` → `PIC16F18446T-I_SS`).

Latest full sweep: 2026-05-20 (107 chips × 9 channels). Pass rates: HQEW 82 % / ICKEY 80 % / LCSC 79 % / Bom2buy 60 % / Digikey 56 % / Future 51 % / Oneyac 51 % / RSONLINE 29 % / Rochester 11 %.

## What each script does

| Script | Site | Method | Status |
|---|---|---|---|
| `scrape_lcsc_v3.py` | szlcsc.com | Playwright Chromium `--headless=new` + `__NEXT_DATA__` + DOM right-panel | ✅ |
| `scrape_digikey.py` | digikey.cn | Playwright stealth Chromium + `__NEXT_DATA__` envelope | ✅ |
| `scrape_hqew.py` | hqew.com | Playwright Chromium + supplier table scrape | ✅ |
| `scrape_future.py` | futureelectronics.com | Playwright **Firefox** (Akamai-HTTP/2 bypass) | ✅ |
| `scrape_rsonline.py` | rsonline.cn (RS 欧时) | curl_cffi + Next.js `__NEXT_DATA__` (Schema.org Product) | ✅ |
| `scrape_oneyac.py` | oneyac.com (唯样商城) | Playwright Firefox + tier-table extraction from first product card | ✅ |
| `scrape_ickey.py` | ickey.cn (云汉芯城) | Playwright Chromium (search hydrates via XHR) → per-supplier `/detail/<sku>/<MPN>.html` | ✅ |
| `scrape_rochester.py` | rocelec.com (Rochester EOL) | Playwright Firefox + LWC hydration + exact-MPN guard | ✅ |
| `scrape_bom2buy.py` | bom2buy.com (买芯片网) | Playwright + **user's Opera install** (session reuse to bypass IconCaptcha); single-MPN or `--mpns` batch mode with rate-limit. Requires Opera fully closed at run time and a pre-passed captcha session. | ✅ session-dependent |
| `batch_scraper_test.py` | — | Driver: subprocess per (MPN × channel), hard timeouts, consolidated CSV/XLSX/MD outputs | ✅ |
| `scrape_mouser_v2.py` | mouser.cn | curl_cffi cascade — blocked by Akamai bm-verify | ❌ |
| `scrape_arrow_v2.py` | arrow.com | curl_cffi cascade — blocked by Akamai _abck | ❌ |

## Imports

Scripts import the shared summary renderer from `common/`:

```python
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
from _summary import write_summary
```

## Reports & schemas

- `doc/batch_output_schema.md` — data contract for `batch_index.csv/.xlsx`, `batch_index.json`, and per-MPN folder layout. **Read this first if you're parsing batch output.**
- `doc/scraper_report_v3.md` — per-channel findings, blockers, lessons learned (v2 / v1 retained for history).
- `doc/source_technical_reference.md` — per-source cookbook (engine choice, DOM selectors, pitfalls, canonical mapping).

## Rule — new-source feasibility tests MUST reach the product detail page

When evaluating whether a new distributor source is scrapable, the test must navigate **search → click first matching result → product detail page** and demonstrate that canonical-schema fields (`manufacturer_part_number`, `manufacturer`, `stock_now_qty`, `stock_now_ship_text`, `stock_future_qty`, `stock_future_ship_text`, `stock_breakdown`, `prices`, `parameters`, `datasheet_url`, `lifecycle_status`, `package`) are recoverable from that detail HTML.

Stopping at the search page is **not enough**. The search page alone doesn't reveal:
- whether per-supplier prices / stock are visible vs. login-gated,
- whether spec parameters are exposed at all,
- whether the detail URL pattern is stable / scrapable,
- whether the page needs JS hydration / cookie consent / WAF bypass.

Every per-source probe folder must therefore include `_product.html` + `_product.png` (the **detail** page, not just `_search.*`) plus an `extracted.json` or `extracted_canonical.json` showing recoverable schema fields. If a probe can only get to the search page, document the specific blocker (WAF, no exact match, no detail URL pattern) and retry with a chip more likely to be in catalog.

Reference probes that meet this bar:
- `test/scraper/Test_BTA316-600E_127_ROCHESTER_*_detail/` — full canonical schema, 5 price tiers, datasheet URL, 14 spec fields.
- `test/scraper/Test_LIS2DH12TR_RSONLINE_*_detail/` — `__NEXT_DATA__` carries mpn/brand/stockStatus/displayPrice/breakQty1.
- `test/scraper/Test_LIS2DH12TR_ONEYAC_*_detail/` — rendered detail HTML with title, brand, spec table.

See `_NEW_SOURCES_DETAIL_PROBE_20260518.md` in `test/scraper/` for the working template.

## Deps

`pip install -r requirements.txt` (uses the project's `.venv/` at the repo root).

## Auto-updating this README

The `<!-- BEGIN AUTO:status --> … <!-- END AUTO:status -->` block at the top is regenerated by `scraper/scripts/_update_readme_status.py`. The rest of this file is hand-written. The regenerator runs:

- automatically at the end of every `batch_scraper_test.py` invocation (best-effort, never blocks the batch);
- automatically via the Claude Code PostToolUse hook in `.claude/settings.json` whenever files under `scraper/` or `test/scraper/` change in-session;
- manually any time, with `.venv/Scripts/python.exe scraper/scripts/_update_readme_status.py`.

To update the hand-maintained channel list (e.g. when a blocker is resolved), edit `CHANNEL_STATUS` at the top of the regenerator script.
