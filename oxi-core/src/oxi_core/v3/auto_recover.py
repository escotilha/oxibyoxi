"""auto_recover — retry rejected or CI-failed PR dispatches.

After a PR is rejected by the critic (``failure_reason='critic_rejected'``)
or fails hard (``failure_reason`` set, status ``'failed'``), this module
waits for a configurable cooldown window and then resets the task to
``'planned'`` so the engine's dispatch loop picks it up again.

When the task is re-seeded it receives a ``recovery_note`` field in its
brief context (via the ``subtitle`` annotation). This note tells the
next worker session *why* it is a retry and what the previous rejection
reason was — giving it a chance to do better.

Architecture decisions
----------------------
- ``run()`` is synchronous — same pattern as ``heartbeat``, ``auto_merge``,
  ``ship_recovery``.  The engine's tick loop calls it once per cycle.
- Cooldown is per-task, tracked by ``failed_at`` (already stamped by
  ``auto_merge`` and ``pr_watcher``).  No new DB columns are needed.
- Retry count is read from the ``event`` table: count of
  ``auto_recover_attempted`` events for that task.  When the count
  reaches ``max_retries`` the task is left in ``failed`` / ``abandoned``
  and an ``auto_recover_exhausted`` event is emitted.
- Dashboard labelling: ``auto_recover`` annotates recovered task titles
  (actually the subtitle field, so the column is preserved) with a
  ``[retry N]`` prefix.  ``dashboard.render_html`` already renders
  ``subtitle`` and the task table already shows ``status`` — no HTML
  changes are strictly needed, but this module ensures the badge is
  visible in the event stream and as a subtitle annotation.
- Engine-stop respected at entry (same discipline as every other loop).
- Every DB mutation is atomic with ``last_progress_at`` + ``updated_at``
  in one SQLite transaction (anti-pattern #1 invariant).

Trigger conditions
------------------
A task qualifies for recovery when **all** of the following hold:

1. ``status IN ('failed', 'abandoned')``
2. ``failure_reason IS NOT NULL``  — it was a definite failure, not a
   missing-reason gap.
3. ``failed_at IS NOT NULL``  — stamped by the failing transition; if
   it is missing we cannot compute the cooldown.
4. ``now - failed_at >= cooldown_minutes``
5. Retry count < ``max_retries``
6. The failure is a *recoverable* kind:
   - ``'critic_rejected'``  (critic sent REJECT)
   - ``'pr_closed_unmerged'`` (PR closed without merge — could be transient)
   - ``'stale_no_progress'``  (heartbeat reaped a stalled worker)
   Explicitly *excluded* (non-recoverable):
   - budget-stop reasons  (retrying would hit the same wall)
   - ``'abandoned'`` from a previous auto_recover (loop-guard; the
     ``auto_recover_exhausted`` event stops the cycle)

What this module does NOT do
-----------------------------
- Does not re-dispatch immediately.  It resets to ``'planned'`` and the
  normal dispatch loop picks it up on the next tick.  This keeps the
  dispatch loop as the single entry-point for worker spawning.
- Does not patch the git worktree or branch.  ``worktree_provision``
  in the dispatch loop will create a fresh worktree (or reuse if branch
  exists).
- Does not interact with GitHub.  PR cleanup (closing the old rejected
  PR) is the human operator's job — or a future module.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ._timefmt import now_iso as _now_iso
from .dispatch import Task
from .engine_state import EngineState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default cooldown between failure and retry (minutes).
DEFAULT_COOLDOWN_MINUTES: int = 30

#: Default maximum retry attempts before giving up.
DEFAULT_MAX_RETRIES: int = 3

#: Failure reasons that are *recoverable* (allow reset to planned).
RECOVERABLE_REASONS: frozenset[str] = frozenset(
    {
        "critic_rejected",
        "pr_closed_unmerged",
        "stale_no_progress",
    }
)

#: Recovery note injected into the task subtitle so the next session
#: knows this is a retry.  Uses a simple bracketed marker style
#: consistent with ``[retry N]`` prefixes shown in the dashboard.
_NOTE_TEMPLATE = "[retry {n}] Previous attempt failed: {reason}. {original_subtitle}"


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoverReport:
    """Summary of one auto_recover pass.

    Attributes:
        considered: tasks that passed the pre-filter
            (failed/abandoned + cooldown + recoverable reason).
        recovered: tasks successfully reset to ``'planned'``.
        exhausted: tasks that hit ``max_retries`` — left as-is with an
            ``auto_recover_exhausted`` event.
        skipped_cooldown: tasks still within the cooldown window.
        skipped_non_recoverable: tasks whose failure_reason is not in
            ``RECOVERABLE_REASONS``.
    """

    considered: int
    recovered: int
    exhausted: int
    skipped_cooldown: int
    skipped_non_recoverable: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _retry_count(conn: sqlite3.Connection, task_id: int) -> int:
    """Return the number of auto_recover_attempted events for this task."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM event "
        "WHERE task_id = ? AND kind = 'auto_recover_attempted'",
        (task_id,),
    ).fetchone()
    return int(row[0] if row else 0)


def _load_candidates(conn: sqlite3.Connection) -> list[Task]:
    """Return failed/abandoned tasks with a failure_reason and failed_at."""
    rows = conn.execute(
        "SELECT * FROM task "
        "WHERE status IN ('failed', 'abandoned') "
        "AND failure_reason IS NOT NULL "
        "AND failed_at IS NOT NULL "
        "ORDER BY failed_at ASC"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _build_recovery_subtitle(task: Task, retry_n: int) -> str:
    """Compose the new subtitle that embeds the retry note.

    Keeps the original subtitle accessible after the marker so the
    operator can still see what the task is about.
    """
    reason = task.failure_reason or "unknown"
    original = (task.subtitle or "").strip()
    # Strip any prior [retry N] prefix to avoid stacking.
    import re

    original = re.sub(r"^\[retry \d+\]\s*Previous attempt failed:[^.]+\.\s*", "", original)
    return _NOTE_TEMPLATE.format(n=retry_n, reason=reason, original_subtitle=original).strip()


def _reset_to_planned(
    conn: sqlite3.Connection,
    task: Task,
    retry_n: int,
) -> None:
    """Atomically reset a task to ``'planned'`` with a recovery note.

    Stamps ``last_progress_at`` + ``updated_at`` in the same transaction
    (anti-pattern #1 invariant).  Clears ``failed_at``, ``failure_reason``,
    and ``pr_number`` so the dispatch loop treats it as a fresh start.
    The recovery note is embedded in ``subtitle`` so it propagates into
    the dispatch prompt.
    """
    now = _now_iso()
    new_subtitle = _build_recovery_subtitle(task, retry_n)
    with conn:
        conn.execute(
            "UPDATE task SET "
            "status = 'planned', "
            "last_progress_at = ?, "
            "updated_at = ?, "
            "failed_at = NULL, "
            "failure_reason = NULL, "
            "pr_number = NULL, "
            "subtitle = ? "
            "WHERE id = ? AND status IN ('failed', 'abandoned')",
            (now, now, new_subtitle, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task.id,
                "auto_recover_attempted",
                json.dumps(
                    {
                        "retry_n": retry_n,
                        "prior_failure_reason": task.failure_reason,
                        "prior_pr_number": task.pr_number,
                        "prior_status": task.status,
                    }
                ),
            ),
        )


def _emit_exhausted(conn: sqlite3.Connection, task: Task, retry_n: int) -> None:
    """Record that this task has exhausted its retry budget."""
    now = _now_iso()
    with conn:
        conn.execute(
            "UPDATE task SET last_progress_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task.id,
                "auto_recover_exhausted",
                json.dumps(
                    {
                        "retry_n": retry_n,
                        "failure_reason": task.failure_reason,
                        "max_retries_reached": True,
                    }
                ),
            ),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    max_retries: int = DEFAULT_MAX_RETRIES,
    now: datetime | None = None,
) -> RecoverReport:
    """Run one auto-recovery pass.

    Arguments:
        conn: SQLite connection.
        engine_state: shared stopping flag.
        cooldown_minutes: minimum minutes between failure and retry.
            Defaults to 30. Tests pass a small value (e.g. 0) for speed.
        max_retries: maximum number of recovery attempts before the task
            is left permanently in its failed state.  Defaults to 3.
        now: override for current time — tests pass a fixed value so
            cooldown arithmetic is deterministic.

    Returns a ``RecoverReport``. Per-task failures are captured as
    ``auto_recover_exhausted`` events; no exceptions are raised for
    individual task errors.
    """
    empty = RecoverReport(
        considered=0,
        recovered=0,
        exhausted=0,
        skipped_cooldown=0,
        skipped_non_recoverable=0,
    )

    if engine_state.is_stopping():
        logger.info("auto_recover: engine stopping, skipping pass")
        return empty

    now_dt = now if now is not None else datetime.now(tz=UTC)
    cooldown = timedelta(minutes=cooldown_minutes)

    candidates = _load_candidates(conn)
    considered = 0
    recovered = 0
    exhausted = 0
    skipped_cooldown = 0
    skipped_non_recoverable = 0

    for task in candidates:
        if engine_state.is_stopping():
            break

        # --- Filter 1: recoverable failure reason ---
        reason = task.failure_reason or ""
        if reason not in RECOVERABLE_REASONS:
            skipped_non_recoverable += 1
            logger.debug(
                "auto_recover: %s — non-recoverable reason %r, skipping",
                task.identifier,
                reason,
            )
            continue

        # --- Filter 2: cooldown window ---
        failed_at = _parse_timestamp(task.failed_at)
        if failed_at is None:
            # Defensive: failed_at was NOT NULL in the query but parse failed.
            skipped_cooldown += 1
            continue
        if (now_dt - failed_at) < cooldown:
            skipped_cooldown += 1
            logger.debug(
                "auto_recover: %s — still within cooldown (%s remaining), skipping",
                task.identifier,
                (cooldown - (now_dt - failed_at)),
            )
            continue

        # --- Filter 3: retry budget ---
        considered += 1
        n_retries = _retry_count(conn, task.id)
        if n_retries >= max_retries:
            _emit_exhausted(conn, task, n_retries)
            exhausted += 1
            logger.info(
                "auto_recover: %s — exhausted (%d/%d retries), giving up",
                task.identifier,
                n_retries,
                max_retries,
            )
            continue

        # --- Reset to planned ---
        retry_n = n_retries + 1
        _reset_to_planned(conn, task, retry_n)
        recovered += 1
        logger.info(
            "auto_recover: %s — reset to planned (retry %d/%d, was %r)",
            task.identifier,
            retry_n,
            max_retries,
            reason,
        )

    return RecoverReport(
        considered=considered,
        recovered=recovered,
        exhausted=exhausted,
        skipped_cooldown=skipped_cooldown,
        skipped_non_recoverable=skipped_non_recoverable,
    )


__all__ = [
    "DEFAULT_COOLDOWN_MINUTES",
    "DEFAULT_MAX_RETRIES",
    "RECOVERABLE_REASONS",
    "RecoverReport",
    "run",
]
