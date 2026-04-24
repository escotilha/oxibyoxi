"""deep_fix — escalation for repeatedly-stuck tasks.

When a task has failed or been repeatedly rejected, the normal retry
loop (``dispatch_one`` picks it up again as ``planned``) will produce
the same result unless *something* changes.  ``deep_fix`` is that
something.

On each pass, ``deep_fix.run``:

1. Finds tasks in ``failed`` status.
2. Counts how many times each has already failed — by counting
   ``dispatch_failed``, ``dispatch_timeout``, and
   ``auto_merge_rejected`` events in the ledger.
3. If the count is **below** the configured threshold *N*, the task
   is left alone — ``dispatch`` will retry it on the next tick with
   the same parameters.
4. If the count **meets** the threshold, and a cooldown window has
   elapsed since the most recent failure event, the escalation recipe
   is applied:

   a. The task is reset to ``planned``.
   b. A ``deep_fix_escalated`` ledger event is written that records
      the chosen model override and any prompt loosening notes.
   c. On the **next** dispatch the engine reads the most recent
      ``deep_fix_escalated`` event for the task and:
      - Overrides the model (e.g. Sonnet → Opus).
      - Prepends a context note to the prompt so the worker knows this
        is an escalated attempt and why previous attempts stalled.

The escalation model and parameters are adapter-configurable via
:class:`EscalationConfig`.  Forks set ``failure_threshold=N``,
``cooldown_minutes=M``, and the ordered ``recipes`` list.  Each recipe
is a :class:`EscalationRecipe` that specifies the model override and an
optional prompt note.  Recipes are consumed in order: the first attempt
uses ``recipes[0]``, the second uses ``recipes[1]``, etc.  Once the
last recipe is exhausted, subsequent failures are logged as
``deep_fix_exhausted`` and the task is left in ``failed`` state — a
human must intervene.

Invariants shared with the rest of the engine:

- Killswitch respected: ``engine_state.is_stopping()`` checked at entry.
- Every state transition stamps ``last_progress_at`` + ``updated_at``
  atomically.
- No adapter dependency at import time — adapter is read inside ``run``.
- No subprocess or network calls — pure DB + event work.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..adapter import get_active_adapter
from ._timefmt import now_iso as _now_iso
from .dispatch import Task
from .engine_state import EngineState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config dataclasses (public; adapters reference these)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationRecipe:
    """One escalation step applied to a stuck task.

    Attributes:
        model: Claude model string to use for this attempt.  If None,
            the adapter's default model is used unchanged.
        prompt_note: Optional text prepended to the dispatch prompt as
            context for the worker.  Operators use this to loosen
            constraints, add hints, or reference prior failure reasons.
        label: Human-readable label for dashboards and ledger events.
    """

    model: str | None = None
    prompt_note: str = ""
    label: str = "escalated"


@dataclass(frozen=True)
class EscalationConfig:
    """Adapter-configurable deep_fix policy.

    Attributes:
        failure_threshold: minimum number of failure events before an
            escalation attempt is made.  The *first* time the count
            reaches this value, ``recipes[0]`` is applied.  Each
            subsequent escalation uses the next recipe in the list.
        cooldown_minutes: minimum elapsed time since the last failure
            event before an escalation may fire.  Prevents back-to-back
            re-dispatches when a task fails fast.
        recipes: ordered list of escalation steps.  Must be non-empty
            for escalation to fire.  If a task has exhausted all recipes
            it is left in ``failed`` with a ``deep_fix_exhausted`` event.
        enabled: global toggle.  ``False`` disables the module entirely,
            matching the pattern in other opt-in subsystems (auto_merge,
            etc.).
    """

    failure_threshold: int = 3
    cooldown_minutes: int = 30
    recipes: tuple[EscalationRecipe, ...] = field(
        default_factory=lambda: (
            EscalationRecipe(
                model="claude-opus-4-5",
                prompt_note=(
                    "This is an escalated dispatch. Previous attempts with "
                    "the default model failed or were rejected. You have "
                    "more token budget and the full conversation context. "
                    "Study the prior failure reasons in the ledger events "
                    "included below and address them explicitly."
                ),
                label="opus-escalation",
            ),
        )
    )
    enabled: bool = True


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeepFixReport:
    """Summary of one deep_fix pass.

    Attributes:
        considered: failed tasks examined.
        escalated: tasks reset to ``planned`` with a new recipe.
        cooldown_skipped: tasks that met the failure threshold but are
            still within the cooldown window.
        exhausted: tasks that have used up every recipe and are left in
            ``failed`` state — human intervention required.
        below_threshold: tasks whose failure count is below the
            threshold (normal retry, no escalation needed).
        disabled_skipped: tasks skipped because ``enabled=False``.
    """

    considered: int
    escalated: int
    cooldown_skipped: int
    exhausted: int
    below_threshold: int
    disabled_skipped: int


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_FAILURE_KINDS: frozenset[str] = frozenset({
    "dispatch_failed",
    "dispatch_timeout",
    "auto_merge_rejected",
})

_ESCALATION_KIND = "deep_fix_escalated"
_EXHAUSTED_KIND = "deep_fix_exhausted"


def _load_failed_tasks(conn: sqlite3.Connection) -> list[Task]:
    """Return all tasks in ``failed`` status, ordered by id."""
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'failed' ORDER BY id ASC"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _count_failures(conn: sqlite3.Connection, task_id: int) -> int:
    """Count the number of failure events for this task in the ledger."""
    placeholders = ",".join(f"'{k}'" for k in sorted(_FAILURE_KINDS))
    row = conn.execute(
        f"SELECT COUNT(*) FROM event WHERE task_id = ? AND kind IN ({placeholders})",
        (task_id,),
    ).fetchone()
    return int(row[0] or 0)


def _count_escalations(conn: sqlite3.Connection, task_id: int) -> int:
    """Count how many escalation events have already fired for this task."""
    row = conn.execute(
        "SELECT COUNT(*) FROM event WHERE task_id = ? AND kind = ?",
        (task_id, _ESCALATION_KIND),
    ).fetchone()
    return int(row[0] or 0)


def _last_failure_at(conn: sqlite3.Connection, task_id: int) -> datetime | None:
    """Return the ``created_at`` of the most recent failure event, or None."""
    placeholders = ",".join(f"'{k}'" for k in sorted(_FAILURE_KINDS))
    row = conn.execute(
        f"SELECT MAX(created_at) FROM event "
        f"WHERE task_id = ? AND kind IN ({placeholders})",
        (task_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _last_failure_reasons(
    conn: sqlite3.Connection, task_id: int, *, limit: int = 3
) -> list[str]:
    """Return the ``reason`` or ``classification`` field from the most recent
    failure events.  Used to build the escalation context note.
    """
    placeholders = ",".join(f"'{k}'" for k in sorted(_FAILURE_KINDS))
    rows = conn.execute(
        f"SELECT payload FROM event "
        f"WHERE task_id = ? AND kind IN ({placeholders}) "
        f"ORDER BY id DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    reasons: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            payload = {}
        reason = (
            payload.get("detail")
            or payload.get("reason")
            or payload.get("classification")
            or ""
        )
        if reason:
            reasons.append(str(reason)[:200])
    return reasons


def _emit_escalated(
    conn: sqlite3.Connection,
    task: Task,
    recipe: EscalationRecipe,
    failure_count: int,
    escalation_index: int,
    prior_reasons: list[str],
) -> None:
    now = _now_iso()
    payload = {
        "failure_count": failure_count,
        "escalation_index": escalation_index,
        "recipe_label": recipe.label,
        "model_override": recipe.model,
        "prompt_note": recipe.prompt_note[:500] if recipe.prompt_note else "",
        "prior_failure_reasons": prior_reasons,
    }
    with conn:
        conn.execute(
            "UPDATE task SET status = 'planned', "
            "last_progress_at = ?, updated_at = ?, "
            "failure_reason = NULL "
            "WHERE id = ?",
            (now, now, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task.id, _ESCALATION_KIND, json.dumps(payload)),
        )


def _emit_exhausted(
    conn: sqlite3.Connection,
    task: Task,
    failure_count: int,
    escalation_count: int,
) -> None:
    """Record that no recipes remain; task stays in ``failed``."""
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task.id,
                _EXHAUSTED_KIND,
                json.dumps({
                    "failure_count": failure_count,
                    "escalation_count": escalation_count,
                    "note": "all escalation recipes exhausted; human intervention required",
                }),
            ),
        )


def _exhausted_event_exists(conn: sqlite3.Connection, task_id: int) -> bool:
    """Return True if a ``deep_fix_exhausted`` event already exists for this
    task.  Used to suppress repeated exhausted events across ticks.
    """
    row = conn.execute(
        "SELECT 1 FROM event WHERE task_id = ? AND kind = ? LIMIT 1",
        (task_id, _EXHAUSTED_KIND),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Public API — read escalation override for dispatch
# ---------------------------------------------------------------------------


def get_escalation_override(
    conn: sqlite3.Connection, task_id: int
) -> EscalationRecipe | None:
    """Return the most recent escalation recipe for a task, or None.

    Called by ``dispatch.dispatch_one`` (and tests) to check whether the
    *next* dispatch should use a different model / prompt note.

    Returns the most recent ``deep_fix_escalated`` event's recipe fields
    unmarshalled into an :class:`EscalationRecipe`, or ``None`` if no
    escalation event exists.

    The dispatch code is free to ignore the return value — ``deep_fix``
    never forces a dispatch, it only annotates the task.
    """
    row = conn.execute(
        "SELECT payload FROM event WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, _ESCALATION_KIND),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return None
    return EscalationRecipe(
        model=payload.get("model_override"),
        prompt_note=payload.get("prompt_note", ""),
        label=payload.get("recipe_label", "escalated"),
    )


# ---------------------------------------------------------------------------
# Public API — run one pass
# ---------------------------------------------------------------------------


def run(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    config: EscalationConfig | None = None,
    now: datetime | None = None,
) -> DeepFixReport:
    """Run one deep_fix pass.

    Arguments:
        conn: SQLite connection.
        engine_state: shared stopping flag.
        config: escalation policy.  If None, reads from the active
            adapter via ``adapter.escalation_config()`` if that method
            exists; otherwise falls back to ``EscalationConfig()``
            defaults.
        now: wall-clock override for tests.  Defaults to UTC now.

    Returns a :class:`DeepFixReport`.  No exceptions for per-task
    failures — they're captured as events.
    """
    empty = DeepFixReport(
        considered=0, escalated=0, cooldown_skipped=0,
        exhausted=0, below_threshold=0, disabled_skipped=0,
    )

    if engine_state.is_stopping():
        logger.info("deep_fix: engine stopping, skipping pass")
        return empty

    if config is None:
        adapter = get_active_adapter()
        if hasattr(adapter, "escalation_config"):
            config = adapter.escalation_config()
        else:
            config = EscalationConfig()

    if not config.enabled:
        logger.info("deep_fix: disabled via EscalationConfig.enabled=False")
        failed_count = conn.execute(
            "SELECT COUNT(*) FROM task WHERE status = 'failed'"
        ).fetchone()[0]
        return DeepFixReport(
            considered=0, escalated=0, cooldown_skipped=0,
            exhausted=0, below_threshold=0,
            disabled_skipped=int(failed_count),
        )

    if not config.recipes:
        logger.info("deep_fix: no recipes configured, skipping pass")
        return empty

    now_dt = now if now is not None else datetime.now(tz=UTC)
    cooldown = timedelta(minutes=config.cooldown_minutes)

    tasks = _load_failed_tasks(conn)

    considered = 0
    escalated = 0
    cooldown_skipped = 0
    exhausted = 0
    below_threshold = 0

    for task in tasks:
        if engine_state.is_stopping():
            break

        considered += 1
        failure_count = _count_failures(conn, task.id)

        if failure_count < config.failure_threshold:
            below_threshold += 1
            logger.debug(
                "deep_fix: task %s has %d failures (threshold %d); skipping",
                task.identifier, failure_count, config.failure_threshold,
            )
            continue

        # Count how many escalation recipes have already been consumed.
        escalation_count = _count_escalations(conn, task.id)

        if escalation_count >= len(config.recipes):
            # All recipes exhausted.
            if not _exhausted_event_exists(conn, task.id):
                _emit_exhausted(conn, task, failure_count, escalation_count)
                logger.warning(
                    "deep_fix: task %s has exhausted all %d escalation recipes "
                    "(%d failures); human intervention required",
                    task.identifier, len(config.recipes), failure_count,
                )
            exhausted += 1
            continue

        # Cooldown check — don't re-dispatch if the task just failed.
        last_fail = _last_failure_at(conn, task.id)
        if last_fail is not None:
            elapsed = now_dt - last_fail
            if elapsed < cooldown:
                remaining = int((cooldown - elapsed).total_seconds() / 60)
                logger.debug(
                    "deep_fix: task %s in cooldown (%d min remaining)",
                    task.identifier, remaining,
                )
                cooldown_skipped += 1
                continue

        # Apply the next recipe.
        recipe = config.recipes[escalation_count]
        prior_reasons = _last_failure_reasons(conn, task.id)

        logger.info(
            "deep_fix: escalating task %s (failure_count=%d, "
            "escalation_index=%d, model=%s, label=%s)",
            task.identifier, failure_count, escalation_count,
            recipe.model or "<adapter default>", recipe.label,
        )

        _emit_escalated(
            conn, task, recipe,
            failure_count=failure_count,
            escalation_index=escalation_count,
            prior_reasons=prior_reasons,
        )
        escalated += 1

    return DeepFixReport(
        considered=considered,
        escalated=escalated,
        cooldown_skipped=cooldown_skipped,
        exhausted=exhausted,
        below_threshold=below_threshold,
        disabled_skipped=0,
    )


__all__ = [
    "DeepFixReport",
    "EscalationConfig",
    "EscalationRecipe",
    "get_escalation_override",
    "run",
]
