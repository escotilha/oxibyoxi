"""Agentic-backend adapters for oxi-core.

This sub-package contains adapter implementations that wrap external
agentic CLI tools (codex, etc.) behind a common ``AgenticAdapter``
protocol.  The protocol is the stable contract; callers depend only on
``AgenticAdapter``, never on a concrete class.

Public surface
--------------
- ``AgenticAdapter``       — Protocol all adapters implement.
- ``CodexCliAdapter``      — Wraps the ``codex exec --json`` CLI.
- ``CodexFormatDriftError``— Raised when the running binary's output
  format diverges from the known-good JSONL fixture.
- ``codex_events``         — Pure functions for parsing and normalising
  the ``codex --json`` JSONL event stream.
- ``shadow``               — Codex shadow-run harness (T2-35).  Activated
  by ``OXI_AGENTIC_SHADOW=codex``; zero-behavior-change observer.
"""

from . import codex_events, shadow
from .base import AgenticAdapter
from .codex import CodexCliAdapter, CodexFormatDriftError
from .shadow import (
    ShadowComparison,
    compare_results,
    maybe_schedule_shadow,
    persist_shadow_event,
    run_shadow,
    should_shadow,
)

__all__ = [
    "AgenticAdapter",
    "CodexCliAdapter",
    "CodexFormatDriftError",
    "ShadowComparison",
    "codex_events",
    "compare_results",
    "maybe_schedule_shadow",
    "persist_shadow_event",
    "run_shadow",
    "shadow",
    "should_shadow",
]
