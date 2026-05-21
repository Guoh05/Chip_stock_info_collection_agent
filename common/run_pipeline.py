"""Chip-availability pipeline orchestrator — runs API + scraper + bom2buy + merge.

Thin subprocess wrapper around the three drivers + bom2buy backfill. Designed
for the "I need to check N chips end-to-end" use case where you'd otherwise
copy-paste 3-4 commands. Drift-friendly: advanced flags pass through via
`--api-args` / `--scraper-args` / `--merge-args` so this script doesn't have
to track every upstream argparse change.

# Phases

    1.  API     — api/scripts/batch_api_test.py
    2a. Scraper — scraper/scripts/batch_scraper_test.py --no-bom2buy
    2b. bom2buy — scrape_bom2buy.py + _merge_bom2buy_into_batch.py
                  (split out so captcha failures isolate cleanly)
    3.  Merge   — common/merge_batch_for_procurement.py

Phases are SERIAL. On failure, state is saved to
`<env_root>/.pipeline_state.json` and the script exits 2 with actionable
next-steps printed. Resume via `--resume`; the orchestrator picks up from the
first non-`ok` phase. Use `--skip-<phase>` to skip a phase that failed (the
merge will run in `--api-only` or `--scraper-only` mode if one side is
missing).

# State machine

Each phase records `status` ∈ {pending, running, ok, failed, skipped}, plus
`batch_dir` / `error` / timestamps. `pending` → `running` → `ok` / `failed`.
`skipped` is terminal. Resume retries `failed` and `running` (treats running
as crashed mid-flight); leaves `ok`/`skipped` alone.

# Usage

    .venv/Scripts/python.exe common/run_pipeline.py                          # full sweep, test env
    .venv/Scripts/python.exe common/run_pipeline.py --limit 3                # 3-chip dry-run
    .venv/Scripts/python.exe common/run_pipeline.py --mpns "STM32G030F6P6;BT168GW,115"
    .venv/Scripts/python.exe common/run_pipeline.py --env prod               # production output
    .venv/Scripts/python.exe common/run_pipeline.py --resume                 # continue after a failure
    .venv/Scripts/python.exe common/run_pipeline.py --resume --skip-scraper  # skip scraper, merge api only
    .venv/Scripts/python.exe common/run_pipeline.py --skip-bom2buy           # skip bom2buy from the start

# Smoke test (after editing any pipeline component)

    .venv/Scripts/python.exe common/run_pipeline.py --limit 1 \
        --api-args "--only DIGIKEY" \
        --scraper-args "--only DIGIKEY" \
        --skip-bom2buy

Should complete in < 2 min and produce a merged xlsx.
"""
from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_ROOTS = {
    "test": PROJECT_ROOT / "test",
    "prod": PROJECT_ROOT / "production",
}
PYTHON = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
STATE_VERSION = 1
PHASE_ORDER = ("api", "scraper_main", "bom2buy", "merge")
BOM2BUY_CAPTCHA_EXIT_CODE = 3


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- state file ------------------------------------------------------

def state_path(env: str) -> Path:
    return ENV_ROOTS[env] / ".pipeline_state.json"


def init_state(args: argparse.Namespace) -> dict:
    return {
        "version": STATE_VERSION,
        "env": args.env,
        "started_at": now_iso(),
        "cli_args": {
            "limit": args.limit,
            "mpns": args.mpns,
            "mpns_file": args.mpns_file,
            "xlsx": str(args.xlsx) if args.xlsx else None,
            "with_bom2buy": not args.skip_bom2buy,
        },
        "phases": {p: {"status": "pending"} for p in PHASE_ORDER},
    }


def load_state(env: str) -> dict | None:
    p = state_path(env)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"state file is corrupted ({p}): {e}")


def save_state(state: dict, env: str) -> None:
    p = state_path(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


class PhaseFailure(Exception):
    def __init__(self, phase: str, msg: str, hint: str = ""):
        self.phase = phase
        self.msg = msg
        self.hint = hint
        super().__init__(msg)


# ---------- subprocess helper -----------------------------------------------

def run_cmd(cmd: list[str], phase: str) -> None:
    """Run a subprocess, streaming output. Raise PhaseFailure on non-zero exit."""
    print(f"\n[{phase}] $ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if r.returncode != 0:
        raise PhaseFailure(phase, f"exit code {r.returncode}", hint=f"(rc={r.returncode})")


def snapshot_batches(track_root: Path) -> set[str]:
    if not track_root.exists():
        return set()
    return {p.name for p in track_root.iterdir() if p.is_dir() and p.name.startswith("BatchTest_")}


def new_batch_dir(track_root: Path, before: set[str]) -> Path:
    after = snapshot_batches(track_root)
    new = sorted(after - before)
    if not new:
        raise PhaseFailure("?", f"no new BatchTest_* folder appeared in {track_root}")
    if len(new) > 1:
        # Unlikely but defensive — pick newest by name (= newest by ts due to naming).
        print(f"  [warn] multiple new batches found, picking last: {new[-1]}")
    return track_root / new[-1]


# ---------- phases ----------------------------------------------------------

def run_phase_api(state: dict, args: argparse.Namespace) -> None:
    env_root = ENV_ROOTS[args.env]
    before = snapshot_batches(env_root / "api")
    cmd = [PYTHON, "api/scripts/batch_api_test.py", "--env", args.env]
    cmd.extend(_passthrough_common(args))
    if args.api_args:
        cmd.extend(shlex.split(args.api_args))
    run_cmd(cmd, "api")
    batch_dir = new_batch_dir(env_root / "api", before)
    state["phases"]["api"]["batch_dir"] = str(batch_dir.relative_to(PROJECT_ROOT))


def run_phase_scraper_main(state: dict, args: argparse.Namespace) -> None:
    env_root = ENV_ROOTS[args.env]
    before = snapshot_batches(env_root / "scraper")
    cmd = [PYTHON, "scraper/scripts/batch_scraper_test.py", "--env", args.env, "--no-bom2buy"]
    cmd.extend(_passthrough_common(args))
    if args.scraper_args:
        cmd.extend(shlex.split(args.scraper_args))
    run_cmd(cmd, "scraper_main")
    batch_dir = new_batch_dir(env_root / "scraper", before)
    state["phases"]["scraper_main"]["batch_dir"] = str(batch_dir.relative_to(PROJECT_ROOT))


def run_phase_bom2buy(state: dict, args: argparse.Namespace) -> None:
    """Run bom2buy on the same MPN list scraper_main used, then merge into its batch."""
    scr = state["phases"]["scraper_main"].get("batch_dir")
    if not scr:
        raise PhaseFailure(
            "bom2buy",
            "no scraper batch dir — scraper_main must succeed (or be resumed) before bom2buy",
        )
    scr_abs = PROJECT_ROOT / scr
    input_csv = scr_abs / "batch_input.csv"
    if not input_csv.exists():
        raise PhaseFailure("bom2buy", f"missing {input_csv}")

    # Translate scraper's batch_input.csv → tab-separated MPN<TAB>MFR for scrape_bom2buy.
    tsv = scr_abs / "_bom2buy_input.tsv"
    with input_csv.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    tsv.write_text(
        "\n".join(f"{r['input_mpn']}\t{r.get('expected_mfr') or ''}" for r in rows) + "\n",
        encoding="utf-8",
    )

    # Phase 2b.1 — scrape bom2buy cells INTO the existing scraper batch dir.
    cmd = [PYTHON, "scraper/scripts/scrape_bom2buy.py",
           "--mpns-file", str(tsv), "--out", str(scr_abs)]
    print(f"\n[bom2buy] $ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if r.returncode == BOM2BUY_CAPTCHA_EXIT_CODE:
        raise PhaseFailure(
            "bom2buy",
            "captcha session expired — Opera prep required",
            hint="captcha",
        )
    if r.returncode != 0:
        raise PhaseFailure("bom2buy", f"scrape_bom2buy exit {r.returncode}")

    # Phase 2b.2 — fold the bom2buy cells into batch_index.csv/.xlsx/.json.
    cmd2 = [PYTHON, "scraper/scripts/_merge_bom2buy_into_batch.py", str(scr_abs)]
    run_cmd(cmd2, "bom2buy")


def run_phase_merge(state: dict, args: argparse.Namespace) -> None:
    api_ph = state["phases"]["api"]
    scr_ph = state["phases"]["scraper_main"]
    api_ok = api_ph["status"] == "ok"
    scr_ok = scr_ph["status"] == "ok"
    if not api_ok and not scr_ok:
        raise PhaseFailure(
            "merge",
            "both api and scraper phases are non-ok — nothing to merge",
        )

    cmd = [PYTHON, "common/merge_batch_for_procurement.py", "--env", args.env]
    if api_ok:
        cmd.extend(["--api", api_ph["batch_dir"]])
    if scr_ok:
        cmd.extend(["--scr", scr_ph["batch_dir"]])
    if not api_ok:
        cmd.append("--scraper-only")
    elif not scr_ok:
        cmd.append("--api-only")
    if args.merge_args:
        cmd.extend(shlex.split(args.merge_args))
    run_cmd(cmd, "merge")
    state["phases"]["merge"]["mode"] = (
        "api-only" if not scr_ok else "scraper-only" if not api_ok else "full"
    )


PHASE_RUNNERS = {
    "api": run_phase_api,
    "scraper_main": run_phase_scraper_main,
    "bom2buy": run_phase_bom2buy,
    "merge": run_phase_merge,
}


def _passthrough_common(args: argparse.Namespace) -> list[str]:
    """Flags shared by api + scraper drivers."""
    out: list[str] = []
    if args.limit is not None:
        out += ["--limit", str(args.limit)]
    if args.mpns:
        out += ["--mpns", args.mpns]
    if args.mpns_file:
        out += ["--mpns-file", args.mpns_file]
    if args.xlsx:
        out += ["--xlsx", str(args.xlsx)]
    return out


# ---------- failure UX ------------------------------------------------------

def print_actionable_error(state: dict, env: str, phase: str, err: PhaseFailure) -> None:
    sp = state_path(env)
    print()
    print(f"❌ Phase {phase!r} failed")
    print(f"   Error: {err.msg}")
    print(f"   State: {sp.relative_to(PROJECT_ROOT)}")
    print()
    print("   Next steps:")
    if phase == "bom2buy" and err.hint == "captcha":
        print("   1. Open Opera (https://www.bom2buy.com/) and solve the IconCaptcha.")
        print("   2. FULLY close Opera (kill opera.exe in Task Manager if needed).")
        print(f"   3. Resume: {PYTHON} common/run_pipeline.py --resume --env {env}")
        print("      Or skip bom2buy and proceed to merge:")
        print(f"      {PYTHON} common/run_pipeline.py --resume --skip-bom2buy --env {env}")
    else:
        print(f"     Retry the failed phase: --resume --env {env}")
        skip_flag = f"--skip-{phase.replace('_main', '')}" if phase != "merge" else None
        if skip_flag:
            print(f"     Skip it and continue:    --resume {skip_flag} --env {env}")


def print_final_summary(state: dict) -> None:
    print()
    print("=" * 60)
    print("Pipeline complete.")
    for phase in PHASE_ORDER:
        ph = state["phases"][phase]
        st = ph["status"]
        extra = ""
        if "batch_dir" in ph:
            extra = f"  ({ph['batch_dir']})"
        elif phase == "merge" and "mode" in ph:
            extra = f"  ({ph['mode']})"
        print(f"  {phase:<14} {st}{extra}")


# ---------- main ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", choices=("test", "prod"), default="test",
                    help="Output env root. Default test (test/{api,scraper,merged}/); "
                         "prod writes to production/{...}/.")
    ap.add_argument("--limit", type=int, default=None, help="Pass-through to api + scraper drivers.")
    ap.add_argument("--mpns", default=None, help="Pass-through (semicolon-separated 'MPN[:MFR]').")
    ap.add_argument("--mpns-file", default=None, help="Pass-through (tab-separated file).")
    ap.add_argument("--xlsx", type=Path, default=None, help="Pass-through chip-list xlsx.")
    ap.add_argument("--skip-api", action="store_true", help="Skip phase 1 (api). On resume: keep prior state.")
    ap.add_argument("--skip-scraper", action="store_true", help="Skip phase 2a (scraper main). bom2buy is also skipped.")
    ap.add_argument("--skip-bom2buy", action="store_true", help="Skip phase 2b (bom2buy).")
    ap.add_argument("--skip-merge", action="store_true", help="Stop after batches; no merged xlsx.")
    ap.add_argument("--resume", action="store_true", help="Continue from existing state file.")
    ap.add_argument("--api-args", default="", help="Extra args appended to batch_api_test.py.")
    ap.add_argument("--scraper-args", default="", help="Extra args appended to batch_scraper_test.py.")
    ap.add_argument("--merge-args", default="", help="Extra args appended to merge_batch_for_procurement.py.")
    args = ap.parse_args(argv)

    # State init / resume
    existing = load_state(args.env)
    if args.resume:
        if existing is None:
            print(f"[warn] --resume given but no state file at {state_path(args.env)}; starting fresh.")
            state = init_state(args)
        else:
            state = existing
            print(f"[resume] state from {state['started_at']} (env={state['env']})")
    else:
        if existing is not None:
            non_final = [p for p, ph in existing["phases"].items()
                         if ph.get("status") in ("running", "failed")]
            if non_final:
                print(f"[warn] existing state file has non-final phases {non_final}.")
                print(f"       overwriting. (Use --resume to continue instead.)")
        state = init_state(args)

    save_state(state, args.env)

    # Skip-scraper implies skip-bom2buy (bom2buy depends on scraper's batch_input.csv).
    if args.skip_scraper:
        args.skip_bom2buy = True

    skip_map = {
        "api": args.skip_api,
        "scraper_main": args.skip_scraper,
        "bom2buy": args.skip_bom2buy,
        "merge": args.skip_merge,
    }

    for phase in PHASE_ORDER:
        ph = state["phases"][phase]
        if ph.get("status") == "ok":
            print(f"[{phase}] already complete; skipping (resume).")
            continue
        if skip_map[phase]:
            ph["status"] = "skipped"
            ph["ended_at"] = now_iso()
            save_state(state, args.env)
            print(f"[{phase}] skipped (--skip-*).")
            continue
        # Run it.
        ph["status"] = "running"
        ph["started_at"] = now_iso()
        ph.pop("error", None)
        save_state(state, args.env)
        try:
            PHASE_RUNNERS[phase](state, args)
        except PhaseFailure as e:
            ph["status"] = "failed"
            ph["error"] = e.msg
            ph["ended_at"] = now_iso()
            save_state(state, args.env)
            print_actionable_error(state, args.env, phase, e)
            return 2
        ph["status"] = "ok"
        ph["ended_at"] = now_iso()
        save_state(state, args.env)

    print_final_summary(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
