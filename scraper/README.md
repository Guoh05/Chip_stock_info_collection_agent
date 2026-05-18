# Scraper Track — Web scraping

Track 1 of the chip-availability pipeline. Drives distributor websites with `curl_cffi` + Playwright to extract availability, pricing, and parameters.

<!-- BEGIN AUTO:status — managed by scraper/scripts/_update_readme_status.py (see "Auto-updating this README" at bottom) -->

## Status snapshot (2026-05-18)

| Channel | Method | Working? |
|---|---|---|
| **LCSC** (szlcsc.com) | Playwright Chromium `--headless=new` + `__NEXT_DATA__` + DOM right-panel | ✅ |
| **Digikey** (digikey.cn) | Playwright stealth Chromium + `__NEXT_DATA__` envelope | ✅ |
| **HQEW** (华强电子网, hqew.com) | Playwright Chromium + supplier-table DOM scrape | ✅ |
| **Future** (futureelectronics.com) | Playwright **Firefox** (Akamai HTTP/2 bypass) | ✅ |
| Mouser (mouser.cn) | curl_cffi cascade — blocked by Akamai BotManager `bm-verify` | ❌ |
| Arrow (arrow.com) | curl_cffi cascade — blocked by Akamai BotManager `_abck` | ❌ |

**Latest batch run:** `test/scraper_test/BatchTest_20260518_11_25_53/` — 3 MPNs × 4 channel(s) = 12 calls.

| Channel | OK | No results | Blocked | Failed | OK % |
|---|---|---|---|---|---|
| LCSC | 3 | 0 | 0 | 0 | 100.0 % |
| Digikey | 3 | 0 | 0 | 0 | 100.0 % |
| HQEW | 3 | 0 | 0 | 0 | 100.0 % |
| Future | 3 | 0 | 0 | 0 | 100.0 % |

Cross-channel coverage: **3** chip(s) returned ok on all 4 channels.

No manufacturer-name mismatches in the latest run.

<!-- END AUTO:status -->

## Single-MPN run

```bash
# from the project root (02_work_chip_availability/)
.venv/Scripts/python.exe scraper/scripts/scrape_lcsc_v3.py STM32G030F6P6
.venv/Scripts/python.exe scraper/scripts/scrape_digikey.py STM32G030F6P6
.venv/Scripts/python.exe scraper/scripts/scrape_hqew.py STM32G030F6P6
.venv/Scripts/python.exe scraper/scripts/scrape_future.py "ATXMEGA32E5-ANR"
```

Each run writes a folder under `test/scraper_test/Test_<MPN>_<CHANNEL>_<YYYYMMDD>_<HH>_<MM>_<SS>/`.

Optional `argv[2]` overrides the output directory (used by the batch driver — see below). When omitted, the auto-timestamp path above is used.

## Batch run — all chips from the master sheet

```bash
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py            # full sweep, 4 channels
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --limit 3  # dry-run
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --only LCSC,HQEW
.venv/Scripts/python.exe scraper/scripts/batch_scraper_test.py --resume   # top up most recent BatchTest_*
```

CLI flags:

| Flag | Default | Purpose |
|---|---|---|
| `--xlsx PATH` | `ref/Chip_DataSource_Master.xlsx` | Input chip list (sheet 1, header row 4, MPN in col 1, mfr in col 2). |
| `--limit N` | none | Process only the first N valid MPNs (dry-run aid). |
| `--only CSV` | all 4 | Comma-separated subset of `LCSC,DIGIKEY,HQEW,FUTURE`. |
| `--throttle SEC` | `1.0` | Sleep between chips (politeness gap). |
| `--resume` | off | Reuse the most recent `BatchTest_*` folder and skip per-MPN-per-channel runs whose parent JSON already exists. |
| `--sequential` | off | Run channels one at a time per chip (old behavior). Default is to fan out all selected channels concurrently. |

Each (MPN × channel) call runs as its own **subprocess** with a hard wallclock timeout (LCSC 240s, Digikey 180s, HQEW 90s, Future 300s), so a hung browser cannot stall the batch. UTF-8 is forced in the subprocess env so `¥` / Chinese prints don't crash Windows GBK stdout capture.

**Channel parallelism (default):** For each chip, all selected channel subprocesses run concurrently via a `ThreadPoolExecutor` (one worker per channel). Since each channel hits a different domain there's no per-vendor rate-limit collision, and the chip's wallclock drops from `sum(channel times)` to `max(channel times)`. Observed speedup on the 103-chip × 4-channel sweep: ~53% (chip wallclock 40–50 s vs ~90 s sequential; full batch ~75 min vs 208 min). RAM cost: 4 concurrent browsers, peak ~2–4 GB. Use `--sequential` to opt out if RAM-constrained or debugging a single channel.

Output goes to `test/scraper_test/BatchTest_<YYYYMMDD>_<HH_MM_SS>/`:

- `batch_summary.md` — TL;DR, per-channel pass rate, cross-channel coverage histogram, top-5 stock per channel, manufacturer mismatches, skipped rows
- `batch_index.csv` / `.xlsx` — long form, 20 cols (one row per MPN × channel)
- `batch_compare.csv` / `.xlsx` — wide form, 43 cols (10 fields × 4 channels + 2 input cols + disagreement flag)
- `batch_index.json` — machine-readable, includes subprocess stdout/stderr tails
- `batch_input.csv` — verbatim (MPN, expected_mfr) input
- `failures.md` — non-ok rows grouped by channel
- `Test_<sanitized_mpn>_<CHANNEL>/` — per-MPN-per-channel run folders (populated by each subprocess)

Sanitization: non-alphanumeric except `.`, `_`, `-` → `_` (e.g. `PIC16F18446T-I/SS` → `PIC16F18446T-I_SS`).

First full sweep ran 2026-05-17 (103 chips × 4 channels, 208 min elapsed): LCSC 77.7 % / Digikey 55.3 % / HQEW 75.7 % / Future 42.7 % OK. See `project_scrape_batch_state.md` in memory for the post-mortem.

## What each script does

| Script | Site | Method | Status |
|---|---|---|---|
| `scrape_lcsc_v3.py` | szlcsc.com | Playwright Chromium `--headless=new` + `__NEXT_DATA__` + DOM right-panel | ✅ |
| `scrape_digikey.py` | digikey.cn | Playwright stealth Chromium + `__NEXT_DATA__` envelope | ✅ |
| `scrape_hqew.py` | hqew.com | Playwright Chromium + supplier table scrape | ✅ |
| `scrape_future.py` | futureelectronics.com | Playwright **Firefox** (Akamai-HTTP/2 bypass) | ✅ |
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

- `doc/batch_output_schema.md` — data contract for `batch_index.csv/.xlsx`, `batch_compare.csv/.xlsx`, `batch_index.json`, and per-MPN folder layout. **Read this first if you're parsing batch output.**
- `doc/scraper_report_v2.md` — per-channel findings, blockers, lessons learned (v1 retained for history).

## Deps

`pip install -r requirements.txt` (uses the project's `.venv/` at the repo root).

## Auto-updating this README

The `<!-- BEGIN AUTO:status --> … <!-- END AUTO:status -->` block at the top is regenerated by `scraper/scripts/_update_readme_status.py`. The rest of this file is hand-written. The regenerator runs:

- automatically at the end of every `batch_scraper_test.py` invocation (best-effort, never blocks the batch);
- automatically via the Claude Code PostToolUse hook in `.claude/settings.json` whenever files under `scraper/` or `test/scraper_test/` change in-session;
- manually any time, with `.venv/Scripts/python.exe scraper/scripts/_update_readme_status.py`.

To update the hand-maintained channel list (e.g. when a blocker is resolved), edit `CHANNEL_STATUS` at the top of the regenerator script.
