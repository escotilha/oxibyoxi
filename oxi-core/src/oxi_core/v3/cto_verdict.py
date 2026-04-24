"""Parse a /cto-style markdown report into a structured verdict.

The /cto skill produces a review as markdown: sections with severity
labels, a recommendation block, and usually a top-line proceed/abort.
The engine needs a machine-readable gate: "does this plan pass muster
to dispatch, or should we abort before spending tokens?"

``cto_verdict.parse`` collapses the markdown into a ``CtoVerdict``:

- ``verdict``: ``PROCEED`` or ``ABORT``.
- ``severity_max``: the worst severity found in the concerns list
  (``none`` / ``info`` / ``low`` / ``medium`` / ``high`` / ``critical``).
- ``concerns``: the enumerated bullet items, with severity tag per item.

Fail-closed: any parse ambiguity or missing verdict line returns
``ABORT``. The critic downstream can override if you trust the skill
to always produce a clean report, but oxi's default never assumes.

No Claude is invoked by this module. It's a pure string parser.
Callers hand it the rendered output of ``/cto`` (or any skill that
follows the same convention).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Verdict(Enum):
    PROCEED = "proceed"
    ABORT = "abort"


class Severity(Enum):
    """Ordered severity. NONE is the absence of any concern."""

    NONE = 0
    INFO = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    CRITICAL = 5

    @classmethod
    def from_label(cls, label: str) -> Severity:
        """Parse a severity label. Unknown → NONE (conservative).

        The report format uses labels like ``severity: high`` or
        ``[HIGH]``; we strip surrounding punctuation and compare
        case-insensitively.
        """
        cleaned = label.strip().strip("[](){}:").lower()
        aliases = {
            "": cls.NONE,
            "none": cls.NONE,
            "n/a": cls.NONE,
            "nil": cls.NONE,
            "info": cls.INFO,
            "informational": cls.INFO,
            "low": cls.LOW,
            "medium": cls.MEDIUM,
            "med": cls.MEDIUM,
            "high": cls.HIGH,
            "hi": cls.HIGH,
            "critical": cls.CRITICAL,
            "crit": cls.CRITICAL,
            "blocker": cls.CRITICAL,
        }
        return aliases.get(cleaned, cls.NONE)


@dataclass(frozen=True)
class Concern:
    """One item in the concerns list."""

    severity: Severity
    text: str


@dataclass(frozen=True)
class CtoVerdict:
    """Structured verdict returned by ``parse()``."""

    verdict: Verdict
    severity_max: Severity
    concerns: tuple[Concern, ...] = field(default_factory=tuple)
    reason: str = ""


# Regex patterns for the three things we look for in a report.
# Each is forgiving of punctuation, bold formatting, and case.

# Top-line verdict: matches ``**Verdict: proceed**``, ``## VERDICT: abort``,
# ``Final verdict — PROCEED``, etc.
_VERDICT_RE = re.compile(
    r"(?i)\b(?:final\s+)?verdict\b[\s\-—:*]+\*{0,2}(proceed|abort|go|no-?go|ship|hold)",
)

# Per-bullet severity tag: ``- [HIGH] ...`` or ``* (critical) ...`` or
# ``1. severity: medium — ...``.
_SEVERITY_BULLET_RE = re.compile(
    r"""(?ix)
    ^\s*(?:[-*]|\d+\.)\s+           # bullet marker
    (?:
       (?:\*\*|__)?                  # optional bold
       (?:\[|\()                     # opening bracket
       (?P<br_sev>[a-z/]+)           # severity in brackets
       (?:\]|\))                     # closing bracket
       (?:\*\*|__)?                  # optional bold
       \s*[:\-—]?\s*
       |
       (?:\*\*|__)?                  # bold-wrapped severity:
       severity\s*[:\-—]\s*
       (?P<kv_sev>[a-z]+)
       (?:\*\*|__)?
       \s*[:\-—]?\s*
    )
    (?P<text>.+)
    $
    """,
)


def _normalize_verdict_token(token: str) -> Verdict:
    t = token.lower()
    if t in ("proceed", "go", "ship"):
        return Verdict.PROCEED
    return Verdict.ABORT  # "abort", "no-go", "hold", unknown → ABORT


def _extract_concerns(text: str) -> list[Concern]:
    """Scan every line for severity-tagged bullet items."""
    concerns: list[Concern] = []
    for raw_line in text.splitlines():
        m = _SEVERITY_BULLET_RE.match(raw_line)
        if not m:
            continue
        label = m.group("br_sev") or m.group("kv_sev") or ""
        severity = Severity.from_label(label)
        concerns.append(Concern(severity=severity, text=m.group("text").strip()))
    return concerns


def parse(report: str) -> CtoVerdict:
    """Turn a /cto report into a structured verdict.

    Fail-closed: a report with no recognizable verdict line → ABORT
    with ``reason="no verdict line"``. A report whose top-line says
    PROCEED but contains a ``critical`` bullet is NOT automatically
    downgraded — the parser reports the verdict the skill gave. Callers
    (e.g., auto_merge gates) can layer additional rules.
    """
    if not report or not report.strip():
        return CtoVerdict(
            verdict=Verdict.ABORT,
            severity_max=Severity.NONE,
            concerns=(),
            reason="empty report",
        )

    verdict_match = _VERDICT_RE.search(report)
    if verdict_match is None:
        return CtoVerdict(
            verdict=Verdict.ABORT,
            severity_max=Severity.NONE,
            concerns=tuple(_extract_concerns(report)),
            reason="no verdict line",
        )

    verdict = _normalize_verdict_token(verdict_match.group(1))
    concerns = _extract_concerns(report)
    severity_max = (
        max((c.severity for c in concerns), key=lambda s: s.value)
        if concerns
        else Severity.NONE
    )
    return CtoVerdict(
        verdict=verdict,
        severity_max=severity_max,
        concerns=tuple(concerns),
        reason="parsed",
    )


__all__ = [
    "Concern",
    "CtoVerdict",
    "Severity",
    "Verdict",
    "parse",
]
