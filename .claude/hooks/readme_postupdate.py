"""Claude Code PostToolUse hook — refresh per-track README status blocks.

Fires on `Bash`, `Edit`, `Write`, `MultiEdit`. Reads the Claude Code hook
payload from stdin (a JSON dict with `tool_name`, `tool_input`, optionally
`tool_response`) and decides which track was touched:

  - "api"     when the tool path or bash command mentions `api/scripts/`
  - "scraper" when the tool path or bash command mentions `scraper/scripts/`

For each touched track that has a regen script at
`<track>/scripts/_update_readme_status.py`, runs it as a best-effort
subprocess. The hook never blocks the agent: timeouts and errors are
swallowed; exit code is always 0 unless stdin is unreadable.

Design choices:
  - Single shared dispatcher across both tracks. The scraper window adds its
    own `scraper/scripts/_update_readme_status.py` without touching
    settings.json or this file.
  - Filters internally on file_path / command (settings.json `matcher`
    matches the tool name only, which would over-fire).
  - Skips when the modified file is the README itself, to avoid feedback
    loops (Edit on README.md → regen rewrites README.md → another Edit fires
    nothing extra, but be defensive).
"""

from __future__ import annotations

import json
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


def _touched_tracks(payload: dict) -> list[str]:
    """Return list of track names touched by the tool invocation."""
    tool = payload.get("tool_name") or ""
    inp = payload.get("tool_input") or {}
    candidates: list[str] = []

    if tool == "Bash":
        candidates.append(_norm(inp.get("command")))
    elif tool in ("Edit", "Write", "MultiEdit"):
        path = _norm(inp.get("file_path"))
        # Skip when the touched file is a README — avoid loops.
        if path.endswith("/readme.md"):
            return []
        candidates.append(path)
    else:
        return []

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
