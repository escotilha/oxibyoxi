"""Auto-replenish the planned-task queue from the ``front`` table.

Runs on every tick (called by the CLI / higher-level loop). If the
planned task count is below ``target_planned_count``, it picks the
highest-tier front past its cooldown that has no currently-open task,
and creates a planned task from it.

Invariants:

- Never creates a task for an identifier already present in ``task``
  (in any status). The planner's ``seed_tasks`` idempotency lives
  here too.
- Never re-seeds a front within its ``cooldown_hours``. If a front
  was just merged, it waits the cooldown before becoming eligible.
- Tier priority: tier 0 first, then tier 1, etc. Within a tier, the
  front whose ``last_seeded_at`` is oldest (or NULL) wins.
- Shares ``engine_state.is_stopping()`` with every other loop.

Emits one ledger event per seeded task (``front_seeded``) plus a
summary (``seed_pass_complete``) so operators see the autonomous
queue-refill rhythm.

Adapter-configurable: the trigger threshold and batch size. Defaults
(10 + 1) are conservative — a single task per tick replenishment,
only when the queue gets below 10 planned items.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ._timefmt import now_iso as _now_iso
from .engine_state import EngineState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedReport:
    planned_before: int
    planned_after: int
    seeded: int
    eligible: int  # fronts that passed cooldown + no-existing-task gates


def _planned_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM task WHERE status = 'planned'"
    ).fetchone()
    return int(row[0])


def _existing_identifiers(conn: sqlite3.Connection) -> set[str]:
    """Every identifier currently in ``task``, any status."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT identifier FROM task"
        )
    }


def _eligible_fronts(
    conn: sqlite3.Connection,
    existing_idents: set[str],
    *,
    now: datetime,
) -> list[sqlite3.Row]:
    """Fronts past their cooldown and not currently tracked in ``task``.

    Returned ordered by tier asc, then ``last_seeded_at`` asc (NULL
    first). Callers pick from the head.
    """
    rows = conn.execute(
        "SELECT identifier, tier, title, subtitle, last_seeded_at, "
        "cooldown_hours FROM front WHERE identifier IS NOT NULL "
        "ORDER BY tier ASC, last_seeded_at ASC NULLS FIRST"
    ).fetchall()

    eligible: list[sqlite3.Row] = []
    for row in rows:
        if row["identifier"] in existing_idents:
            continue
        if row["last_seeded_at"]:
            try:
                last = datetime.strptime(
                    row["last_seeded_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=UTC)
            except ValueError:
                last = None
            if last is not None:
                cooldown = timedelta(hours=float(row["cooldown_hours"] or 24.0))
                if (now - last) < cooldown:
                    continue
        eligible.append(row)
    return eligible


def _seed_one(
    conn: sqlite3.Connection,
    front_row: sqlite3.Row,
) -> None:
    """Insert a planned task for this front and stamp its last_seeded_at."""
    now = _now_iso()
    with conn:
        conn.execute(
            "INSERT INTO task (identifier, tier, title, subtitle, status, "
            "last_progress_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'planned', ?, ?)",
            (
                front_row["identifier"],
                front_row["tier"],
                front_row["title"],
                front_row["subtitle"] or "",
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE front SET last_seeded_at = ? WHERE identifier = ?",
            (now, front_row["identifier"]),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (
                "front_seeded",
                json.dumps({
                    "identifier": front_row["identifier"],
                    "tier": front_row["tier"],
                    "title": front_row["title"],
                }),
            ),
        )


def seed(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    target_planned_count: int = 10,
    batch_size: int = 1,
    now: datetime | None = None,
) -> SeedReport:
    """Run one replenish pass.

    Arguments:
        target_planned_count: if ``planned`` count < this, replenish.
            Default 10 — conservative; adapter forks can pass smaller.
        batch_size: max tasks to seed per pass. Default 1 — slow-drip
            so a new roadmap entry doesn't flood the queue.
        now: override for the current time (tests only).

    Returns a ``SeedReport``. No-op if engine_state.is_stopping(),
    planned count already at target, or no eligible fronts.
    """
    if engine_state.is_stopping():
        logger.info("seed: engine stopping, skipping pass")
        return SeedReport(planned_before=0, planned_after=0, seeded=0, eligible=0)

    now_dt = now if now is not None else datetime.now(tz=UTC)
    planned_before = _planned_count(conn)

    if planned_before >= target_planned_count:
        return SeedReport(
            planned_before=planned_before,
            planned_after=planned_before,
            seeded=0,
            eligible=0,
        )

    existing = _existing_identifiers(conn)
    eligible = _eligible_fronts(conn, existing, now=now_dt)

    slots = min(batch_size, target_planned_count - planned_before)
    to_seed = eligible[:slots]

    for row in to_seed:
        if engine_state.is_stopping():
            break
        _seed_one(conn, row)

    planned_after = _planned_count(conn)

    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (
                "seed_pass_complete",
                json.dumps({
                    "planned_before": planned_before,
                    "planned_after": planned_after,
                    "seeded": len(to_seed),
                    "eligible": len(eligible),
                    "target_planned_count": target_planned_count,
                }),
            ),
        )

    return SeedReport(
        planned_before=planned_before,
        planned_after=planned_after,
        seeded=len(to_seed),
        eligible=len(eligible),
    )


__all__ = ["SeedReport", "seed"]
