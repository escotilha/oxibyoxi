"""``AgenticAdapter`` — the protocol every agentic-backend adapter implements.

Design intent
-------------
The protocol is deliberately narrow: an agentic adapter knows *how* to
spawn its backing CLI and what version it is running.  It does *not*
know about tasks, budgets, or the oxi state machine.  Those concerns
live in the callers (``dispatch.py``, future routing layer).

The three-method surface maps to three questions the engine needs to
answer before using any backend:

1. ``pinned_version()``  — what semver string did the operator pin?
2. ``binary_path()``     — where is the binary on disk?
3. ``spawn(...)``        — given a prompt + cwd, run the backend and
                           return an async iterable of raw event dicts.

``spawn`` is *not* yet defined here; it will be added in the next PR
(DispatchResult wiring).  This skeleton PR establishes the version-pin
smoke test and the spawn-shell only.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AgenticAdapter(Protocol):
    """Minimal contract for agentic-backend CLI adapters.

    Implementations must be instantiatable with no arguments so that
    the engine can discover and create them via entry-points or env-var
    in the same way it discovers ``oxi_core.adapter.Adapter``.
    """

    def pinned_version(self) -> str:
        """Return the operator-pinned semver string for the backend binary.

        The returned string is used in format-drift error messages and
        for version-gate logging.  Must be non-empty.  Example::

            "0.1.0"
        """

    def binary_path(self) -> str:
        """Return the filesystem path (or bare name) of the backend binary.

        May be an absolute path (``/usr/local/bin/codex``) or a bare
        name that will be resolved via ``PATH`` at spawn time.
        Example::

            "codex"
        """


__all__ = ["AgenticAdapter"]
