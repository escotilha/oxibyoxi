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

Stuck-task triage (optional, behind feature flag)
--------------------------------------------------
When ``HeartbeatConfig.triage_enabled`` is ``True`` **and** an
``InferenceGateway`` (or compatible fake) is provided to ``reap()``,
the reaper sends a short prompt to the LLM asking it to summarise why
the task appears stuck.  The summary is stored in the
``triage_summary`` field of the ``abandoned_by_heartbeat`` ledger
event payload.

When ``triage_enabled`` is ``False`` (the default), the code path is
never entered and behaviour is byte-identical to the pre-T2-38
implementation.  No extra imports, no network calls, no surprises.

Model selection follows ``routing.yaml`` → role ``"heartbeat-triage"``
(default ``claude-haiku-4-5-20251001``).  The gateway is injected by
the caller (CLI / tests) rather than constructed inside the module, so
tests use ``FakeInferenceGateway`` without touching any network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .dispatch import Task, is_orphan_reapable
from .engine_state import EngineState

if TYPE_CHECKING:
    from .inference import InferenceGateway

logger = logging.getLogger(__name__)

# Grace period default. Can be overridden via ``reap()``'s argument so
# tests can exercise the logic with realistic-looking values (10 min)
# without waiting 10 minutes. Production should use something closer
# to 30 minutes, tuned to the longest reasonable dispatch duration.
DEFAULT_STALE_AFTER_SECONDS = 10 * 60  # 10 minutes

#: Role key used in routing.yaml for heartbeat triage calls.
_TRIAGE_ROLE = "heartbeat-triage"

#: Default model used when the routing table cannot be loaded.
_TRIAGE_MODEL_FALLBACK = "claude-haiku-4-5-20251001"

#: Maximum tokens to request for the triage summary.
_TRIAGE_MAX_TOKENS = 256


# ---------------------------------------------------------------------------
# Config dataclass (public; adapters reference this)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeartbeatConfig:
    """Adapter-configurable heartbeat policy.

    Adapters may implement a ``heartbeat_config()`` method that returns
    an instance of this class.  The method is intentionally **NOT** part
    of the ``Adapter`` protocol so existing adapters written before T2-38
    continue to satisfy the protocol without changes.

    Example adapter usage::

        def heartbeat_config(self) -> HeartbeatConfig:
            return HeartbeatConfig(triage_enabled=True)

    Attributes:
        triage_enabled: When ``True`` and an ``InferenceGateway`` is
            supplied to ``reap()``, the reaper calls the LLM to produce
            a short summary of why the task appears stuck.  The summary
            is stored in ``abandoned_by_heartbeat`` event payloads under
            the key ``"triage_summary"``.  Default ``False`` — opt-in.
    """

    triage_enabled: bool = False


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


def _load_recent_events(conn: sqlite3.Connection, task_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent ledger events for *task_id* as plain dicts.

    Used to build triage context.  Returns an empty list when no events
    exist (e.g. a brand-new task that was dispatched and immediately
    went stale without any intermediate events).
    """
    rows = conn.execute(
        "SELECT kind, payload, created_at FROM event "
        "WHERE task_id = ? ORDER BY id DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] if isinstance(row, sqlite3.Row) else row[1])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        result.append({
            "kind": row["kind"] if isinstance(row, sqlite3.Row) else row[0],
            "payload": payload,
            "created_at": row["created_at"] if isinstance(row, sqlite3.Row) else row[2],
        })
    return result


def _build_triage_messages(task: Task, recent_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the messages list for the triage LLM call.

    Produces a minimal prompt that gives the model enough context to
    summarise why the task is stuck, without leaking excessive data or
    requiring expensive context windows.
    """
    events_text = ""
    if recent_events:
        lines = []
        for ev in recent_events:
            lines.append(f"- [{ev['created_at']}] {ev['kind']}: {json.dumps(ev['payload'])}")
        events_text = "\n".join(lines)
    else:
        events_text = "(no ledger events found)"

    prompt = (
        f"A software task has been stuck in 'dispatched' state without progress "
        f"and is being abandoned by the heartbeat reaper.\n\n"
        f"Task identifier: {task.identifier}\n"
        f"Task title: {task.title}\n"
        f"Task subtitle: {task.subtitle or '(none)'}\n"
        f"Last progress at: {task.last_progress_at or 'never'}\n"
        f"Updated at: {task.updated_at or 'unknown'}\n\n"
        f"Most recent ledger events (newest first):\n{events_text}\n\n"
        f"In 1-3 sentences, summarise the most likely reason this task got stuck "
        f"and suggest what an operator should check. Be concise and specific."
    )
    return [{"role": "user", "content": prompt}]


def _resolve_triage_model() -> str:
    """Return the model ID from routing.yaml for the heartbeat-triage role.

    Falls back to ``_TRIAGE_MODEL_FALLBACK`` if the routing table cannot
    be loaded (missing YAML, test environment without the file, etc.)
    so triage is never a hard dependency.
    """
    try:
        from .routing import RoleNotConfiguredError, RoutingConfigError, route_for
        choice = route_for(_TRIAGE_ROLE)
        return choice.model_id
    except (RoleNotConfiguredError, RoutingConfigError, Exception):
        logger.warning(
            "heartbeat.triage: could not resolve model from routing table; "
            "falling back to %s",
            _TRIAGE_MODEL_FALLBACK,
        )
        return _TRIAGE_MODEL_FALLBACK


async def _triage_task(
    task: Task,
    conn: sqlite3.Connection,
    gateway: "InferenceGateway",
) -> str:
    """Call the LLM to produce a triage summary for the stuck task.

    Returns the summary text, or an empty string on any error.
    Errors are logged at WARNING level — triage is best-effort and
    must never block the abandonment itself.
    """
    try:
        recent_events = _load_recent_events(conn, task.id)
        messages = _build_triage_messages(task, recent_events)
        model = _resolve_triage_model()
        result = await gateway.complete(
            messages=messages,
            model=model,
            max_tokens=_TRIAGE_MAX_TOKENS,
        )
        logger.info(
            "heartbeat.triage: summarised stuck task %s (%.4f USD, model=%s)",
            task.identifier,
            result.cost_usd,
            result.model,
        )
        return result.text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "heartbeat.triage: failed for %s (%s: %s); proceeding without summary",
            task.identifier,
            type(exc).__name__,
            exc,
        )
        return ""


def _abandon(
    conn: sqlite3.Connection,
    task: Task,
    reason: str,
    triage_summary: str = "",
) -> None:
    """Transition a stuck task to ``abandoned``. Atomic with ledger write.

    Arguments:
        conn: database connection.
        task: the stuck task to abandon.
        reason: short reason code stored in ``failure_reason``.
        triage_summary: optional LLM-generated summary written into the
            ``abandoned_by_heartbeat`` event payload under
            ``"triage_summary"``.  Empty string when triage is disabled.
    """
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    payload: dict[str, Any] = {"reason": reason, "identifier": task.identifier}
    if triage_summary:
        payload["triage_summary"] = triage_summary

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
                json.dumps(payload),
            ),
        )


def reap(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
    gateway: "InferenceGateway | None" = None,
    config: HeartbeatConfig | None = None,
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
        gateway: optional ``InferenceGateway`` (or ``FakeInferenceGateway``
            in tests) used for stuck-task triage summaries.  Ignored when
            ``config.triage_enabled`` is ``False`` (the default).  When
            ``config.triage_enabled`` is ``True`` but ``gateway`` is
            ``None``, triage is silently skipped.
        config: optional ``HeartbeatConfig`` controlling feature flags.
            When ``None``, the module attempts to read ``heartbeat_config()``
            from the active adapter (if available), then falls back to
            ``HeartbeatConfig()`` defaults.

    Safety properties (tested in test_heartbeat.py):

    - Rows with \`pr_number IS NOT NULL\` are never abandoned, even if
      their freshness is older than the grace window. They belong to
      pr_watcher.
    - ``last_progress_at`` is preferred over ``updated_at``. Only rows
      with NEITHER timestamp are treated as stale by default.
    - Every abandonment writes a ledger event. No silent state flips.
    - When ``triage_enabled=False`` (default), behaviour is
      byte-identical to the pre-T2-38 implementation.
    """
    if engine_state.is_stopping():
        logger.info("heartbeat: engine stopping, skipping reap")
        return ReapReport(
            considered=0, abandoned=0, protected_by_pr=0, skipped_fresh=0
        )

    # Resolve config: explicit argument > adapter method > defaults.
    effective_config = _resolve_config(config)

    now_dt = now if now is not None else datetime.now(tz=UTC)
    stale_after = timedelta(seconds=stale_after_seconds)
    tasks = _load_dispatched_tasks(conn)

    abandoned = 0
    protected = 0
    fresh = 0

    triage_active = effective_config.triage_enabled and gateway is not None

    for task in tasks:
        # Anti-pattern #4: never abandon a task with a PR open.
        if not is_orphan_reapable(task):
            protected += 1
            continue
        if not _is_stale(task, now_dt, stale_after):
            fresh += 1
            continue

        # --- Triage (optional) -------------------------------------------
        triage_summary = ""
        if triage_active:
            # Run the async triage call.  ``reap()`` is synchronous;
            # we detect whether we're already inside a running event loop
            # (e.g. pytest-asyncio test) and use a thread to avoid
            # deadlock.  In the common production case there is no running
            # loop, so ``asyncio.run()`` creates a fresh one.
            try:
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    running_loop = None

                if running_loop is not None and running_loop.is_running():
                    # Inside an async context — spawn a thread to avoid
                    # deadlocking the caller's loop.
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(
                            asyncio.run, _triage_task(task, conn, gateway)
                        )
                        triage_summary = future.result()
                else:
                    triage_summary = asyncio.run(
                        _triage_task(task, conn, gateway)
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "heartbeat.triage: event loop error for %s (%s); skipping",
                    task.identifier,
                    exc,
                )
                triage_summary = ""

        _abandon(task=task, conn=conn, reason="stale_no_progress", triage_summary=triage_summary)
        abandoned += 1

    return ReapReport(
        considered=len(tasks),
        abandoned=abandoned,
        protected_by_pr=protected,
        skipped_fresh=fresh,
    )


def _resolve_config(config: HeartbeatConfig | None) -> HeartbeatConfig:
    """Return the effective ``HeartbeatConfig``.

    Resolution order:
    1. Explicit argument (takes precedence over everything).
    2. ``adapter.heartbeat_config()`` if the method exists.
    3. ``HeartbeatConfig()`` defaults (triage_enabled=False).
    """
    if config is not None:
        return config
    try:
        from ..adapter import get_active_adapter
        adapter = get_active_adapter()
        if hasattr(adapter, "heartbeat_config"):
            return adapter.heartbeat_config()
    except Exception:  # noqa: BLE001
        pass
    return HeartbeatConfig()


__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "HeartbeatConfig",
    "ReapReport",
    "reap",
]
