"""Agentic-backend adapters for oxi-core.

This sub-package contains adapter implementations that wrap external
agentic CLI tools (codex, etc.) behind a common ``AgenticAdapter``
protocol.  The protocol is the stable contract; callers depend only on
``AgenticAdapter``, never on a concrete class.

Public surface
--------------
- ``AgenticAdapter``   — Protocol all adapters implement.
- ``CodexCliAdapter``  — Wraps the ``codex exec --json`` CLI.
- ``CodexFormatDriftError`` — Raised when the running binary's output
  format diverges from the known-good JSONL fixture.
"""

from .base import AgenticAdapter
from .codex import CodexCliAdapter, CodexFormatDriftError

__all__ = [
    "AgenticAdapter",
    "CodexCliAdapter",
    "CodexFormatDriftError",
]
