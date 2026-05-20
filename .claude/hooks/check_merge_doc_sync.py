"""Claude Code PostToolUse hook — flag drift between the procurement-merge
script and its rules doc.

Fires on Edit / Write / MultiEdit. If the touched file is
`common/merge_batch_for_procurement.py`, extract the field-name lists and
key constants from the source via regex and confirm each one is mentioned
in `doc/merge_for_procurement_rules.md`. If anything is missing, emit a
hookSpecificOutput.additionalContext message Claude will read on the
next turn.

Never blocks: exits 0 always, even on parse errors.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCH_FILE = PROJECT_ROOT / "common" / "merge_batch_for_procurement.py"
RULES_DOC = PROJECT_ROOT / "doc" / "merge_for_procurement_rules.md"

# Token-extraction patterns. Each entry: (label, regex returning a list of
# string tokens). The hook checks every token appears verbatim in the doc.
PATTERNS = [
    ("output column", re.compile(r"OUTPUT_COLUMNS\s*=\s*\[(.*?)\]", re.S)),
    ("mismatch column", re.compile(r"MISMATCH_COLUMNS\s*=\s*\[(.*?)\]", re.S)),
    ("dropped source prefix", re.compile(r"DROP_SOURCE_PREFIXES\s*=\s*\((.*?)\)", re.S)),
]

STR_RE = re.compile(r"[\"']([^\"']+)[\"']")


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
    try:
        if Path(fp).resolve() != WATCH_FILE.resolve():
            return 0
    except Exception:
        return 0

    if not WATCH_FILE.exists() or not RULES_DOC.exists():
        return 0

    src = WATCH_FILE.read_text(encoding="utf-8")
    doc = RULES_DOC.read_text(encoding="utf-8")

    missing: list[str] = []
    for label, pat in PATTERNS:
        m = pat.search(src)
        if not m:
            continue
        for tok in STR_RE.findall(m.group(1)):
            if tok and tok not in doc:
                missing.append(f"{label} `{tok}`")

    if not missing:
        return 0

    msg = (
        "⚠ merge_batch_for_procurement.py was edited but "
        "doc/merge_for_procurement_rules.md appears out of sync.\n"
        "Tokens present in the code but missing from the doc:\n  - "
        + "\n  - ".join(sorted(set(missing)))
        + "\nUpdate doc/merge_for_procurement_rules.md before completing this task "
        "(field-name table, sheet column list, or filter rules section as appropriate)."
    )
    emit_context(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
