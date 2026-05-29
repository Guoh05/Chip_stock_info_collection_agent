"""Claude Code PostToolUse hook — refresh per-track README status blocks.

Fires on `Bash`, `Edit`, `Write`, `MultiEdit`. Reads the Claude Code hook
payload from stdin (a JSON dict with `tool_name`, `tool_input`, optionally
`tool_response`) and decides which track was touched:

  - "api"     when the bash command mentions `api/scripts/`
  - "scraper" when the bash command mentions `scraper/scripts/`

For each touched track that has a regen script at
`<track>/scripts/_update_readme_status.py`, runs it as a best-effort
subprocess. The hook never blocks the agent: timeouts and errors are
swallowed; exit code is always 0 unless stdin is unreadable.

**`--env prod` only (matches CLAUDE.md Hard Rule #5).** The README status
block must reflect production batches, not test/dev/smoke runs. So the hook
only refreshes when:
  - the tool is `Bash` AND the command carries `--env prod` (or `--env=prod`).
Edit/Write/MultiEdit no longer trigger a refresh — editing a script is not a
batch run, produces no new stats, and was a clobber source. The batch driver
applies the same `--env prod` gate on its own self-call, so the two paths
stay consistent (a direct `--env prod` bash run double-fires harmlessly —
the regen is idempotent).

Design choices:
  - Single shared dispatcher across both tracks. Each track owns its own
    `<track>/scripts/_update_readme_status.py` without touching this file.
  - Filters internally on the command (settings.json `matcher` matches the
    tool name only, which would over-fire).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# .claude/hooks/readme_postupdate.py  →  parents[2] = project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

TRACK_REGEN = {
    "api": PROJECT_ROOT / "api" / "scripts" / "_update_readme_status.py",
    "scraper": PROJECT_ROOT / "scraper" / "scripts" / "_update_readme_status.py",
}

TIMEOUT_SECONDS = 15


def _norm(s) -> str:
    return (s or "").replace("\\", "/").lower()


# `--env prod` or `--env=prod` (command already lowercased by _norm).
_PROD_ENV_RE = re.compile(r"--env[=\s]+prod\b")


def _is_prod_run(command: str) -> bool:
    return bool(_PROD_ENV_RE.search(command))


def _touched_tracks(payload: dict) -> list[str]:
    """Return list of track names touched by the tool invocation.

    Only `--env prod` Bash batch runs qualify (CLAUDE.md Hard Rule #5).
    Edit/Write/MultiEdit never trigger a refresh — they produce no new batch
    stats and historically clobbered the status block.
    """
    tool = payload.get("tool_name") or ""
    inp = payload.get("tool_input") or {}

    if tool != "Bash":
        return []
    command = _norm(inp.get("command"))
    if not _is_prod_run(command):
        return []
    candidates = [command]

    touched: list[str] = []
    for track in TRACK_REGEN:
        marker = f"{track}/scripts/"
        # Match the track only when `marker` appears at the start of a path
        # segment — i.e. preceded by a path separator, whitespace, or string
        # start. Without this guard, "myapi/scripts/" would also match.
        if any(
            marker in c
            and (c.find(marker) == 0 or c[c.find(marker) - 1] in "/\\ \t\"'")
            for c in candidates
        ):
            touched.append(track)
    return touched


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return 0
    if not PYTHON.exists():
        return 0
    for track in _touched_tracks(payload):
        script = TRACK_REGEN.get(track)
        if not script or not script.exists():
            continue
        try:
            subprocess.run(
                [str(PYTHON), str(script)],
                timeout=TIMEOUT_SECONDS,
                check=False,
                capture_output=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            # Best-effort. Never block the agent.
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
