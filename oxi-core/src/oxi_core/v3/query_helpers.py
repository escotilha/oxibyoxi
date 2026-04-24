"""Typed wrapper functions for repeated SQL patterns across the v3 engine.

Every v3 module that touches the database uses the same handful of SQL
shapes repeatedly:

- Fetch all tasks in a given status.
- Fetch a single task by primary-key ID.
- Insert a ledger event (with or without a task_id).
- Stamp ``last_progress_at`` + ``updated_at`` on a task row without
  changing its status.
- Atomically update a task's status and timestamps in one transaction.
- Count ledger events for a task filtered by kind.

Before this module, each caller inlined its own copy of these queries.
String drift (a mistyped column name, a forgotten ``updated_at`` stamp,
a missing ``WHERE id = ?``) was caught only at runtime — not statically.

This module centralises the five-plus patterns into typed helpers.
The contract is:

- **No behaviour change**: every helper is a thin wrapper; all
  callers that are ported to use these helpers must produce the same
  rows, events, and side-effects as before.
- **No new columns, tables, or migrations**: all SQL is read-only or
  writes to pre-existing columns.
- **sqlite3.Row compatibility**: helpers that return task rows still
  return ``Task`` objects (via ``Task.from_row``) so callers need no
  changes beyond the import.
- **Type annotations throughout**: parameter types match what callers
  already pass; return types are as narrow as the underlying data
  allows.

Usage
-----
.. code-block:: python

    from oxi_core.v3.query_helpers import (
        select_task_by_id,
        select_tasks_by_status,
        insert_event,
        insert_engine_event,
        stamp_progress,
        atomic_status_update,
        count_events_by_kind,
    )

    task = select_task_by_id(conn, 42)
    planned = select_tasks_by_status(conn, "planned")
    insert_event(conn, task_id=task.id, kind="my_event", payload={"k": "v"})
    stamp_progress(conn, task_id=task.id)
    atomic_status_update(conn, task_id=task.id, new_status="merged")

Design notes
------------
- All helpers that write to the DB use ``with conn:`` so writes are
  wrapped in a SQLite transaction.  This matches the convention in
  every v3 caller.
- ``insert_engine_event`` is a separate function from ``insert_event``
  because the SQL differs (``task_id = NULL`` vs ``task_id = ?``).
  A single function with an optional ``task_id`` would require callers
  to pass ``None`` explicitly, which is an easy mistake.
- ``stamp_progress`` calls ``_now_iso()`` once and uses the same
  timestamp for both columns so they are always identical within a
  single call — consistent with the caller convention.
- ``atomic_status_update`` accepts ``**extra_cols`` for columns that
  vary by caller (e.g. ``merged_at``, ``failed_at``, ``failure_reason``,
  ``dispatched_at``).  The allowed set is validated against the schema
  so unknown column names raise immediately rather than silently
  producing a broken UPDATE.
- ``count_events_by_kind`` accepts a single kind or a frozenset of
  kinds; the SQL differs but the return type is always ``int``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ._timefmt import now_iso as _now_iso
from .dispatch import Task

# ---------------------------------------------------------------------------
# Allowed extra columns for atomic_status_update
# (must exactly match column names in the ``task`` table)
# ---------------------------------------------------------------------------

_ALLOWED_EXTRA_COLS: frozenset[str] = frozenset(
    {
        "dispatched_at",
        "failed_at",
        "failure_reason",
        "merged_at",
        "pr_number",
        "subtitle",
        "cost_usd",
    }
)


# ---------------------------------------------------------------------------
# select_task_by_id
# ---------------------------------------------------------------------------


def select_task_by_id(
    conn: sqlite3.Connection,
    task_id: int,
) -> Task | None:
    """Return the ``Task`` row with the given primary-key ID, or ``None``.

    Parameters
    ----------
    conn:
        Open SQLite connection (from ``db.connect``).
    task_id:
        Integer primary key of the ``task`` row.

    Returns
    -------
    Task | None
        The hydrated ``Task`` object, or ``None`` if no row exists with
        that ID.

    Examples
    --------
    .. code-block:: python

        task = select_task_by_id(conn, 7)
        if task is None:
            raise RuntimeError("task 7 not found")
    """
    row = conn.execute(
        "SELECT * FROM task WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return Task.from_row(row)


# ---------------------------------------------------------------------------
# select_tasks_by_status
# ---------------------------------------------------------------------------


def select_tasks_by_status(
    conn: sqlite3.Connection,
    status: str,
    *,
    order_by: str = "id ASC",
    limit: int | None = None,
    pr_number_filter: str | None = None,
) -> list[Task]:
    """Return all task rows whose ``status`` column equals *status*.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    status:
        Exact status string to filter on (e.g. ``'planned'``,
        ``'dispatched'``, ``'failed'``, ``'abandoned'``).
    order_by:
        SQL ORDER BY clause fragment.  Defaults to ``id ASC`` for
        deterministic ordering.  Pass ``'tier ASC, identifier ASC'``
        for the dispatch-picker ordering.
    limit:
        Optional LIMIT clause.  ``None`` means no limit (returns all
        matching rows).
    pr_number_filter:
        Optional extra filter on ``pr_number``.  Accepted values:

        - ``'is_null'``   — ``AND pr_number IS NULL``
        - ``'not_null'``  — ``AND pr_number IS NOT NULL``
        - ``None``        — no pr_number filter (default)

    Returns
    -------
    list[Task]
        Hydrated ``Task`` objects in the requested order.

    Examples
    --------
    .. code-block:: python

        # All dispatched tasks, no PR yet:
        tasks = select_tasks_by_status(
            conn, "dispatched", pr_number_filter="is_null"
        )

        # Top-1 planned task by tier then identifier:
        tasks = select_tasks_by_status(
            conn, "planned",
            order_by="tier ASC, identifier ASC",
            limit=1,
        )
    """
    # Build SQL dynamically but only from a controlled set of fragments.
    sql = "SELECT * FROM task WHERE status = ? "  # noqa: S608 — status is a param
    params: list[Any] = [status]

    if pr_number_filter == "is_null":
        sql += "AND pr_number IS NULL "
    elif pr_number_filter == "not_null":
        sql += "AND pr_number IS NOT NULL "
    elif pr_number_filter is not None:
        raise ValueError(
            f"pr_number_filter must be 'is_null', 'not_null', or None; "
            f"got {pr_number_filter!r}"
        )

    sql += f"ORDER BY {order_by} "
    if limit is not None:
        sql += "LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [Task.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# insert_event
# ---------------------------------------------------------------------------


def insert_event(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    kind: str,
    payload: dict[str, Any],
) -> None:
    """Insert one row into the ``event`` table for a specific task.

    The write is wrapped in ``with conn:`` (SQLite savepoint transaction).
    Callers that want the event to be part of a larger transaction should
    call this inside their own ``with conn:`` block — SQLite's nested
    savepoints ensure correctness.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    task_id:
        Integer FK into ``task.id``.  Must not be ``None``; use
        ``insert_engine_event`` for task-less engine-level events.
    kind:
        Event kind string.  Use ``LedgerEvent`` constants where possible.
    payload:
        Dict serialised to JSON.  Must be JSON-serialisable.

    Raises
    ------
    TypeError
        If ``payload`` is not JSON-serialisable.
    sqlite3.IntegrityError
        If ``task_id`` does not exist in the ``task`` table and foreign
        keys are enabled (which ``db.connect`` always does).

    Examples
    --------
    .. code-block:: python

        insert_event(
            conn,
            task_id=42,
            kind=LedgerEvent.DISPATCH_STARTED,
            payload={"session_id": "abc123"},
        )
    """
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, kind, json.dumps(payload)),
        )


# ---------------------------------------------------------------------------
# insert_engine_event
# ---------------------------------------------------------------------------


def insert_engine_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict[str, Any],
) -> None:
    """Insert a task-less engine-level event (``task_id = NULL``).

    Used for events that are not associated with a specific task row —
    budget signals, roadmap ingest summaries, seed-pass completions,
    engine health transitions, etc.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    kind:
        Event kind string.  Use ``LedgerEvent`` constants where possible.
    payload:
        Dict serialised to JSON.

    Examples
    --------
    .. code-block:: python

        insert_engine_event(
            conn,
            kind=LedgerEvent.ROADMAP_INGESTED,
            payload={"inserted": 3, "updated": 1, "total": 4},
        )
    """
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (kind, json.dumps(payload)),
        )


# ---------------------------------------------------------------------------
# stamp_progress
# ---------------------------------------------------------------------------


def stamp_progress(
    conn: sqlite3.Connection,
    *,
    task_id: int,
) -> str:
    """Stamp ``last_progress_at`` and ``updated_at`` on a task row.

    Does **not** change ``status``.  Use this to keep heartbeat from
    reaping a task that is making forward progress without a full
    status transition.

    Both columns receive the *same* timestamp value (generated once
    inside this function) so they are always identical for any given
    call — consistent with the convention across all v3 callers.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    task_id:
        Integer PK of the task row to freshen.

    Returns
    -------
    str
        The ISO timestamp written to both columns.  Useful for tests
        that need to assert the exact value.

    Examples
    --------
    .. code-block:: python

        ts = stamp_progress(conn, task_id=task.id)
        # task.last_progress_at is now ts
    """
    now = _now_iso()
    with conn:
        conn.execute(
            "UPDATE task SET last_progress_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task_id),
        )
    return now


# ---------------------------------------------------------------------------
# atomic_status_update
# ---------------------------------------------------------------------------


def atomic_status_update(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    new_status: str,
    guard_status: str | None = None,
    **extra_cols: Any,
) -> str:
    """Update a task's status and timestamps in one atomic transaction.

    Always stamps ``last_progress_at`` and ``updated_at`` with the same
    timestamp.  Optionally stamps additional columns (e.g. ``merged_at``,
    ``failed_at``, ``dispatched_at``, ``failure_reason``) via
    ``extra_cols``.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    task_id:
        Integer PK of the task row to update.
    new_status:
        New value for ``task.status`` (e.g. ``'dispatched'``,
        ``'merged'``, ``'failed'``, ``'abandoned'``, ``'planned'``).
    guard_status:
        If not ``None``, appends ``AND status = '<guard_status>'`` to
        the WHERE clause.  This prevents double-transitions (e.g. from
        ``dispatched`` → ``failed`` when the row has already been moved
        to ``merged`` by a concurrent pr_watcher tick).
    **extra_cols:
        Additional column-value pairs to set in the same UPDATE.  Only
        columns in ``_ALLOWED_EXTRA_COLS`` are accepted; an unknown
        column name raises ``ValueError`` immediately.

        Common extras:
        - ``dispatched_at=now`` — when the task was first dispatched
        - ``failed_at=now``     — when the failure was recorded
        - ``merged_at=now``     — when the PR merged
        - ``failure_reason=str`` — short reason for failure

    Returns
    -------
    str
        The ISO timestamp written to ``last_progress_at`` and
        ``updated_at``.

    Raises
    ------
    ValueError
        If any key in ``extra_cols`` is not in ``_ALLOWED_EXTRA_COLS``.

    Examples
    --------
    .. code-block:: python

        # Simple: dispatched → merged
        atomic_status_update(conn, task_id=task.id, new_status="merged",
                             guard_status="dispatched", merged_at=now_iso())

        # With failure reason:
        atomic_status_update(
            conn, task_id=task.id, new_status="failed",
            guard_status="dispatched",
            failed_at=now_iso(), failure_reason="critic_rejected",
        )
    """
    unknown = set(extra_cols) - _ALLOWED_EXTRA_COLS
    if unknown:
        raise ValueError(
            f"atomic_status_update: unknown extra column(s): {sorted(unknown)}. "
            f"Allowed: {sorted(_ALLOWED_EXTRA_COLS)}"
        )

    now = _now_iso()
    # Build SET clause: status + timestamps + caller-supplied extras.
    set_fragments = ["status = ?", "last_progress_at = ?", "updated_at = ?"]
    params: list[Any] = [new_status, now, now]

    for col, val in extra_cols.items():
        set_fragments.append(f"{col} = ?")
        params.append(val)

    sql = f"UPDATE task SET {', '.join(set_fragments)} WHERE id = ?"
    params.append(task_id)

    if guard_status is not None:
        sql += " AND status = ?"
        params.append(guard_status)

    with conn:
        conn.execute(sql, params)
    return now


# ---------------------------------------------------------------------------
# count_events_by_kind
# ---------------------------------------------------------------------------


def count_events_by_kind(
    conn: sqlite3.Connection,
    task_id: int,
    kind: str | frozenset[str],
) -> int:
    """Count ledger events for a task filtered by event kind.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    task_id:
        Integer PK of the task whose events to count.
    kind:
        Either a single event kind string or a frozenset of kind strings.
        When a frozenset is passed, the count covers all matching kinds
        (``IN (...)`` clause).

    Returns
    -------
    int
        Number of matching rows in the ``event`` table.

    Examples
    --------
    .. code-block:: python

        # Single kind:
        n = count_events_by_kind(conn, task.id, "auto_recover_attempted")

        # Multiple kinds — equivalent to counting any failure event:
        from oxi_core.v3.deep_fix import _FAILURE_KINDS
        n = count_events_by_kind(conn, task.id, _FAILURE_KINDS)
    """
    if isinstance(kind, str):
        row = conn.execute(
            "SELECT COUNT(*) FROM event WHERE task_id = ? AND kind = ?",
            (task_id, kind),
        ).fetchone()
    else:
        # frozenset or other collection
        kinds = list(kind)
        if not kinds:
            return 0
        placeholders = ", ".join("?" * len(kinds))
        row = conn.execute(
            f"SELECT COUNT(*) FROM event WHERE task_id = ? AND kind IN ({placeholders})",
            [task_id, *kinds],
        ).fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "atomic_status_update",
    "count_events_by_kind",
    "insert_engine_event",
    "insert_event",
    "select_task_by_id",
    "select_tasks_by_status",
    "stamp_progress",
]
