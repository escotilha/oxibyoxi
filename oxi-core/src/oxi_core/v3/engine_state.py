"""Shared engine state — the killswitch and runtime config.

Every long-running loop in the engine (dispatch, heartbeat, pr_watcher,
auto_merge) takes an `EngineState` and checks `is_stopping()` at the
top of every iteration. This structurally prevents the bug class where
a module-level flag or a file-based killswitch is checked by some
loops but not others, leading to dispatches after the operator has
hit stop.

`EngineState` is an in-memory object. Nothing here reads or writes to
disk. The CLI / supervisor is responsible for surfacing a file-based
or signal-based stop into `state.request_stop()`.

This module has no adapter dependency: it doesn't care what the
project is. It's pure runtime plumbing.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class EngineState:
    """Runtime state shared by every engine loop.

    Attributes:
        plan_tier: echo of adapter.plan_tier() — captured at startup so
            it's visible in logs and dashboards even if the adapter
            changes mid-run (which it shouldn't, but belts-and-suspenders).
        max_concurrent: maximum simultaneous dispatches. Adapter-derived.
        _stop_event: internal stop flag. Use request_stop() / is_stopping().

    The class is intentionally small. Anything loop-specific (per-tick
    state, per-task state) belongs elsewhere.
    """

    plan_tier: str
    max_concurrent: int
    _stop_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if not self.plan_tier:
            # Redundant with the adapter-level check, but cheap belt.
            raise ValueError("plan_tier must be non-empty (anti-pattern #3)")
        if self.max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be >= 1, got {self.max_concurrent}"
            )

    # ---- Stop signal -------------------------------------------------

    def request_stop(self, reason: str = "") -> None:
        """Ask every loop to stop after its current iteration.

        Idempotent: calling twice has no additional effect. The `reason`
        is informational — the state itself records only the boolean.
        Callers that want to log a reason should do so around this call.
        """
        self._stop_event.set()

    def clear_stop(self) -> None:
        """Reset the stop flag. Intended for tests and for supervisor restart."""
        self._stop_event.clear()

    def is_stopping(self) -> bool:
        """Return True if request_stop() has been called.

        Every long-running loop MUST call this at the top of each
        iteration. Checking only at entry is a known bug class — see
        semantic memory pattern-dispatch-stamp-progress-atomically for
        the parallel invariant on the DB side, and the prior-orchestrator
        post-mortems for the killswitch-mid-loop-race incident.
        """
        return self._stop_event.is_set()


__all__ = ["EngineState"]
