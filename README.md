# Chip Availability — Cross-Distributor Data Pipeline

Goal: for any manufacturer part number (MPN), produce a unified view of **availability, lead time, and pricing** across multiple distributors.

Two independent technical tracks, each writing into its own `test/` subroot (`test/scraper_test/` and `test/api_test/`) and emitting the same canonical schema so results are directly comparable.

```
02_work_chip_availability/
├── scraper/                    # Track 1 — Web scraping
│   ├── scripts/                #   scrape_lcsc_v3.py, scrape_digikey.py, scrape_hqew.py,
│   │                           #   scrape_future.py, scrape_mouser_v2.py, scrape_arrow_v2.py
│   ├── doc/                    #   scraper_report_v1.md, scraper_report_v2.md
│   ├── requirements.txt
│   └── README.md
│
├── api/                        # Track 2 — Distributor APIs (NEW)
│   ├── scripts/                #   (per-vendor API clients — Mouser, Digikey, Octopart, …)
│   ├── doc/                    #   api_report_v*.md
│   ├── requirements.txt
│   ├── .env.example            #   API-key placeholders
│   └── README.md
│
├── common/                     # Cross-track shared code
│   ├── _summary.py             #   Canonical <MPN>_summary.md renderer
│   └── _backfill_summaries.py  #   One-shot regenerator (walks test/ + one level deep)
│
├── test/                       # Output, split by track
│   ├── scraper_test/           #   ← scraper/ scripts write here
│   │   └── Test_<MPN>_<CHANNEL>_<YYYYMMDD>_<HH>_<MM>_<SS>/
│   └── api_test/               #   ← api/ scripts write here
│
├── ref/                        # Reference docs (datasheets, site notes, etc.)
├── temp/                       # Scratch workspace (case_screenshot/, etc.)
├── .venv/                      # Python venv shared by both tracks
└── .claude/                    # Claude settings, skills
```

## Canonical schema (both tracks MUST emit)

Every per-variant record carries these scalars + structure regardless of channel:

| Field | Meaning |
|---|---|
| `stock_now_qty` | 现货 (immediately shippable) quantity |
| `stock_now_ship_text` | 发货时间 string for 现货 |
| `stock_future_qty` | 期货 / 在途 quantity (`null` if unbounded factory order) |
| `stock_future_ship_text` | 发货时间 string for future stock |
| `stock_breakdown` | `[{label, warehouse, quantity, ship_text, note?}, …]` — preserves the site/API's native labels |
| `site_*` keys | site/API-native field names alongside (e.g. `site_global_stock`, `gdWarehouseStockNumber`) |

Plus: `prices`, `parameters`, `manufacturer_part_number`, `manufacturer`, `package`, `datasheet_url`, `attempts`, `method`, `data_quality`, `paywall`.

## Output convention

Both tracks write per-MPN-per-channel folders under their own subroot — scraper to `test/scraper_test/`, API to `test/api_test/` — using the shared naming convention `Test_<MPN>_<CHANNEL>_<YYYYMMDD>_<HH>_<MM>_<SS>/` containing:
- `<MPN>.json` — full normalized record
- `<MPN>_summary.md` — human-readable view (rendered by `common/_summary.py`)
- raw response artefacts (HTML, JSON dumps, screenshots) as available

When a search returns multiple distinct MPN variants (e.g. `STM32G030F6P6` returns both base part and `STM32G030F6P6TR`), each variant goes into its own subfolder under the parent run folder.

## Memory / context

`MEMORY.md` (at `~/.claude/projects/.../memory/`) indexes the cross-track rules:
- Folder convention (`feedback_test_output_folder.md`)
- Canonical stock fields (`feedback_stock_breakdown_fields.md`)
- MPN-variant grouping (`feedback_mpn_variant_grouping.md`)
- Site-native field preservation (`feedback_site_native_fields.md`)
- Per-channel state (`project_scrape_state.md`)

## Status (2026-05-17)

| Source | Scraper track | API track |
|---|---|---|
| LCSC | ✅ working | not started |
| Digikey | ✅ working | not started |
| Mouser | ❌ blocked (Akamai) | not started — has free API |
| Arrow | ❌ blocked (Akamai) | not started |
| HQEW (华强电子网) | ✅ working | unlikely (B2B marketplace, no public API) |
| Future Electronics | ✅ working | not started |
