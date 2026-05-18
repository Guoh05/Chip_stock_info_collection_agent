# API Track — Distributor APIs

Track 2 of the chip-availability pipeline. Calls vendor APIs directly (Mouser
Search, Digikey Product Information, …) and normalizes results into the same
canonical schema as the scraper track (`scraper/`). One round-trip per part,
no browser, no anti-bot fight.

<!-- BEGIN AUTO:status — managed by api/scripts/_update_readme_status.py (see "Auto-updating this README" at bottom) -->

## Status snapshot (2026-05-18)

| Vendor | Endpoint | Auth | Working? |
|---|---|---|---|
| **Mouser** Search API v1 | `POST api.mouser.com/api/v1/search/partnumber (fallback /search/keyword)` | API key in querystring | ✅ |
| **Digikey** Product Information API v4 | `POST api.digikey.com/products/v4/search/keyword` | OAuth2 client_credentials → bearer | ✅ |
| Octopart / Nexar | not started | OAuth2 (keys not yet acquired) | ⏳ |
| Element14 / Farnell | not started | API key (key not yet acquired) | ⏳ |

**Latest batch run:** `test/api_test/BatchTest_20260517_16_07_16/` — 103 MPNs × 2 channel(s) = 206 calls.

| Channel | OK | No results | Failed | OK % |
|---|---|---|---|---|
| Mouser | 64 | 39 | 0 | 62.1 % |
| Digikey | 58 | 45 | 0 | 56.3 % |

Both channels returned a usable result for **57** of the 103 chips. Of those: 33 have stock at both, 16 only at Digikey, 2 only at Mouser, 6 factory-order at both.

**Manufacturer-name mismatches surfaced:** 2 — `HT66F0021 8SOP TR` (MOUSER: HOLTEK → ROHM Semiconductor), `EMW3080` (DIGIKEY: MXCHIP → Seeed Technology Co., Ltd).

<!-- END AUTO:status -->

## Why a separate track from scraping

The scraper track and API track are independent technical lines. They:

- Have **different dependencies** — this track pulls in `requests` +
  `python-dotenv` + `openpyxl` only; no Playwright / curl_cffi.
- Have **different reliability profiles** — APIs need keys + token management +
  rate-limit handling; scraping fights bot detection.
- Have **different paths to the same data** — e.g. Mouser is completely blocked
  for scraping (Akamai BMP `bm-verify`) but ships a free Search API. Digikey
  works for both, but the API is ~500 ms / part vs. ~30 s for the scraper.

Both tracks share:

- The output convention — same `Test_<MPN>_<CHANNEL>_<YYYYMMDD>_<HH>_<MM>_<SS>/`
  folder layout, but API runs go under `test/api_test/`.
- The canonical schema (`stock_now_qty` / `stock_future_qty` /
  `stock_breakdown` / `site_*`).
- The summary renderer (`common/_summary.py`).
- The MPN-variant grouping rule (different MPN strings → separate variant
  subfolders).
- The Python venv at `.venv/`.

## Folder layout

```
api/
├── scripts/
│   ├── api_mouser.py         single-MPN Mouser client
│   ├── api_digikey.py        single-MPN Digikey client (with OAuth token cache)
│   └── batch_api_test.py     batch driver: read xlsx → run all chips × both APIs
├── doc/
│   └── api_report_v1.md      full report — auth, mapping per vendor, gotchas
├── requirements.txt          requests, python-dotenv, openpyxl
├── .env                      real keys (gitignored)
├── .env.example              placeholders
└── README.md                 this file
```

## Conventions

- **Channel codes** (used in the `Test_<MPN>_<CHANNEL>_<ts>/` folder name and
  the JSON `channel` field) match the scraper track for the same vendor:
  `MOUSER`, `DIGIKEY`, `OCTOPART`, `ELEMENT14`, ….
- **Method label** in the JSON `method` field: `api_<vendor>_v<n>` — e.g.
  `api_mouser_v1`, `api_digikey_v4`.
- **Attempts log** is still mandatory — even API calls fail (auth, 5xx, no
  results). Every call appends to `attempts: []`.
- **Imports** from `common/`:
  ```python
  import sys as _sys
  from pathlib import Path as _Path
  _sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "common"))
  from _summary import write_summary
  ```

## Credentials

API keys go in environment variables read from `api/.env` via `python-dotenv`:

```
MOUSER_API_KEY=...
DIGIKEY_CLIENT_ID=...
DIGIKEY_CLIENT_SECRET=...
NEXAR_CLIENT_ID=...        # not yet used
NEXAR_CLIENT_SECRET=...
ELEMENT14_API_KEY=...
```

`api/.env.example` documents the names without values. The real `.env` is
gitignored and MUST NOT be committed. Scripts use `os.environ.get(...)` only —
no value is ever echoed to stdout, logs, or output files.

**Mouser key locale gotcha:** the registered key is bound to mouser.cn → every
response comes back localized to zh-CN (Availability `"108590 库存量"`, lead
time in days suffixed `"天数"`, currency `RMB ¥`). For a USD/weeks view a key
registered on mouser.com would be needed.

## Run

### Single MPN
```bash
.venv/Scripts/python.exe api/scripts/api_mouser.py STM32G030F6P6
.venv/Scripts/python.exe api/scripts/api_digikey.py "BT168GW,115"
```
Quote MPNs containing commas or spaces. Output: one timestamped folder under
`test/api_test/` with `<MPN>.json`, `parent_summary.md`, `raw_response.json`,
and one per-variant subfolder for each distinct returned MPN string.

### Full batch (all 103 MPNs from the xlsx × both APIs)
```bash
.venv/Scripts/python.exe api/scripts/batch_api_test.py
# Helpful flags:
.venv/Scripts/python.exe api/scripts/batch_api_test.py --limit 3        # dry-run
.venv/Scripts/python.exe api/scripts/batch_api_test.py --only DIGIKEY   # one channel
.venv/Scripts/python.exe api/scripts/batch_api_test.py --throttle 0.5   # slower
.venv/Scripts/python.exe api/scripts/batch_api_test.py --xlsx OTHER.xlsx
```
Output: `test/api_test/BatchTest_<ts>/` containing `batch_summary.md`,
`batch_index.csv/.xlsx`, `batch_compare.csv/.xlsx`, `batch_index.json`,
`batch_input.csv`, `failures.md`, plus one `Test_<sanitized_mpn>_<CHANNEL>/`
folder per call (identical shape to a single-MPN run).

## Canonical schema (mandatory)

Every per-variant record carries:

| Field | Meaning |
|---|---|
| `stock_now_qty` | 现货 quantity (the distributor's own warehouse) |
| `stock_now_ship_text` | The API's SLA / ship-time string, or a constant if not provided |
| `stock_future_qty` | 期货 / 在途 quantity — `null` if API doesn't disclose a bounded number |
| `stock_future_ship_text` | Lead-time / factory-stock text |
| `stock_breakdown` | `[{label, warehouse, quantity, ship_text, note?}, …]` using the **API's own field names** in `label` |
| `site_*` keys | API-native fields preserved verbatim alongside, for audit |

See `../common/_summary.py` for how these render into `<MPN>_summary.md`, and
`../README.md` (project root) for the cross-track rationale.

**MPN-variant grouping rule:** keyword/fuzzy searches that return multiple
distinct `ManufacturerPartNumber` strings → one variant subfolder per string.
Never aggregate across MPN strings.

## OAuth token cache (Digikey)

`api_digikey.py::fetch_token()` carries a module-level cache keyed by
`client_id`. Within one Python process, the first call hits the OAuth endpoint;
subsequent calls reuse the cached bearer until 30 s before expiry. The batch
driver reuses one token across 103 calls; each cache hit logs an attempts entry
with `outcome: "cached"`.

## Throttle / quota

- Mouser free tier: ~1,000 calls/day.
- Digikey Production: ~1,000 calls/day (token TTL 599 s).
- Batch defaults: 0.3 s sleep between successive calls (≈ 30 s overhead per
  batch). No 429 / 5xx observed in any run so far.

## Where to dig deeper

- `doc/batch_output_schema.md` — data contract for `batch_index.csv/.xlsx`,
  `batch_compare.csv/.xlsx`, `batch_index.json`, and the per-MPN folder layout.
  **Read this first if you're parsing batch output.**
- `doc/api_report_v1.md` — full report: auth flow per vendor, canonical
  mapping (Mouser `Availability/FactoryStock/AvailabilityOnOrder` → 现货/期货,
  Digikey `QuantityAvailable/ManufacturerLeadWeeks` → 现货/期货), gotchas, and
  test results per MPN.
- Project root `../README.md` — cross-track schema rationale and the canonical
  field reference.
- Memory (out of tree, at `~/.claude/projects/.../memory/`):
  - `project_api_state.md` — per-vendor state, env-var names, latest test runs.
  - `project_batch_state.md` — batch driver design + most recent sweep results.
  - `feedback_stock_breakdown_fields.md` — the canonical-schema rule.
  - `feedback_mpn_variant_grouping.md` — the per-variant rule.
  - `feedback_site_native_fields.md` — preserve-site-wording rule.

## Auto-updating this README

The `<!-- BEGIN AUTO:status --> … <!-- END AUTO:status -->` block at the top is
regenerated by `api/scripts/_update_readme_status.py`. The rest of this file
(including the "Where to dig deeper" pointer list) is hand-written and must be
edited by hand when new docs land. The regenerator runs:

- automatically at the end of every `batch_api_test.py` invocation (best-effort,
  never blocks the batch);
- automatically via the Claude Code PostToolUse hook in `.claude/settings.json`
  whenever files under `api/scripts/` change in-session (the hook dispatcher
  lives at `.claude/hooks/readme_postupdate.py`);
- manually any time, with
  `.venv/Scripts/python.exe api/scripts/_update_readme_status.py`.

To update the hand-maintained vendor list (e.g. when Octopart/Element14 goes
live), edit `VENDOR_STATUS` at the top of the regenerator script.
