"""Claude Code PostToolUse hook — remind to keep run_pipeline.py + workflow
doc in sync when any pipeline component is edited.

Fires on Edit / Write / MultiEdit. If the touched file is one of the pipeline
components (batch drivers, merge script, bom2buy scripts), emit a reminder
that the orchestrator may need a corresponding update.

This hook does NOT try to semantically diff CLI surfaces — that's a deep
parse and the false-positive rate would be high. Instead it just nudges the
editor (human or agent) to make a judgment call. CLAUDE.md Hard Rule #7
covers the contract.

Never blocks: exits 0 always.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Files whose edits should trigger the orchestrator-sync reminder. Anything on
# this list participates in run_pipeline.py's subprocess calls (or is depended
# on by them).
WATCH_FILES = {
    PROJECT_ROOT / "common" / "merge_batch_for_procurement.py",
    PROJECT_ROOT / "api" / "scripts" / "batch_api_test.py",
    PROJECT_ROOT / "scraper" / "scripts" / "batch_scraper_test.py",
    PROJECT_ROOT / "scraper" / "scripts" / "scrape_bom2buy.py",
    PROJECT_ROOT / "scraper" / "scripts" / "_merge_bom2buy_into_batch.py",
}
ORCHESTRATOR = PROJECT_ROOT / "common" / "run_pipeline.py"
WORKFLOW_DOC = PROJECT_ROOT / "doc" / "run_pipeline_workflow.md"


def emit_context(msg: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    fp = (payload.get("tool_input") or {}).get("file_path") or ""
    if not fp:
        return 0
    try:
        edited = Path(fp).resolve()
    except Exception:
        return 0
    if edited not in {p.resolve() for p in WATCH_FILES}:
        return 0

    rel = edited.relative_to(PROJECT_ROOT) if PROJECT_ROOT in edited.parents else edited.name
    msg = (
        f"⚠ Pipeline-component edited: `{rel}`.\n"
        f"Per CLAUDE.md Hard Rule #7, check whether `common/run_pipeline.py` and "
        f"`doc/run_pipeline_workflow.md` need a corresponding update. Look for:\n"
        f"  - CLI surface changes (argparse args added/removed/renamed)\n"
        f"  - Output paths / folder naming\n"
        f"  - Preconditions (manual prep, new env vars)\n"
        f"  - Exit-code semantics (especially scrape_bom2buy's code 3 for captcha)\n"
        f"If anything material changed, also re-run the smoke test from "
        f"`doc/run_pipeline_workflow.md`."
    )
    emit_context(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
