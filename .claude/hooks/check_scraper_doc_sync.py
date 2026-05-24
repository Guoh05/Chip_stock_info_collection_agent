"""Claude Code PostToolUse hook — flag drift between an edited per-source
scraper (`scraper/scripts/scrape_*.py`) and its section in
`scraper/doc/source_technical_reference.md`.

Fires on Edit / Write / MultiEdit. If the edited file is one of the
per-source scrapers, the hook:

1. Reads the script and identifies its CHANNEL.
2. Locates the corresponding `## N. <Channel>` section in
   source_technical_reference.md.
3. Extracts a set of HIGH-SIGNAL top-level constants from the script
   (throttle / rate-limit / interval / delay knobs, URL bases, key
   timeout / per-row-cap constants, sentinel-file paths).
4. Confirms each such constant NAME appears verbatim somewhere in the
   doc (preferably in the channel's section, but falling back to the
   whole file to keep noise low).
5. Emits a non-blocking `hookSpecificOutput.additionalContext` reminder
   when something is missing.

Goals:
- Catch the case where a behaviour-changing knob (e.g. `_THROTTLE_SEC`,
  `BASE_URL`, `PER_MPN_DELAY_SEC`) was added to a script without a
  corresponding doc paragraph in `source_technical_reference.md`.
- Do NOT try to semantically diff the prose — token presence is the
  shallow heuristic; the agent must still write the prose update by
  hand.

Never blocks: exits 0 always, even on parse errors / missing files."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOC = PROJECT_ROOT / "scraper" / "doc" / "source_technical_reference.md"
SCRIPT_DIR = PROJECT_ROOT / "scraper" / "scripts"

# Map CHANNEL constant value -> the heading-label fragment used in
# source_technical_reference.md (`## N. <fragment> ...`). The fragment is the
# Latin / English part that appears AFTER the section number.
CHANNEL_TO_SECTION_LABEL = {
    "LCSC": "LCSC",
    "DIGIKEY": "Digikey",
    "HQEW": "HQEW",
    "FUTURE": "Future",
    "RSONLINE": "RSONLINE",
    "ONEYAC": "ONEYAC",
    "ICKEY": "ICKEY",
    "ROCHESTER": "Rochester",
    "BOM2BUY": "bom2buy",
    "MOUSER": "Mouser",
    "ARROW": "Arrow",
}

# Constants worth checking — typically these encode "user-visible behaviour"
# that should be reflected in the per-source doc section. We deliberately
# DO NOT include code-structure constants like `TEST_ROOT`, `CHANNEL`, `UA`.
HIGH_SIGNAL_NAME_PATTERNS = [
    re.compile(r"^_THROTTLE_\w+$"),       # cross-process throttle knobs
    re.compile(r"^_RATE_\w+$"),
    re.compile(r"^.*_DELAY_\w+$"),        # PER_MPN_DELAY_SEC, LONG_DELAY_SEC ...
    re.compile(r"^.*_INTERVAL_\w+$"),
    re.compile(r"^.*_TIMEOUT$"),
    re.compile(r"^.*_TIMEOUT_\w+$"),
    re.compile(r"^.*_RETRY_\w+$"),
    re.compile(r"^.*_CAP$"),              # top-N listing caps (HQEW etc.)
    re.compile(r"^.*_LIMIT$"),
    re.compile(r"^SEARCH_URL$"),          # full URL template — user-visible
    re.compile(r"^DETAIL_URL$"),
    re.compile(r"^API_URL$"),
    # `BASE` / `BASE_URL` deliberately excluded — too generic, false positives
]

TOP_CONST_RE = re.compile(r"^([_A-Z][_A-Z0-9]+)\s*=\s*", re.MULTILINE)
CHANNEL_RE = re.compile(r'^CHANNEL\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def is_high_signal(name: str) -> bool:
    return any(p.match(name) for p in HIGH_SIGNAL_NAME_PATTERNS)


def find_channel(src: str) -> str | None:
    m = CHANNEL_RE.search(src)
    return m.group(1) if m else None


def extract_section(doc: str, label: str | None) -> str | None:
    if not label:
        return None
    # Section spans from `## N. <label>` (case-sensitive on the label) to the
    # next `## <something>` heading or `---` separator at column 0.
    pat = re.compile(
        rf"^##\s+\d+\.\s+{re.escape(label)}\b.*?(?=^##\s+\d+\.|^---)",
        re.MULTILINE | re.DOTALL,
    )
    m = pat.search(doc + "\n## 99. ZZZ END\n")
    return m.group(0) if m else None


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
        p = Path(fp).resolve()
    except Exception:
        return 0

    # Only fire for `scraper/scripts/scrape_*.py`. Skip helper scripts
    # like _merge_*, _update_*, _adhoc_*.
    try:
        rel = p.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return 0
    if not rel.startswith("scraper/scripts/scrape_"):
        return 0
    if not p.exists() or not DOC.exists():
        return 0

    src = p.read_text(encoding="utf-8", errors="replace")
    doc = DOC.read_text(encoding="utf-8")

    channel = find_channel(src)
    label = CHANNEL_TO_SECTION_LABEL.get(channel or "")
    section = extract_section(doc, label)

    # Extract HIGH-SIGNAL top-level constants from the script
    interesting: list[str] = []
    for m in TOP_CONST_RE.finditer(src):
        name = m.group(1)
        if is_high_signal(name):
            interesting.append(name)

    if not interesting:
        return 0

    # Check each: prefer the channel's section; fall back to whole-doc match
    # to keep false positives low.
    missing: list[str] = []
    for name in interesting:
        in_section = section is not None and name in section
        in_doc = name in doc
        if not (in_section or in_doc):
            missing.append(name)

    if not missing:
        return 0

    head = f"{p.name} was edited"
    if channel:
        head += f" (CHANNEL={channel})"
    if not label:
        head += " — could not locate a matching section in source_technical_reference.md"
    elif not section:
        head += f" — `## N. {label} ...` section not found in source_technical_reference.md"

    msg = (
        f"⚠ {head}; high-signal constants not mentioned in "
        f"scraper/doc/source_technical_reference.md:\n  - "
        + "\n  - ".join(sorted(set(missing)))
        + "\n\nUpdate the corresponding section of source_technical_reference.md "
        "(engine choice / URL patterns / DOM selectors / throttle or timeout knob / pitfalls list) "
        "before completing this task. Token presence is a shallow check — the prose update is yours to write."
    )
    emit_context(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
