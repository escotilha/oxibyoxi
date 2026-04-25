"""Typed constants for every event kind written to the ``event`` table.

All modules that insert a row into ``event`` should reference these
constants instead of bare string literals.  This eliminates the
string-drift class of bugs — a mistyped event kind would never match
any constant, and a grep for a constant shows every reader *and* writer
at once.

Usage
-----
.. code-block:: python

    from oxi_core.v3.ledger_events import LedgerEvent

    # emit
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
        (task_id, LedgerEvent.DISPATCH_STARTED, json.dumps(payload)),
    )

    # query
    conn.execute(
        "SELECT * FROM event WHERE kind = ?",
        (LedgerEvent.BUDGET_HARD_STOP,),
    )

Design notes
------------
- ``LedgerEvent`` is a plain ``str`` subclass (via ``StrEnum``).  This
  means ``LedgerEvent.DISPATCH_STARTED == "dispatch_started"`` and the
  constant can be used anywhere a ``str`` is expected without casting.
  It is also comparable with ``==`` in ``assert`` statements, which
  makes test assertions readable.
- Constants cover *every* event kind found in the codebase as of
  T1-14.  Modules with dynamic kind construction (``deadman_<level>``,
  ``oauth_watch_<status>``) expose helper functions that compose the
  kind from a ``LedgerEvent`` prefix constant and a suffix so the
  prefix is still typed.
- The ``__all__`` list is exhaustive so ``from ledger_events import *``
  gives a predictable surface.
"""

from __future__ import annotations

from enum import StrEnum, auto

# ---------------------------------------------------------------------------
# Core constant set
# ---------------------------------------------------------------------------


class LedgerEvent(StrEnum):
    """String constants for every event kind written to the ledger.

    Members are grouped by subsystem.  The string value is the exact
    ``kind`` field stored in the ``event`` table.
    """

    # --- dispatch -----------------------------------------------------------
    # Emitted by oxi_core.v3.dispatch when a planned task is accepted.
    DISPATCH_STARTED = auto()
    # Worker session finished successfully; pr_watcher will reconcile.
    DISPATCH_SUCCEEDED = auto()
    # Transient failure (rate-limit, infra hiccup); task reset to planned.
    DISPATCH_RETRYABLE = auto()
    # Wall-clock timeout exceeded; task → abandoned.
    DISPATCH_TIMEOUT = auto()
    # Terminal failure; task → failed.
    DISPATCH_FAILED = auto()

    # --- pr_watcher ---------------------------------------------------------
    # First time the PR's number is stamped onto a task row.
    PR_OBSERVED = auto()
    # PR was merged (transition to terminal success).
    PR_MERGED = auto()
    # PR closed without merge (transition to failed).
    PR_FAILED = auto()
    # PR still open; freshness stamped to prevent heartbeat reaping.
    PR_OPEN_OBSERVED = auto()

    # --- handoff ------------------------------------------------------------
    # Resume snapshot written to the worktree.
    HANDOFF_WRITTEN = auto()

    # --- budget -------------------------------------------------------------
    # Daily spend crossed the soft-warn threshold (one per UTC day).
    BUDGET_SOFT_WARN = auto()
    # Daily spend hit or exceeded the hard cap (one per UTC day; sticky).
    BUDGET_HARD_STOP = auto()

    # --- auto_merge ---------------------------------------------------------
    # Critic approved; PR merged by the engine.
    MERGED_BY_AUTO_MERGE = auto()
    # Critic rejected the PR; task → failed with reason=critic_rejected.
    AUTO_MERGE_REJECTED = auto()
    # PR was already merged (by a human / another tool) when auto_merge ran.
    MERGE_ALREADY_DONE = auto()
    # gh merge call returned a non-zero exit code.
    AUTO_MERGE_GH_FAILURE = auto()

    # --- auto_recover -------------------------------------------------------
    # Task reset to planned for a retry attempt.
    AUTO_RECOVER_ATTEMPTED = auto()
    # All retry attempts exhausted; task stays in failed/abandoned.
    AUTO_RECOVER_EXHAUSTED = auto()

    # --- heartbeat ----------------------------------------------------------
    # Stale dispatched task reaped by the heartbeat reaper.
    ABANDONED_BY_HEARTBEAT = auto()

    # --- deep_fix -----------------------------------------------------------
    # Task escalated to a stronger model / modified prompt.
    DEEP_FIX_ESCALATED = auto()
    # All escalation recipes exhausted; human intervention required.
    DEEP_FIX_EXHAUSTED = auto()

    # --- ship_recovery ------------------------------------------------------
    # Uncommitted work staged, committed, pushed, and PR queued.
    SHIP_RECOVERED = auto()
    # One step of the recovery pipeline failed (git add/commit/push).
    SHIP_RECOVERY_FAILED = auto()

    # --- ingest_roadmap / seed_from_roadmap ---------------------------------
    # Roadmap items projected into the front table.
    ROADMAP_INGESTED = auto()
    # One front promoted to a planned task.
    FRONT_SEEDED = auto()
    # Summary event at the end of a seed pass.
    SEED_PASS_COMPLETE = auto()

    # --- deadman (dynamic prefix) -------------------------------------------
    # These are composed at runtime as ``deadman_<level.value>``.
    # The prefix constant lets callers build the kind from a typed root.
    DEADMAN_INFO = auto()
    DEADMAN_WARN = auto()
    DEADMAN_ALERT = auto()
    DEADMAN_DEAD = auto()

    # --- oauth_watch (dynamic prefix) ---------------------------------------
    # Composed at runtime as ``oauth_watch_<status.value>``.
    OAUTH_WATCH_EXPIRING_SOON = auto()
    OAUTH_WATCH_EXPIRING_URGENTLY = auto()
    OAUTH_WATCH_CRITICAL = auto()
    OAUTH_WATCH_EXPIRED = auto()
    OAUTH_WATCH_UNKNOWN = auto()

    # --- github_source ------------------------------------------------------
    # A single org release feed or topic query raised an exception; other
    # sources continue.  Payload: {source_kind, source_name, error}.
    AUTO_IMPROVE_SOURCE_FAILED = auto()


# ---------------------------------------------------------------------------
# Convenience helpers for dynamically-composed kinds
# ---------------------------------------------------------------------------


def deadman_kind(level_value: str) -> str:
    """Return the ledger event kind for a deadman notification level.

    ``level_value`` is the ``.value`` of a
    ``oxi_core.v3.notification.Level`` member.

    Example::

        from oxi_core.v3.notification import Level
        kind = deadman_kind(Level.WARN.value)   # "deadman_warn"
    """
    return f"deadman_{level_value}"


def oauth_watch_kind(status_value: str) -> str:
    """Return the ledger event kind for an OAuth-watch status.

    ``status_value`` is the ``.value`` of an
    ``oxi_core.v3.oauth_watch.Status`` member.

    Example::

        from oxi_core.v3.oauth_watch import Status
        kind = oauth_watch_kind(Status.EXPIRED.value)  # "oauth_watch_expired"
    """
    return f"oauth_watch_{status_value}"


# ---------------------------------------------------------------------------
# Complete set of statically-known kind strings as a frozenset
# ---------------------------------------------------------------------------

#: All statically-known event kinds.  Useful for validation and exhaustive
#: tests.  Dynamically-composed kinds (deadman_*, oauth_watch_*) are
#: represented by their enum member values above; all combinations can be
#: enumerated from the Level / Status enums in their respective modules.
ALL_STATIC_KINDS: frozenset[str] = frozenset(e.value for e in LedgerEvent)


__all__ = [
    "ALL_STATIC_KINDS",
    "LedgerEvent",
    "deadman_kind",
    "oauth_watch_kind",
]
