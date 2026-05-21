# Pipeline orchestrator — workflow & contract

`common/run_pipeline.py` is the default entry for end-to-end chip-availability
runs. It chains the three drivers + bom2buy backfill + merge into a single
command, with state tracking + resume so failures don't force a full re-run.

This doc is the **contract** between `run_pipeline.py` and the upstream
components. Per CLAUDE.md Hard Rule #7, when you edit the CLI / output paths /
preconditions of any pipeline component, also update this doc.

## Components in the pipeline

| Component | Owned by | What it does |
|---|---|---|
| `api/scripts/batch_api_test.py` | api track | Sweep N chips × API sources, write `<env>/api/BatchTest_<ts>/` |
| `scraper/scripts/batch_scraper_test.py` | scraper track | Sweep N chips × 8 scrapers (excluding bom2buy when `--no-bom2buy`), write `<env>/scraper/BatchTest_<ts>/` |
| `scraper/scripts/scrape_bom2buy.py` | scraper track | bom2buy-only scrape (Opera-driven, captcha-sensitive) |
| `scraper/scripts/_merge_bom2buy_into_batch.py` | scraper track | Folds bom2buy cells into an existing scraper BatchTest's `batch_index.*` |
| `common/merge_batch_for_procurement.py` | common | Reads api + scraper batches, writes `Versuni_chip_stock_availability_check_<YYYYMMDD>.xlsx` to `<env>/merged/Merge_.../` |

## Phase sequence

The orchestrator runs phases **serially**:

```
1.  API           batch_api_test.py --env <env>
2a. Scraper main  batch_scraper_test.py --env <env> --no-bom2buy
2b. bom2buy       scrape_bom2buy.py --mpns-file <auto> --out <scraper_batch_dir>
                  _merge_bom2buy_into_batch.py <scraper_batch_dir>
3.  Merge         merge_batch_for_procurement.py --env <env> --api <dir> --scr <dir>
                  (or with --api-only / --scraper-only on partial failure)
```

**Why bom2buy is its own phase** (not embedded in scraper via `--with-bom2buy`):
bom2buy is sensitive to manual Opera + captcha prep. Isolating it as a
separate phase means a captcha failure during 2b does not invalidate 2a's
data; the user solves the captcha and resumes 2b only.

**Why merge always uses explicit `--api <dir> --scr <dir>`** instead of merge's
default "newest batch": prevents a parallel session from racing and creating
a newer batch that the merge would mistakenly grab.

## CLI surface

```
common/run_pipeline.py
  --env {test,prod}          Output root (default test).
  --limit N                  Pass-through to api + scraper (and bom2buy
                             inherits via the scraper batch's batch_input.csv).
  --mpns "A;B;..."           Pass-through (semicolon-separated, optional :MFR).
  --mpns-file PATH           Pass-through (tab-separated MPN<TAB>MFR).
  --xlsx PATH                Pass-through (chip list xlsx).

  --skip-api                 Don't run phase 1. Merge will use --scraper-only.
  --skip-scraper             Don't run phase 2a (implies --skip-bom2buy).
                             Merge will use --api-only.
  --skip-bom2buy             Don't run phase 2b. Other scraper data unaffected.
  --skip-merge               Stop after batches.

  --resume                   Continue from existing state file. Re-runs phases
                             in {failed, running}; leaves {ok, skipped} alone.

  --api-args "..."           Extra flags appended to batch_api_test.py call.
  --scraper-args "..."       Extra flags appended to batch_scraper_test.py call.
  --merge-args "..."         Extra flags appended to merge call.
```

**Drift-friendly passthrough.** The orchestrator hard-codes only flags that
exist on multiple drivers (`--env`, `--limit`, `--mpns`, `--mpns-file`,
`--xlsx`). Anything else flows through `--api-args` / `--scraper-args` /
`--merge-args` so a new upstream flag doesn't force an orchestrator change.

**No `--only` on the orchestrator.** `api`'s `--only` is repeated-arg
(`--only MOUSER --only ARROW`), `scraper`'s is comma-separated
(`--only LCSC,HQEW`). Inconsistency → handle per-side via the `--*-args`
passthrough.

## State file

Location: `<env_root>/.pipeline_state.json` (i.e. `test/.pipeline_state.json`
or `production/.pipeline_state.json`).

Schema (v1):

```json
{
  "version": 1,
  "env": "test",
  "started_at": "2026-05-21T14:00:00",
  "cli_args": {"limit": null, "mpns": null, "mpns_file": null, "xlsx": null, "with_bom2buy": true},
  "phases": {
    "api":          {"status": "ok",     "batch_dir": "test/api/BatchTest_...",     "started_at": "...", "ended_at": "..."},
    "scraper_main": {"status": "ok",     "batch_dir": "test/scraper/BatchTest_...", "started_at": "...", "ended_at": "..."},
    "bom2buy":      {"status": "failed", "error": "captcha session expired",        "started_at": "...", "ended_at": "..."},
    "merge":        {"status": "pending"}
  }
}
```

`status` values: `pending` → `running` → (`ok` | `failed`). `skipped` is set
when `--skip-<phase>` is passed and is terminal.

`--resume` walks `PHASE_ORDER` and re-runs any phase whose status is
`failed`, `running`, or `pending`; leaves `ok` / `skipped` alone.

## Failure handling

On non-zero exit from any phase subprocess, the orchestrator:

1. Marks the phase `failed` in state + records the error.
2. Saves state, exits with code 2 (distinct from script-internal errors at 1).
3. Prints actionable next steps. For bom2buy captcha (exit code 3 from
   `scrape_bom2buy.py`) the prompt includes the Opera prep steps.

Skipping a failed phase + resuming is the documented way to produce a
partial-data merge. The merge phase auto-selects `--api-only` /
`--scraper-only` based on which sides are `ok` in state.

## Preconditions

- **API track**: `api/.env` must contain credentials for any source the
  drivers will call (Mouser, Digikey OAuth, Element14, Arrow, LCSC HMAC).
  `--api-args "--only DIGIKEY"` to test with fewer credentials.
- **Scraper track**: `.venv` must have Playwright browsers installed
  (`playwright install chromium firefox`).
- **bom2buy**: requires Opera browser + a captcha-cleared session.
  Workflow when `scrape_bom2buy.py` exits with code 3 (captcha expired):
  open Opera → bom2buy.com → solve IconCaptcha → fully close Opera → resume.
- **LCSC API quota**: 200 calls / endpoint / day. Full 107-chip sweep is
  fine; back-to-back sweeps on the same day will exceed quota.

## Smoke test

Run this any time you edit a pipeline component:

```bash
.venv/Scripts/python.exe common/run_pipeline.py \
    --limit 1 \
    --api-args "--only DIGIKEY" \
    --scraper-args "--only DIGIKEY" \
    --skip-bom2buy
```

Expected: < 2 min, produces a merged xlsx under `test/merged/Merge_.../
Versuni_chip_stock_availability_check_<YYYYMMDD>.xlsx`.

## Maintenance checklist

When you edit any of:

- `common/merge_batch_for_procurement.py`
- `api/scripts/batch_api_test.py`
- `scraper/scripts/batch_scraper_test.py`
- `scraper/scripts/scrape_bom2buy.py`
- `scraper/scripts/_merge_bom2buy_into_batch.py`

…also check:

- [ ] Did the CLI surface change? Update `common/run_pipeline.py` if a
      flag the orchestrator relies on was renamed/removed.
- [ ] Did the output path or folder convention change?
- [ ] Did the precondition change (new manual step, new env var)?
- [ ] Did the exit code semantics change (e.g., a new "skip-able" failure)?
- [ ] Update this doc.
- [ ] Run the smoke test.

A PostToolUse hook (`check_pipeline_sync.py`) fires on edits to the files
above and prints a reminder. The semantic judgment is the editor's.

## Out of scope (v1.7)

- Parallel phases (API + scraper concurrently). Currently serial — saves
  ~5 min wallclock but complicates failure attribution.
- Auto-retry on transient failures (no current upstream classifies its errors
  as transient vs permanent).
- Notifications on completion / failure (Slack, email, etc.).
- Per-phase timeouts at the orchestrator level (each driver enforces its
  own timeouts internally).
- Multi-user lock on the state file (single-user assumption; if two
  invocations race the second one will clobber state).
