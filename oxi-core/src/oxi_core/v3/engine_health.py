"""Auto-healing engine self-state — T1-18.

When N consecutive dispatches fail (any terminal failure: FAILED or
TIMEOUT), the engine transitions to an ``UNHEALTHY`` state. While
unhealthy:

- ``dispatch_one`` refuses to start new dispatches.
- ``is_healthy()`` returns False so callers can gate other work.
- An ``engine_unhealthy`` event is written to the DB ledger once on
  each transition into the unhealthy state (not on every tick).
- A ``HEALTH_BANNER`` constant is exported so the dashboard can render
  a persistent banner.

The operator resets the unhealthy state via:

    oxi v3 heal

or equivalently via the existing:

    oxi v3 unkill     (which also clears the killswitch)

Both call ``heal()`` on the ``EngineHealth`` instance after resetting
whatever file-based flags they manage.

Architecture decisions
----------------------
- ``EngineHealth`` is a separate object from ``EngineState`` to keep
  ``EngineState`` small (it is widely imported and was intentionally
  minimal). ``dispatch_one`` receives both.
- Consecutive-failure counting is in memory only — it resets on every
  engine restart. Persistent "N failures since boot" would require a
  new DB column, but the contract is "N consecutive failures **in this
  run**", which is cheaper and sufficient.  The DB event provides
  cross-session visibility; the in-memory counter provides the gate.
- The ``engine_unhealthy`` / ``engine_healed`` events are task-id=NULL
  (engine-level, not task-level). The schema allows NULL task_id on the
  ``event`` table (FOREIGN KEY with nullable).
- Thread-safety: a ``threading.Lock`` guards the mutable fields so the
  dispatch pool (multiple threads/tasks) does not race on the counter.
- ``record_dispatch_result`` must be called AFTER ``_record_outcome``
  inside ``dispatch_one`` so the counter only increments after the DB
  row is committed (fail-safe: DB write happened).
- Healed state is stored in memory and in the DB as an event; the in-
  memory state is authoritative for dispatch gating.
- No adapter coupling: pure in-process state + SQLite events.
- Engine-stop is checked by ``EngineState``, not here. This module does
  not need to know about the stop flag.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from ._timefmt import now_iso as _now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: How many consecutive terminal dispatch failures must occur before the
#: engine pauses dispatch and emits ``engine_unhealthy``.  Operators
#: may override per-engine by passing a different value to the
#: constructor.  Five consecutive failures is a reasonable default: it
#: is high enough to ignore transient hiccups (one-off rate limits,
#: brief network interruptions) but low enough to catch a systematic
#: misconfiguration before it burns the daily budget.
DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD: int = 5

#: Ledger event kind written when the engine becomes unhealthy.
EVENT_KIND_UNHEALTHY = "engine_unhealthy"

#: Ledger event kind written when the engine is healed.
EVENT_KIND_HEALED = "engine_healed"

#: Dashboard banner text — rendered by ``dashboard.render_html`` when the
#: engine is unhealthy.
HEALTH_BANNER = (
    "⚠ ENGINE UNHEALTHY — dispatch is paused after consecutive failures. "
    "Run `oxi v3 heal` to resume."
)


# ---------------------------------------------------------------------------
# HealthStatus
# ---------------------------------------------------------------------------


class HealthStatus(Enum):
    """Current dispatch health."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


# ---------------------------------------------------------------------------
# EngineHealth
# ---------------------------------------------------------------------------


@dataclass
class EngineHealth:
    """Tracks consecutive dispatch failures and gates dispatch when sick.

    Attributes:
        consecutive_failure_threshold: how many back-to-back terminal
            failures (FAILED or TIMEOUT) must occur before the engine
            transitions to UNHEALTHY.  Defaults to
            ``DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD``.
        _status: current health status.
        _consecutive_failures: running count of terminal failures since
            the last SUCCESS or heal.
        _lock: guards mutable fields for thread-safety.
    """

    consecutive_failure_threshold: int = DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD
    _status: HealthStatus = field(
        default=HealthStatus.HEALTHY, init=False, repr=False
    )
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.consecutive_failure_threshold < 1:
            raise ValueError(
                f"consecutive_failure_threshold must be >= 1, "
                f"got {self.consecutive_failure_threshold}"
            )

    # ---- Public query API ------------------------------------------------

    def is_healthy(self) -> bool:
        """Return True if the engine may dispatch tasks."""
        with self._lock:
            return self._status is HealthStatus.HEALTHY

    def is_unhealthy(self) -> bool:
        """Return True if dispatch is paused due to consecutive failures."""
        with self._lock:
            return self._status is HealthStatus.UNHEALTHY

    def status(self) -> HealthStatus:
        """Return the current ``HealthStatus``."""
        with self._lock:
            return self._status

    def consecutive_failures(self) -> int:
        """Return the current consecutive-failure count (snapshot)."""
        with self._lock:
            return self._consecutive_failures

    # ---- Mutation API ----------------------------------------------------

    def record_success(self) -> None:
        """Record a successful dispatch outcome.

        Resets the consecutive-failure counter but does NOT change the
        health status — once unhealthy the operator must call ``heal()``.
        This keeps the health state sticky (operators see it) rather than
        auto-recovering on a single success (which could mask flapping).
        """
        with self._lock:
            self._consecutive_failures = 0
            logger.debug("engine_health: consecutive_failures reset to 0")

    def record_failure(
        self,
        conn: sqlite3.Connection | None,
        *,
        reason: str = "",
    ) -> None:
        """Record a terminal dispatch failure.

        Increments the consecutive-failure counter. If the counter
        reaches the threshold and the engine is currently healthy,
        transitions to UNHEALTHY and emits an ``engine_unhealthy`` event
        to the ledger.

        Arguments:
            conn: active SQLite connection.  If None (e.g. during tests
                that don't need DB events), the event write is skipped.
            reason: descriptive reason string for the ledger event.
        """
        with self._lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures

            logger.debug(
                "engine_health: consecutive_failures=%d (threshold=%d)",
                count,
                self.consecutive_failure_threshold,
            )

            if (
                count >= self.consecutive_failure_threshold
                and self._status is HealthStatus.HEALTHY
            ):
                self._status = HealthStatus.UNHEALTHY
                logger.warning(
                    "engine_health: UNHEALTHY after %d consecutive failures "
                    "(reason: %r). Dispatch paused. Run `oxi v3 heal` to resume.",
                    count,
                    reason,
                )
                if conn is not None:
                    _emit_unhealthy(conn, count=count, reason=reason)

    def heal(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        operator: str = "",
    ) -> bool:
        """Reset health to HEALTHY and clear the consecutive-failure counter.

        Called by ``oxi v3 heal`` (and ``oxi v3 unkill`` for convenience).

        Arguments:
            conn: active SQLite connection for ledger event.  If None,
                the event write is skipped.
            operator: optional string identifying who triggered the heal
                (e.g. ``"oxi v3 heal"``) — stored in the event payload.

        Returns True if the engine was unhealthy (i.e. something changed);
        False if it was already healthy (idempotent).
        """
        with self._lock:
            was_unhealthy = self._status is HealthStatus.UNHEALTHY
            prior_count = self._consecutive_failures
            self._status = HealthStatus.HEALTHY
            self._consecutive_failures = 0

        if was_unhealthy:
            logger.info(
                "engine_health: HEALED (prior consecutive_failures=%d, operator=%r)",
                prior_count,
                operator,
            )
            if conn is not None:
                _emit_healed(conn, prior_count=prior_count, operator=operator)
        else:
            logger.debug(
                "engine_health: heal() called on healthy engine — no-op"
            )

        return was_unhealthy


# ---------------------------------------------------------------------------
# DB helpers (engine-level events, task_id = NULL)
# ---------------------------------------------------------------------------


def _emit_unhealthy(
    conn: sqlite3.Connection,
    *,
    count: int,
    reason: str,
) -> None:
    """Write an ``engine_unhealthy`` ledger event. task_id is NULL."""
    now = _now_iso()
    payload = json.dumps(
        {
            "consecutive_failures": count,
            "reason": reason[:500] if reason else "",
            "timestamp": now,
        }
    )
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (EVENT_KIND_UNHEALTHY, payload),
        )
    logger.debug("engine_health: emitted %s event", EVENT_KIND_UNHEALTHY)


def _emit_healed(
    conn: sqlite3.Connection,
    *,
    prior_count: int,
    operator: str,
) -> None:
    """Write an ``engine_healed`` ledger event. task_id is NULL."""
    now = _now_iso()
    payload = json.dumps(
        {
            "prior_consecutive_failures": prior_count,
            "operator": operator[:200] if operator else "",
            "timestamp": now,
        }
    )
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (EVENT_KIND_HEALED, payload),
        )
    logger.debug("engine_health: emitted %s event", EVENT_KIND_HEALED)


# ---------------------------------------------------------------------------
# Query helpers (used by dashboard and CLI)
# ---------------------------------------------------------------------------


def is_engine_unhealthy_from_db(conn: sqlite3.Connection) -> bool:
    """Return True if the most recent engine health event is ``engine_unhealthy``.

    This is used by the dashboard (which has no access to the in-memory
    ``EngineHealth`` object) to decide whether to show the health banner.
    The DB event is the durable signal; the in-memory state is the
    real-time gate.

    If both an ``engine_unhealthy`` and an ``engine_healed`` event exist,
    the most recent one wins.
    """
    row = conn.execute(
        "SELECT kind FROM event "
        "WHERE kind IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (EVENT_KIND_UNHEALTHY, EVENT_KIND_HEALED),
    ).fetchone()
    if row is None:
        return False
    return bool(row[0] == EVENT_KIND_UNHEALTHY)


def get_last_unhealthy_event(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the payload of the most recent ``engine_unhealthy`` event, or None."""
    row = conn.execute(
        "SELECT payload, created_at FROM event WHERE kind = ? ORDER BY id DESC LIMIT 1",
        (EVENT_KIND_UNHEALTHY,),
    ).fetchone()
    if row is None:
        return None
    payload: dict[str, Any]
    try:
        payload = cast(dict[str, Any], json.loads(row["payload"] or "{}"))
    except (TypeError, ValueError):
        payload = {}
    payload["created_at"] = row["created_at"]
    return payload


__all__ = [
    "DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD",
    "EVENT_KIND_HEALED",
    "EVENT_KIND_UNHEALTHY",
    "HEALTH_BANNER",
    "EngineHealth",
    "HealthStatus",
    "get_last_unhealthy_event",
    "is_engine_unhealthy_from_db",
]
