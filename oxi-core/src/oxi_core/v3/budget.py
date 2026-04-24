"""Budget enforcement — the hard-cap that keeps a runaway loop from
spending the monthly budget in hours.

On every iteration that would spend money (dispatch, critic invocation),
the engine asks ``budget.check()`` for the current spend verdict:

- ``OK``: under the soft warn threshold. Proceed.
- ``WARN``: between soft and hard. Proceed but emit a one-time warning
  event so operators see the signal before the loop trips the hard
  cap.
- ``HARD_STOP``: at or over the hard cap. Refuse to spend more today.

The "today" window is UTC-day (midnight-to-midnight). Forks that want
a different window can compute the cutoff themselves and pass it via
``check(..., since=...)``.

Caps come from the active adapter's ``budget()`` method. The reference
adapter ships conservative defaults ($1 soft warn, $5 hard cap) so
accidentally-enabled dispatch can't blow budget.

Invariants:

- Budget is advisory at the point of call, authoritative after the
  first HARD_STOP event of the day. Once HARD_STOP lands in the
  ledger, subsequent checks return HARD_STOP even if the window
  rolls — the operator must explicitly clear the event if they want
  to resume mid-day.
- WARN emits at most one event per UTC day, so the ledger isn't
  spammed.
- The ``today_spend_usd`` query sums ``cost_usd`` on tasks whose
  ``updated_at`` falls in the window. Matches what brief.py uses,
  so spend totals align across surfaces.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from ..adapter import get_active_adapter

logger = logging.getLogger(__name__)


class Verdict(Enum):
    OK = "ok"
    WARN = "warn"
    HARD_STOP = "hard_stop"


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of budget state at one point in time."""

    verdict: Verdict
    today_spend_usd: float
    daily_soft_warn_usd: float
    daily_hard_cap_usd: float
    window_start: datetime


def _utc_day_start(now: datetime) -> datetime:
    """Return midnight UTC of the day containing ``now``."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _sum_cost_since(conn: sqlite3.Connection, since: datetime) -> float:
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM task "
        "WHERE updated_at >= ?",
        (since_str,),
    ).fetchone()
    return float(row[0] or 0.0)


def _hard_stop_event_exists_today(
    conn: sqlite3.Connection, since: datetime
) -> bool:
    """Return True if an ``auto_merge_rejected`` or ``budget_hard_stop``
    event with kind=budget_hard_stop was already logged today.

    The check is independent of which module emitted it — once the
    hard stop lands, it sticks for the rest of the UTC day.
    """
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT 1 FROM event WHERE kind = 'budget_hard_stop' "
        "AND created_at >= ? LIMIT 1",
        (since_str,),
    ).fetchone()
    return row is not None


def _warn_event_exists_today(
    conn: sqlite3.Connection, since: datetime
) -> bool:
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT 1 FROM event WHERE kind = 'budget_soft_warn' "
        "AND created_at >= ? LIMIT 1",
        (since_str,),
    ).fetchone()
    return row is not None


def _emit_event(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (kind, json.dumps(payload)),
        )


def check(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> BudgetStatus:
    """Return the current budget verdict.

    On first WARN of the day, emits a ``budget_soft_warn`` event.
    On first HARD_STOP of the day, emits a ``budget_hard_stop`` event.
    Subsequent calls the same day do not re-emit.

    Once a ``budget_hard_stop`` event exists for the current UTC day,
    ``check()`` returns HARD_STOP even if spend momentarily dips
    below the threshold (shouldn't happen, but defensive).
    """
    adapter = get_active_adapter()
    caps = adapter.budget()
    now_dt = now if now is not None else datetime.now(tz=UTC)
    window_start = _utc_day_start(now_dt)

    today_spend = _sum_cost_since(conn, window_start)

    # Sticky HARD_STOP: once the hard-stop event exists, stay stopped.
    if _hard_stop_event_exists_today(conn, window_start):
        return BudgetStatus(
            verdict=Verdict.HARD_STOP,
            today_spend_usd=today_spend,
            daily_soft_warn_usd=caps.daily_soft_warn,
            daily_hard_cap_usd=caps.daily_hard_cap,
            window_start=window_start,
        )

    if today_spend >= caps.daily_hard_cap:
        _emit_event(
            conn,
            "budget_hard_stop",
            {
                "today_spend_usd": today_spend,
                "daily_hard_cap_usd": caps.daily_hard_cap,
                "window_start": window_start.isoformat(),
            },
        )
        return BudgetStatus(
            verdict=Verdict.HARD_STOP,
            today_spend_usd=today_spend,
            daily_soft_warn_usd=caps.daily_soft_warn,
            daily_hard_cap_usd=caps.daily_hard_cap,
            window_start=window_start,
        )

    if today_spend >= caps.daily_soft_warn:
        if not _warn_event_exists_today(conn, window_start):
            _emit_event(
                conn,
                "budget_soft_warn",
                {
                    "today_spend_usd": today_spend,
                    "daily_soft_warn_usd": caps.daily_soft_warn,
                    "daily_hard_cap_usd": caps.daily_hard_cap,
                },
            )
        return BudgetStatus(
            verdict=Verdict.WARN,
            today_spend_usd=today_spend,
            daily_soft_warn_usd=caps.daily_soft_warn,
            daily_hard_cap_usd=caps.daily_hard_cap,
            window_start=window_start,
        )

    return BudgetStatus(
        verdict=Verdict.OK,
        today_spend_usd=today_spend,
        daily_soft_warn_usd=caps.daily_soft_warn,
        daily_hard_cap_usd=caps.daily_hard_cap,
        window_start=window_start,
    )


def is_hard_stopped(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> bool:
    """Convenience: ``check().verdict is Verdict.HARD_STOP``.

    Used by dispatch + auto_merge as a cheap pre-check before any
    Claude-spending operation.
    """
    return check(conn, now=now).verdict is Verdict.HARD_STOP


def clear_today(conn: sqlite3.Connection) -> int:
    """Delete ``budget_hard_stop`` + ``budget_soft_warn`` events for today.

    Escape hatch for operators who want to resume mid-day after
    raising caps or acknowledging the signal. Returns the number of
    events deleted.
    """
    now = datetime.now(tz=UTC)
    window_start = _utc_day_start(now).strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        cursor = conn.execute(
            "DELETE FROM event WHERE created_at >= ? "
            "AND kind IN ('budget_hard_stop', 'budget_soft_warn')",
            (window_start,),
        )
        return cursor.rowcount


def window_for(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return (start, end) of the UTC day containing ``now``.

    End is 24h after start. Exposed so callers like brief.py can align
    their windows with budget's.
    """
    now_dt = now if now is not None else datetime.now(tz=UTC)
    start = _utc_day_start(now_dt)
    return (start, start + timedelta(days=1))


__all__ = [
    "BudgetStatus",
    "Verdict",
    "check",
    "clear_today",
    "is_hard_stopped",
    "window_for",
]
