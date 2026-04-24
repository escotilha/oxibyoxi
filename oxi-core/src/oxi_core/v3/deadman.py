"""Deadman — detect when the engine has gone silent, and shout.

The orchestrator can stop working without crashing visibly:

- systemd timer fired but never got past the first call (auth expiry,
  network outage, permissions change)
- Claude's 7-day rate-limit ceiling hit, every new dispatch exits
  immediately
- Budget hard-stopped silently last night and no one noticed

``deadman.run`` scans the engine's own event log for silence. If it
hasn't seen a dispatch attempt in X minutes, or if the most recent
budget hard-stop has persisted for Y minutes, it escalates via the
injected ``NotificationBackend``. Operators wire that backend to
whatever they can actually receive (Slack, email, pager, stderr).

The escalation is level-bounded: INFO → WARN → ALERT → DEAD with
thresholds from the configuration. Once a level has been emitted
within the current observation window, deadman doesn't re-emit it —
the ledger captures the signal once.

Shares ``engine_state.is_stopping()`` with every other loop: if the
operator has halted the engine, deadman doesn't fire (silence is
expected).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .engine_state import EngineState
from .notification import Level, Notification, NotificationBackend

logger = logging.getLogger(__name__)


# Thresholds for the four escalation levels (minutes).
# INFO: early signal — something is unusual.
# WARN: sustained silence — operator should check soon.
# ALERT: serious — something is probably broken.
# DEAD: catastrophic — loop is effectively off.
#
# These are defaults; ``run()`` accepts override parameters.
DEFAULT_INFO_MINUTES = 30
DEFAULT_WARN_MINUTES = 90
DEFAULT_ALERT_MINUTES = 180
DEFAULT_DEAD_MINUTES = 360


@dataclass(frozen=True)
class DeadmanReport:
    """Outcome of one deadman pass."""

    level: Level | None                      # highest level emitted, or None
    last_dispatch_minutes_ago: float | None  # None if never
    budget_hard_stop_active: bool
    notified: bool                            # True if we sent a notification this pass


def _minutes_ago(then: datetime, now: datetime) -> float:
    return (now - then).total_seconds() / 60.0


def _last_dispatch_attempt_at(conn: sqlite3.Connection) -> datetime | None:
    """Most recent ``dispatch_started`` event timestamp, or None.

    Uses the same event kind the dispatch module emits on every
    transition — that's what "the engine is alive" actually means.
    """
    row = conn.execute(
        "SELECT created_at FROM event WHERE kind = 'dispatch_started' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        return datetime.strptime(
            row[0], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _budget_hard_stop_active(conn: sqlite3.Connection, now: datetime) -> bool:
    """True if a ``budget_hard_stop`` event exists in today's UTC window."""
    window_start = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT 1 FROM event WHERE kind = 'budget_hard_stop' "
        "AND created_at >= ? LIMIT 1",
        (window_start,),
    ).fetchone()
    return row is not None


def _already_notified_within(
    conn: sqlite3.Connection,
    level: Level,
    since: datetime,
) -> bool:
    """Has a deadman_<level> event been emitted since ``since``?

    Used to rate-limit notifications — one per level per observation
    window, not one per tick.
    """
    kind = f"deadman_{level.value}"
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT 1 FROM event WHERE kind = ? AND created_at >= ? LIMIT 1",
        (kind, since_str),
    ).fetchone()
    return row is not None


def _emit_deadman_event(
    conn: sqlite3.Connection,
    level: Level,
    payload: dict,
) -> None:
    import json
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (f"deadman_{level.value}", json.dumps(payload)),
        )


def _level_for(
    minutes_ago: float,
    *,
    info: float,
    warn: float,
    alert: float,
    dead: float,
) -> Level | None:
    """Bucket a silence duration into a level, or None if below threshold."""
    if minutes_ago >= dead:
        return Level.DEAD
    if minutes_ago >= alert:
        return Level.ALERT
    if minutes_ago >= warn:
        return Level.WARN
    if minutes_ago >= info:
        return Level.INFO
    return None


def run(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    notifier: NotificationBackend,
    *,
    info_minutes: float = DEFAULT_INFO_MINUTES,
    warn_minutes: float = DEFAULT_WARN_MINUTES,
    alert_minutes: float = DEFAULT_ALERT_MINUTES,
    dead_minutes: float = DEFAULT_DEAD_MINUTES,
    notification_rate_limit_minutes: float = DEFAULT_WARN_MINUTES,
    now: datetime | None = None,
) -> DeadmanReport:
    """Run one deadman pass.

    Arguments:
        conn: SQLite connection.
        engine_state: shared stopping flag. If set, pass is a no-op
            (silence is expected while the engine is halted).
        notifier: backend that actually delivers the notification.
            Always called for qualifying silence; rate-limited per
            level by ``_already_notified_within``.
        info/warn/alert/dead_minutes: silence thresholds.
        notification_rate_limit_minutes: don't re-notify the same
            level within this many minutes.
        now: override for tests.

    Returns a ``DeadmanReport`` describing what happened.
    """
    empty = DeadmanReport(
        level=None,
        last_dispatch_minutes_ago=None,
        budget_hard_stop_active=False,
        notified=False,
    )
    if engine_state.is_stopping():
        logger.info("deadman: engine stopping, silence is expected")
        return empty

    now_dt = now if now is not None else datetime.now(tz=UTC)
    last_dispatch = _last_dispatch_attempt_at(conn)
    budget_hard_stop = _budget_hard_stop_active(conn, now_dt)

    # Compute silence duration. Never-dispatched engines don't age
    # until they've existed for at least ``info_minutes``; otherwise
    # a fresh install would trip ALERT at install.
    if last_dispatch is None:
        # No dispatch events ever → treat as fresh. Don't fire.
        return DeadmanReport(
            level=None,
            last_dispatch_minutes_ago=None,
            budget_hard_stop_active=budget_hard_stop,
            notified=False,
        )

    minutes_ago = _minutes_ago(last_dispatch, now_dt)

    level = _level_for(
        minutes_ago,
        info=info_minutes, warn=warn_minutes,
        alert=alert_minutes, dead=dead_minutes,
    )

    if level is None:
        return DeadmanReport(
            level=None,
            last_dispatch_minutes_ago=minutes_ago,
            budget_hard_stop_active=budget_hard_stop,
            notified=False,
        )

    # Rate-limit: don't re-notify the same level within the window.
    window_start = now_dt - timedelta(minutes=notification_rate_limit_minutes)
    if _already_notified_within(conn, level, window_start):
        return DeadmanReport(
            level=level,
            last_dispatch_minutes_ago=minutes_ago,
            budget_hard_stop_active=budget_hard_stop,
            notified=False,
        )

    # Compose the notification and dispatch it.
    tags = ("deadman",)
    if budget_hard_stop:
        tags = tags + ("budget",)

    title = (
        f"oxi deadman — {level.value} (no dispatch in "
        f"{int(minutes_ago)}m)"
    )
    body_lines = [
        f"Last dispatch attempt: {last_dispatch.isoformat()}",
        f"Silence: {minutes_ago:.1f} minutes",
    ]
    if budget_hard_stop:
        body_lines.append(
            "Budget hard-stop is active — may be the cause of silence."
        )
    notification = Notification(
        level=level,
        title=title,
        body="\n".join(body_lines),
        tags=tags,
    )

    notifier.send(notification)
    _emit_deadman_event(
        conn,
        level,
        {
            "last_dispatch_minutes_ago": minutes_ago,
            "budget_hard_stop_active": budget_hard_stop,
            "thresholds": {
                "info": info_minutes,
                "warn": warn_minutes,
                "alert": alert_minutes,
                "dead": dead_minutes,
            },
        },
    )
    return DeadmanReport(
        level=level,
        last_dispatch_minutes_ago=minutes_ago,
        budget_hard_stop_active=budget_hard_stop,
        notified=True,
    )


__all__ = [
    "DEFAULT_ALERT_MINUTES",
    "DEFAULT_DEAD_MINUTES",
    "DEFAULT_INFO_MINUTES",
    "DEFAULT_WARN_MINUTES",
    "DeadmanReport",
    "run",
]
