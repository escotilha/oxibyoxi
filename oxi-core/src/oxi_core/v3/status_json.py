"""Stable JSON payload for ``oxi status --json``.

Collects task counts, budget state, heartbeat snapshot, and engine
metadata into a single dict that callers can ``json.dumps()`` directly.

The payload is designed to be:

- **Stable** — keys are never removed without a schema-version bump.
  Callers may rely on key presence; absence signals the version of
  oxi-core that produced the payload doesn't support the field yet.
- **Self-describing** — ``schema_version`` (currently ``1``) lets
  downstream scripts detect incompatible changes without parsing the
  payload body.
- **Read-only** — this module never writes to the DB.

Schema is documented in ``docs/status-json-schema.md``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..adapter import Adapter

# Increment when the top-level payload shape changes in a
# backward-incompatible way (key removed or type changed).
SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return task count grouped by status, all statuses."""
    return {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM task GROUP BY status"
        )
    }


def _task_list(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recently updated tasks (up to *limit*)."""
    rows = conn.execute(
        "SELECT identifier, tier, title, status, pr_number, "
        "cost_usd, last_progress_at, dispatched_at, merged_at, "
        "failed_at, failure_reason, created_at, updated_at "
        "FROM task ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        result.append({
            "identifier": row["identifier"],
            "tier": row["tier"],
            "title": row["title"],
            "status": row["status"],
            "pr_number": row["pr_number"],
            "cost_usd": row["cost_usd"],
            "last_progress_at": row["last_progress_at"],
            "dispatched_at": row["dispatched_at"],
            "merged_at": row["merged_at"],
            "failed_at": row["failed_at"],
            "failure_reason": row["failure_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return result


def _budget_section(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return budget snapshot as a JSON-safe dict."""
    from .budget import check as budget_check

    status = budget_check(conn)
    return {
        "verdict": status.verdict.value,
        "today_spend_usd": status.today_spend_usd,
        "daily_soft_warn_usd": status.daily_soft_warn_usd,
        "daily_hard_cap_usd": status.daily_hard_cap_usd,
        "window_start": status.window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _heartbeat_section(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return heartbeat / orphan-reap snapshot as a JSON-safe dict.

    Counts tasks in each reapable-adjacent state without actually
    reaping them. The values mirror what ``heartbeat.reap()`` would
    report on the next tick — useful for ops scripts that poll status
    to decide whether the engine is healthy.
    """
    dispatched_total = conn.execute(
        "SELECT COUNT(*) FROM task WHERE status = 'dispatched'"
    ).fetchone()[0]

    # Tasks that are dispatched, have no PR, and *might* be stale.
    # We don't check the grace window here — just the structural condition
    # (no PR). Callers who need the real staleness verdict can run
    # heartbeat.reap() themselves.
    dispatched_without_pr = conn.execute(
        "SELECT COUNT(*) FROM task "
        "WHERE status = 'dispatched' AND pr_number IS NULL"
    ).fetchone()[0]

    dispatched_with_pr = dispatched_total - dispatched_without_pr

    abandoned_today = conn.execute(
        "SELECT COUNT(*) FROM task "
        "WHERE status = 'abandoned' "
        "AND updated_at >= datetime('now', '-1 day')"
    ).fetchone()[0]

    return {
        "dispatched_total": dispatched_total,
        "dispatched_without_pr": dispatched_without_pr,
        "dispatched_with_pr": dispatched_with_pr,
        "abandoned_last_24h": abandoned_today,
    }


def _killswitch_section() -> dict[str, Any]:
    """Return killswitch state as a JSON-safe dict."""
    from .kill import is_set, path

    ks_path = path()
    active = is_set(ks_path)
    reason: str | None = None
    if active:
        text = ks_path.read_text().strip()
        reason = text if text else None
    return {
        "active": active,
        "reason": reason,
        "path": str(ks_path),
    }


def build(
    conn: sqlite3.Connection,
    adapter: Adapter,
    *,
    tasks_limit: int = 50,
) -> dict[str, Any]:
    """Build the full status JSON payload.

    Arguments:
        conn: open SQLite connection (from ``db.connect()``).
        adapter: active adapter, used for instance metadata.
        tasks_limit: how many recent tasks to include in ``tasks``.
            Defaults to 50; set to 0 to omit the task list.

    Returns a plain ``dict`` ready for ``json.dumps()``. All values are
    JSON-serialisable (str, int, float, bool, None, list, dict).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "instance": adapter.naming().instance_name,
        "plan_tier": adapter.plan_tier(),
        "repo": adapter.github_repo(),
        "task_counts": _task_counts(conn),
        "tasks": _task_list(conn, limit=tasks_limit),
        "budget": _budget_section(conn),
        "heartbeat": _heartbeat_section(conn),
        "killswitch": _killswitch_section(),
    }


__all__ = [
    "SCHEMA_VERSION",
    "build",
]
