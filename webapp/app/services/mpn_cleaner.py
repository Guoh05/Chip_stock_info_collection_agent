"""MPN cleaner — port of Phase 1 mechanical rules (decision #22).

Only **mechanical** rules are applied automatically. `MANUAL_OVERRIDES` from
Phase 1 are batch-specific literals (e.g. `32H743V1T6 → STM32H743VIT6` for
the 2026-05-23 batch) — applying them globally would mis-clean unrelated MPNs.

Rules (mirrors `test/_input_lists/_build_cleaned_input.py`):
  1. strip leading/trailing whitespace + NBSP (\\xa0)
  2. strip ' (<PACKAGE>)' parenthetical at end
  3. strip ', <PACKAGE>' suffix (REQUIRED space after comma; preserves NXP-style ',118')
  4. strip ' <PACKAGE>' at end (space-separated)
  5. strip '-<PACKAGE>' at end (dash-separated; only when <PACKAGE> matches a known package token)
  6. strip 'MCU-' prefix

After cleaning, if anything still looks unusual (very short, contains non-ASCII
that wasn't stripped, etc.) we mark it ⚠️ but DON'T auto-rewrite — let the user
decide on the review page.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

# Known package tokens. Same set as Phase 1 cleaner — extend in a new batch if new
# package codes show up. The dash-suffix rule (#5) only fires on these to avoid
# eating legit suffixes like -TR (T&R), -ANR (Microchip), -Z (MPS lead-free).
_PKG_TOKEN = r"(?:LQFP|SOP|TSSOP|SOT|DIP|QFN|QFP|TQFP|BGA|SSOP|MSOP|TO|T0|DFN)[\w\-]*"

_RE_PARENS = re.compile(r"\s*\([^()\s]+\)\s*$")
_RE_COMMA_PKG = re.compile(rf",\s+{_PKG_TOKEN}\s*$")
_RE_SPACE_PKG = re.compile(rf"\s+{_PKG_TOKEN}\s*$")
_RE_DASH_PKG = re.compile(rf"-{_PKG_TOKEN}\s*$")

# Suspicious patterns — flag but don't auto-fix
_RE_HAS_CHINESE = re.compile(r"[一-鿿]")
_RE_HAS_WHITESPACE_INSIDE = re.compile(r"\S\s+\S")


@dataclass
class CleanResult:
    raw: str
    cleaned: str
    rules_applied: list[str] = field(default_factory=list)
    warning: str | None = None

    @property
    def changed(self) -> bool:
        return self.raw != self.cleaned


def clean_mpn(raw: str) -> CleanResult:
    s = raw
    rules: list[str] = []
    warn: str | None = None

    # Rule 1: strip NBSP + whitespace
    if "\xa0" in s or s != s.strip():
        s = s.replace("\xa0", "").strip()
        rules.append("R1 strip whitespace/NBSP")

    # Rule 2: '(<PACKAGE>)' parenthetical
    new = _RE_PARENS.sub("", s)
    if new != s:
        rules.append("R2 strip (PACKAGE)")
        s = new

    # Rule 3: ', <PACKAGE>'
    new = _RE_COMMA_PKG.sub("", s)
    if new != s:
        rules.append("R3 strip , PACKAGE")
        s = new

    # Rule 4: ' <PACKAGE>'
    new = _RE_SPACE_PKG.sub("", s)
    if new != s:
        rules.append("R4 strip space-PACKAGE")
        s = new

    # Rule 5: '-<PACKAGE>'
    new = _RE_DASH_PKG.sub("", s)
    if new != s:
        rules.append("R5 strip -PACKAGE")
        s = new

    # Rule 6: 'MCU-' prefix
    if s.startswith("MCU-"):
        s = s[4:]
        rules.append("R6 strip MCU- prefix")

    # Post-clean suspicion detection
    if _RE_HAS_CHINESE.search(s):
        warn = "包含中文字符"
    elif _RE_HAS_WHITESPACE_INSIDE.search(s):
        warn = "MPN 内部含空格——可能需要人工合并"
    elif len(s) < 3:
        warn = "清洗后过短（<3 字符）"
    elif len(s) > 30:
        warn = "MPN 过长（>30 字符）——是否含多余信息？"

    return CleanResult(raw=raw, cleaned=s, rules_applied=rules, warning=warn)


def clean_batch(mpns: list[str]) -> tuple[list[CleanResult], bool]:
    """Apply mechanical rules to a list of MPNs.

    Returns (results, has_changes). When has_changes=True, the smart-review
    page should be shown to the user (decision #22).
    """
    results = [clean_mpn(m) for m in mpns]
    has_changes = any(r.changed or r.warning for r in results)
    return results, has_changes
