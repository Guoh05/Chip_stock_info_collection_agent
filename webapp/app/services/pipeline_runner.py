"""Pipeline subprocess runner (M1).

Single global worker thread pulls run_ids from a Queue and processes them
serially. Matches decision #4 (single worker queue) + decision #15 (合一进程：
worker is a thread inside the FastAPI process).

Per-run flow (mirrors §3.2 T2-T4 in planning.md):
  1. mark started
  2. write /tmp/<run_id>_mpns.tsv
  3. subprocess.run(python ../common/run_pipeline.py ... --env <env>)
  4. read <env_root>/.pipeline_state.json — extract api/scraper batch_dir
  5. locate merged xlsx (newest Merge_*/ in <env_root>/merged/)
  6. parse merged xlsx (in_stock filter + sort)
  7. write parsed.json + slim Versuni_chip_stock_<run_id>.xlsx
  8. mark done / done_empty / failed
"""
from __future__ import annotations
import json
import logging
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil  # cross-platform process-tree kill for user cancel

from .. import storage
from ..config import (
    PIPELINE_PYTHON,
    PIPELINE_ENV,
    PIPELINE_CHIP_LIST,
    PIPELINE_API_ARGS,
    PIPELINE_SCRAPER_ARGS,
    PROJECT_ROOT,
    RUNS_DIR,
    TMP_DIR,
    WEBAPP_BASE_URL,
)
from .emailer import send_run_complete

log = logging.getLogger("webapp.runner")

_WORK_QUEUE: queue.Queue[str] = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()

# Live-process registry for user-initiated cancel. Worker thread populates;
# cancel_run() reads from the HTTP request thread. Dict ops under GIL are
# thread-safe enough for get/pop, but we still wrap mutations in a lock to
# avoid the small race window where worker has popped but the dict transition
# isn't visible yet.
_RUNNING: dict[str, subprocess.Popen] = {}
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_RUNNING_LOCK = threading.Lock()


def enqueue(run_id: str) -> None:
    _WORK_QUEUE.put(run_id)


def cancel_run(run_id: str) -> bool:
    """User clicked 中断查询. Flip DB → cancelled (idempotent — no-op if the
    run is already in a terminal state) and kill the subprocess tree if one
    is alive. Returns True when the cancel was actionable."""
    flipped = storage.mark_cancelled(run_id)
    if not flipped:
        return False
    with _RUNNING_LOCK:
        proc = _RUNNING.get(run_id)
        evt = _CANCEL_EVENTS.get(run_id)
    if evt is not None:
        evt.set()
    if proc is not None and proc.poll() is None:
        log.info("%s: user cancel — killing process tree (pid=%s)", run_id, proc.pid)
        _kill_process_tree(proc.pid)
    else:
        log.info("%s: user cancel — no live process (queued or already exited)", run_id)
    return True


def _kill_process_tree(pid: int) -> None:
    """Terminate then SIGKILL the process and every descendant. psutil handles
    Linux + Windows uniformly, and copes with ephemeral PIDs better than
    os.killpg / taskkill /T (no race on PID reuse, no orphaned chromium)."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def start_worker() -> None:
    """Idempotent: start the single background worker once."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        t = threading.Thread(target=_worker_loop, name="webapp-worker", daemon=True)
        t.start()
        log.info("worker thread started")


def _worker_loop() -> None:
    while True:
        run_id = _WORK_QUEUE.get()
        try:
            # User may have cancelled while this run was still queued — in
            # which case status is already 'cancelled'; skip the pipeline.
            run = storage.get_run(run_id)
            if run and run["status"] == "cancelled":
                log.info("%s: pre-cancelled in queue — skipping pipeline run", run_id)
                continue
            _process(run_id)
        except Exception as e:  # noqa: BLE001 — last-line safety net
            log.exception("worker exception on %s", run_id)
            try:
                storage.mark_failed(run_id, f"Worker exception: {e!r}")
            except Exception:
                log.exception("also failed to mark_failed")
        finally:
            _notify(run_id)
            _WORK_QUEUE.task_done()


def _notify(run_id: str) -> None:
    """Send completion email to the run's owner. Best-effort — never raises.
    Skips email when the user cancelled the run themselves (they don't need
    a reminder of an action they just took)."""
    try:
        run = storage.get_run(run_id)
        if not run:
            return
        if run["status"] == "cancelled":
            return
        view_url = f"{WEBAPP_BASE_URL.rstrip('/')}/r/{run_id}"
        phases = _read_phase_snapshot(run_id)
        send_run_complete(
            to=run["owner_email"],
            run_id=run_id,
            status=run["status"],
            row_count=run.get("row_count") or 0,
            view_url=view_url,
            error_text=run.get("error_text"),
            phases=phases,
        )
    except Exception:
        log.exception("notify failed for %s", run_id)


def _read_phase_snapshot(run_id: str) -> dict | None:
    """Read frozen .pipeline_state.json snapshot for a finished run."""
    snap = RUNS_DIR / run_id / "state_snapshot.json"
    if not snap.exists():
        return None
    try:
        state = json.loads(snap.read_text(encoding="utf-8"))
        phases = state.get("phases", {})
        return {
            "api": (phases.get("api") or {}).get("status", "pending"),
            "scraper_main": (phases.get("scraper_main") or {}).get("status", "pending"),
            "merge": (phases.get("merge") or {}).get("status", "pending"),
        }
    except Exception:
        return None


def _env_root() -> Path:
    return PROJECT_ROOT / ("test" if PIPELINE_ENV == "test" else "production")


def _process(run_id: str) -> None:
    log.info("processing %s", run_id)
    run = storage.get_run(run_id)
    if not run:
        log.error("run %s not in db", run_id)
        return

    storage.mark_started(run_id)
    storage.set_status(run_id, "running", phase="api")

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Step 2: write mpns tsv (MPN<TAB>Mfr, Mfr blank for M1)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tsv_path = TMP_DIR / f"{run_id}_mpns.tsv"
    mpns: list[str] = run["mpns"]
    tsv_path.write_text(
        "\n".join(f"{m}\t" for m in mpns) + "\n", encoding="utf-8",
    )

    # Step 3: subprocess invocation (defensive flag-passing per planning §3)
    cmd = [
        PIPELINE_PYTHON,
        str(PROJECT_ROOT / "common" / "run_pipeline.py"),
        "--env", PIPELINE_ENV,
        "--mpns-file", str(tsv_path),
        "--skip-bom2buy",
    ]
    # NOTE: use --key=value syntax (not "--key value" two-arg form) — orchestrator's
    # argparse rejects two-arg values when the value starts with '--' (e.g. when
    # PIPELINE_SCRAPER_ARGS='--sequential'). The '=' form is unambiguous.
    if PIPELINE_API_ARGS.strip():
        cmd += [f"--api-args={PIPELINE_API_ARGS}"]
    if PIPELINE_SCRAPER_ARGS.strip():
        cmd += [f"--scraper-args={PIPELINE_SCRAPER_ARGS}"]
    if PIPELINE_CHIP_LIST and Path(PIPELINE_CHIP_LIST).exists():
        # Windows + shlex.split(): backslashes get eaten. Use forward slashes
        # (Windows accepts them in file paths) so the path survives unscathed.
        chip_list_posix = Path(PIPELINE_CHIP_LIST).as_posix()
        cmd += [f"--merge-args=--chip-list {chip_list_posix}"]

    log_path = run_dir / "pipeline.log"
    log.info("%s: cmd = %s", run_id, " ".join(shlex.quote(c) for c in cmd))

    pipeline_start_ts = time.time()
    # 6h safety net — covers ~70 MPN × 5 min/MPN + buffer. Larger batches may
    # trip this and get an actionable error pointing the user to split up.
    # Also covers true hangs (playwright dead element wait, network deadlock).
    PIPELINE_TIMEOUT_SECONDS = 6 * 3600

    # PYTHONUNBUFFERED=1 — when Python's stdout is a file (not a tty) it block-
    # buffers (~8 KB), so scraper's sparse "[N/M] MPN" progress prints sit in
    # buffer for minutes until the child exits. That made the run page show
    # "no progress" for the entire scraper phase. Setting this env var
    # propagates to all grand/child Python processes (run_pipeline →
    # batch_*_test.py) and forces line buffering.
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"

    # Popen + poll loop instead of subprocess.run(timeout=...) so cancel_run()
    # can interrupt mid-pipeline. We register the Popen + a cancel Event in
    # the module-level dicts; cancel_run sets the event + kills the tree.
    cancel_event = threading.Event()
    with _RUNNING_LOCK:
        _CANCEL_EVENTS[run_id] = cancel_event

    cancelled = False
    timed_out = False
    rc: int | None = None
    try:
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"# Command: {' '.join(shlex.quote(c) for c in cmd)}\n")
            logf.write(f"# Started: {datetime.now().isoformat()}\n")
            logf.write(f"# cwd: {PROJECT_ROOT}\n\n")
            logf.flush()
            # start_new_session (POSIX) / CREATE_NEW_PROCESS_GROUP (Windows):
            # gives the children their own session/group so a stray signal to
            # webapp doesn't take them down — we manage their lifetime via the
            # registered Popen handle + _kill_process_tree on cancel/timeout.
            popen_kwargs = {
                "cwd": str(PROJECT_ROOT),
                "stdout": logf,
                "stderr": subprocess.STDOUT,
                "env": child_env,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd, **popen_kwargs)
            with _RUNNING_LOCK:
                _RUNNING[run_id] = proc

            deadline = time.time() + PIPELINE_TIMEOUT_SECONDS
            try:
                while True:
                    try:
                        rc = proc.wait(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        if cancel_event.is_set():
                            cancelled = True
                            _kill_process_tree(proc.pid)
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                pass
                            logf.write("\n# CANCELLED by user — pipeline killed\n")
                            break
                        if time.time() > deadline:
                            timed_out = True
                            _kill_process_tree(proc.pid)
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                pass
                            logf.write(f"\n# TIMEOUT after {PIPELINE_TIMEOUT_SECONDS // 3600}h — pipeline killed\n")
                            break
            finally:
                with _RUNNING_LOCK:
                    _RUNNING.pop(run_id, None)
    finally:
        with _RUNNING_LOCK:
            _CANCEL_EVENTS.pop(run_id, None)

    if cancelled:
        log.info("%s: pipeline cancelled by user", run_id)
        # status is already 'cancelled' (set by cancel_run before kill).
        # Snapshot whatever phase state existed at cancel time so the result
        # page can show where the user interrupted.
        try:
            state_path = _env_root() / ".pipeline_state.json"
            if state_path.exists():
                shutil.copy2(state_path, run_dir / "state_snapshot.json")
        except Exception:
            log.exception("snapshot during cancel failed")
        return

    if timed_out:
        log.error("%s: pipeline timed out after %dh", run_id, PIPELINE_TIMEOUT_SECONDS // 3600)
        storage.mark_failed(
            run_id,
            f"Pipeline 超过 {PIPELINE_TIMEOUT_SECONDS // 3600} 小时未完成（兜底超时）。"
            f"如果 MPN 数量很大，请联系管理员评估是否需要拆批跑。"
            f"完整日志：webapp/runs/{run_id}/pipeline.log",
        )
        return

    log.info("%s: pipeline exited rc=%s", run_id, rc)

    # Step 4: read state file
    env_root = _env_root()
    state_path = env_root / ".pipeline_state.json"
    if not state_path.exists():
        storage.mark_failed(run_id, f"No state file at {state_path} after pipeline exit (rc={rc})")
        return

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        storage.mark_failed(run_id, f"State file unparseable: {e}")
        return

    # Snapshot state file (decision #16 step 7)
    shutil.copy2(state_path, run_dir / "state_snapshot.json")

    phases = state.get("phases", {})
    api_batch = (phases.get("api") or {}).get("batch_dir")
    scraper_batch = (phases.get("scraper_main") or {}).get("batch_dir")
    merge_status = (phases.get("merge") or {}).get("status")

    if rc != 0 or merge_status != "ok":
        last_lines = _tail_log(log_path, 50)
        storage.mark_failed(
            run_id,
            f"Pipeline exit rc={rc}; merge phase status={merge_status}. "
            f"Last log lines:\n{last_lines}",
        )
        return

    # Step 5: locate merged xlsx (decision #16: merge phase doesn't save batch_dir,
    # find newest Merge_*/ in <env_root>/merged/ — safe since single-worker queue
    # guarantees no parallel pipeline runs).
    merge_root = env_root / "merged"
    candidates = [p for p in merge_root.iterdir() if p.is_dir() and p.name.startswith("Merge_")]
    candidates = [p for p in candidates if p.stat().st_mtime >= pipeline_start_ts - 5]
    if not candidates:
        storage.mark_failed(run_id, f"No new Merge_*/ folder found in {merge_root}")
        return
    merge_dir = max(candidates, key=lambda p: p.stat().st_mtime)
    merge_batch_rel = str(merge_dir.relative_to(PROJECT_ROOT))

    xlsx_files = list(merge_dir.glob("Versuni_chip_stock_availability_check_*.xlsx"))
    if not xlsx_files:
        storage.mark_failed(run_id, f"No Versuni*.xlsx in {merge_dir}")
        return
    pipeline_xlsx = xlsx_files[0]
    log.info("%s: parsing %s", run_id, pipeline_xlsx)

    # Step 6: parse xlsx
    from .xlsx_parser import parse_merged_xlsx
    from .xlsx_writer import write_slim_xlsx

    try:
        filtered_rows, all_rows = parse_merged_xlsx(pipeline_xlsx)
    except Exception as e:  # noqa: BLE001
        log.exception("parse failed")
        storage.mark_failed(run_id, f"xlsx parse failed: {e!r}")
        return

    # Type/risk are business judgments that must come from the user, never from
    # the master chip-list join. Wipe them first; overlay below puts user values
    # back when present (Mode B upload). Mode A (paste) leaves them empty.
    for record in filtered_rows + all_rows:
        record["Type"] = ""
        record["risk"] = ""

    # Decision #29: overlay Type/risk/Manufacture from user-uploaded input.csv
    input_csv = run_dir / "input.csv"
    if input_csv.exists():
        _apply_metadata_overlay(filtered_rows, all_rows, input_csv)

    # Step 7: write parsed.json + slim xlsx
    parsed_path = run_dir / "parsed.json"
    parsed_path.write_text(
        json.dumps(filtered_rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    slim_path = run_dir / f"Versuni_chip_stock_{run_id}.xlsx"
    try:
        write_slim_xlsx(all_rows, slim_path)
    except Exception as e:  # noqa: BLE001
        log.exception("slim xlsx write failed")
        storage.mark_failed(run_id, f"slim xlsx write failed: {e!r}")
        return

    # Step 8: mark done
    status = "done" if filtered_rows else "done_empty"
    storage.mark_done(
        run_id, status,
        api_batch=api_batch,
        scraper_batch=scraper_batch,
        merge_batch=merge_batch_rel,
        row_count=len(filtered_rows),
    )
    log.info("%s: %s (%d in-stock rows / %d total)", run_id, status, len(filtered_rows), len(all_rows))


def _apply_metadata_overlay(filtered: list[dict], all_rows: list[dict], csv_path: Path) -> None:
    """Decision #29: user-uploaded Type/risk/Manufacture overrides chip-list join.

    Match by MPN_cleaned_byAgent. Only override when user provided non-empty value.
    Mutates both lists in place.
    """
    import csv
    overlay: dict[str, dict] = {}
    try:
        with csv_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mpn = (row.get("Manufacture Part Number") or "").strip()
                if not mpn:
                    continue
                overlay[mpn] = {
                    "Manufacture": (row.get("Manufacture") or "").strip(),
                    "Type": (row.get("Type") or "").strip(),
                    "risk": (row.get("risk") or "").strip(),
                }
    except Exception:
        log.exception("metadata overlay parse failed for %s", csv_path)
        return

    applied = 0
    for record in filtered + all_rows:
        mpn = record.get("MPN_cleaned_byAgent")
        if mpn and mpn in overlay:
            for col, val in overlay[mpn].items():
                if val:  # only override non-empty user values
                    record[col] = val
                    applied += 1
    log.info("metadata overlay applied: %d field updates from %s", applied, csv_path.name)


def _tail_log(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return "(could not read log)"
