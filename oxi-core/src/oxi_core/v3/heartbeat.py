"""Heartbeat — the reaper that rescues tasks dispatch left behind.

On every tick, heartbeat scans the ``task`` table for rows that look
stuck:

- Status is ``dispatched``.
- ``last_progress_at`` is older than the configured grace period.
- ``pr_number`` is NULL (tasks with an open PR belong to pr_watcher,
  not to the reaper — anti-pattern #4 from the prior-orchestrator
  post-mortem).

Stuck rows transition to ``abandoned`` with an ``abandoned_by_heartbeat``
ledger event and a short reason. If a fork wants different reap
semantics (retry instead of abandon, escalate to an operator, etc.),
it overrides by either changing the adapter-configured grace period
or by running its own reaper alongside heartbeat.

Shared invariants with ``dispatch.py``:

- ``is_orphan_reapable`` — the same helper that dispatch uses. Both
  modules agree: never abandon a task with a PR.
- Grace-period check reads ``updated_at`` (the stamped-on-transition
  freshness) and ``last_progress_at`` (the explicit progress beacon),
  never ``created_at``. This is the fix for the orphan-reap-thrash
  bug class documented in semantic memory.
- ``engine_state.is_stopping()`` is checked at the top of every
  reap pass (the loop is small so there's only one iteration per
  tick, but the check is consistent with dispatch).

Heartbeat does NOT:

- Kill subprocesses. If a dispatched process is actually still
  running, reaping its row won't stop it. Process cleanup is the
  responsibility of dispatch_invoke's wall-clock timeout. Heartbeat
  is the paper trail that reconciles DB state with reality after the
  fact.
- Touch PR state. pr_watcher (future P1-6) owns transitions driven
  by GitHub state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .dispatch import Task, is_orphan_reapable
from .engine_state import EngineState

logger = logging.getLogger(__name__)

# Grace period default. Can be overridden via ``reap()``'s argument so
# tests can exercise the logic with realistic-looking values (10 min)
# without waiting 10 minutes. Production should use something closer
# to 30 minutes, tuned to the longest reasonable dispatch duration.
DEFAULT_STALE_AFTER_SECONDS = 10 * 60  # 10 minutes


@dataclass(frozen=True)
class ReapReport:
    """Summary of one reap pass.

    Attributes:
        considered: how many ``dispatched`` rows the pass looked at.
        abandoned: how many rows were transitioned to ``abandoned``.
        protected_by_pr: rows that would have been reaped but had an
            open PR (anti-pattern #4 protection kicked in).
        skipped_fresh: rows whose freshness was within the grace window.
    """

    considered: int
    abandoned: int
    protected_by_pr: int
    skipped_fresh: int


def _parse_sqlite_timestamp(value: str | None) -> datetime | None:
    """Parse the 'YYYY-MM-DD HH:MM:SS' format SQLite emits.

    Returns None on None input or parse failure. The dispatch module
    writes timestamps in this shape; the SQLite DEFAULT also uses it.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _effective_freshness(task: Task) -> datetime | None:
    """Return the timestamp heartbeat compares against the grace window.

    Prefers ``last_progress_at`` (the explicit progress beacon stamped
    at every transition). Falls back to ``updated_at`` (also stamped
    at every transition, per the atomic-transition invariant). NEVER
    uses ``created_at`` — that's the bug class.
    """
    lpa = _parse_sqlite_timestamp(task.last_progress_at)
    if lpa is not None:
        return lpa
    return _parse_sqlite_timestamp(task.updated_at)


def _is_stale(task: Task, now: datetime, stale_after: timedelta) -> bool:
    """Return True if the task's freshness is older than the grace window."""
    freshness = _effective_freshness(task)
    if freshness is None:
        # No freshness timestamp at all → treat as stale. This is a
        # defensive fallback; rows written by ``dispatch.py`` always
        # have last_progress_at stamped.
        return True
    return (now - freshness) > stale_after


def _load_dispatched_tasks(conn: sqlite3.Connection) -> list[Task]:
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'dispatched' ORDER BY id ASC"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _abandon(conn: sqlite3.Connection, task: Task, reason: str) -> None:
    """Transition a stuck task to ``abandoned``. Atomic with ledger write."""
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.execute(
            "UPDATE task SET status = 'abandoned', "
            "last_progress_at = ?, "
            "updated_at = ?, "
            "failure_reason = ? "
            "WHERE id = ? AND status = 'dispatched'",
            (now, now, reason, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task.id,
                "abandoned_by_heartbeat",
                json.dumps({"reason": reason, "identifier": task.identifier}),
            ),
        )


def reap(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
) -> ReapReport:
    r"""Run one reap pass. Returns what happened.

    Arguments:
        conn: SQLite connection (from ``db.connect``).
        engine_state: shared stopping flag. If set at entry, the pass
            is a no-op and returns an empty report.
        stale_after_seconds: grace window. Defaults to 10 minutes; set
            lower in tests for deterministic results.
        now: override for the current time. Tests pass a fixed value
            to make staleness deterministic. Production omits.

    Safety properties (tested in test_heartbeat.py):

    - Rows with \`pr_number IS NOT NULL\` are never abandoned, even if
      their freshness is older than the grace window. They belong to
      pr_watcher.
    - ``last_progress_at`` is preferred over ``updated_at``. Only rows
      with NEITHER timestamp are treated as stale by default.
    - Every abandonment writes a ledger event. No silent state flips.
    """
    if engine_state.is_stopping():
        logger.info("heartbeat: engine stopping, skipping reap")
        return ReapReport(
            considered=0, abandoned=0, protected_by_pr=0, skipped_fresh=0
        )

    now_dt = now if now is not None else datetime.now(tz=UTC)
    stale_after = timedelta(seconds=stale_after_seconds)
    tasks = _load_dispatched_tasks(conn)

    abandoned = 0
    protected = 0
    fresh = 0

    for task in tasks:
        # Anti-pattern #4: never abandon a task with a PR open.
        if not is_orphan_reapable(task):
            protected += 1
            continue
        if not _is_stale(task, now_dt, stale_after):
            fresh += 1
            continue
        _abandon(task=task, conn=conn, reason="stale_no_progress")
        abandoned += 1

    return ReapReport(
        considered=len(tasks),
        abandoned=abandoned,
        protected_by_pr=protected,
        skipped_fresh=fresh,
    )


__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "ReapReport",
    "reap",
]
