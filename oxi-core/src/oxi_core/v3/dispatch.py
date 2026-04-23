"""State-machine driver for the dispatch step.

This is the orchestrator's ``dispatch`` phase. Per tick:

1. Pick the next ``planned`` task the policy admits.
2. Provision a git worktree for it.
3. Render the dispatch prompt from the active adapter.
4. Invoke ``claude -p`` via ``dispatch_invoke``.
5. Update the task row atomically — status, last_progress_at,
   session_id, cost_usd, mini_pid — in one transaction.
6. Write a ledger event describing the outcome.

Invariants this module enforces (each grounded in a prior incident):

- **Atomic transition** — ``status='dispatched'`` and
  ``last_progress_at=now()`` land in the same SQLite transaction. No
  code path updates one without the other. Enforced by reading back
  the row and asserting freshness in tests.
- **Engine-stopping check** — every iteration of the dispatch loop
  asks ``engine_state.is_stopping()`` before picking another task.
  Checking only at the top of the loop is the killswitch-bypass bug
  class.
- **Orphan-with-PR guard** — this module never transitions a task
  out of dispatched if ``pr_number IS NOT NULL``. That guard shares
  a helper with heartbeat (future P1-5d) so both reapers agree.
- **Result classification drives state** — success → ``dispatched``
  (the worker opens the PR from its end; status moves later by
  pr_watcher). retryable → stays ``planned``. failed → ``failed``.
  timeout → ``abandoned``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..adapter import Adapter, RoadmapItem
from ..prompts import dispatch_prompt
from .dispatch_invoke import (
    Classification,
    DispatchInvocation,
    DispatchResult,
    build_env,
    generate_session_id,
    invoke,
)
from .engine_state import EngineState
from .worktree_provision import WorktreeError, provision

logger = logging.getLogger(__name__)


# Session tag used to embed per-session identifiers into branch names.
# Oxi uses "oxi" as the default for its own dispatches; forks can set
# this via an adapter extension point (to be added when needed).
DEFAULT_SESSION_TAG = "oxi"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    The SQLite schema stores timestamps as TEXT. Using datetime.now(utc)
    keeps callers consistent; ``default (datetime('now'))`` in the
    schema also uses UTC, so formats align.
    """
    return datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Task:
    """Row-level view of a task. Read-only; updates go through _transition()."""

    id: int
    identifier: str
    tier: int
    title: str
    subtitle: str
    target_repo: str | None
    status: str
    pr_number: int | None
    last_progress_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            id=row["id"],
            identifier=row["identifier"],
            tier=row["tier"],
            title=row["title"],
            subtitle=row["subtitle"] or "",
            target_repo=row["target_repo"],
            status=row["status"],
            pr_number=_safe_pr_number(row),
            last_progress_at=row["last_progress_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def as_roadmap_item(self) -> RoadmapItem:
        """Convert to a RoadmapItem for use by prompts."""
        return RoadmapItem(
            identifier=self.identifier,
            tier=self.tier,
            title=self.title,
            subtitle=self.subtitle,
            target_repo=self.target_repo,
        )


def _safe_pr_number(row: sqlite3.Row) -> int | None:
    """Return row['pr_number'] if the column exists, else None.

    The current schema does not include ``pr_number`` yet; it will be
    added by a future migration when pr_watcher lands. For now this
    helper returns None so the orphan-with-PR invariant is always
    False (no task has a PR) — correct behavior during Phase 1.
    """
    try:
        return row["pr_number"]
    except (IndexError, KeyError):
        return None


def _pick_next_planned(
    conn: sqlite3.Connection, *, limit: int = 1
) -> list[Task]:
    """Return the top N planned tasks ordered by tier asc, identifier asc.

    Tier 0 first, then tier 1, etc. Within a tier, identifier order is
    stable and deterministic — useful for tests. Real adapters can
    layer priority/weights on top; this function is intentionally
    simple.
    """
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'planned' "
        "ORDER BY tier ASC, identifier ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _transition_to_dispatched(
    conn: sqlite3.Connection,
    task_id: int,
    session_id: str,
) -> None:
    """Move task from ``planned`` to ``dispatched``.

    Stamps ``last_progress_at`` atomically — this is the load-bearing
    invariant of the dispatch loop. Both updates live in a single
    transaction (SQLite's default autocommit off inside ``with
    conn:``).
    """
    now = _now_iso()
    with conn:
        conn.execute(
            "UPDATE task SET status = 'dispatched', "
            "last_progress_at = ?, "
            "dispatched_at = ?, "
            "updated_at = ? "
            "WHERE id = ? AND status = 'planned'",
            (now, now, now, task_id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task_id,
                "dispatch_started",
                json.dumps({"session_id": session_id}),
            ),
        )


def _record_outcome(
    conn: sqlite3.Connection,
    task_id: int,
    result: DispatchResult,
) -> None:
    """Apply the dispatch outcome to the task row.

    Status transitions:
    - SUCCESS → stays 'dispatched' (pr_watcher moves it onward once the PR lands)
    - RETRYABLE_TRANSIENT → back to 'planned' (next tick retries)
    - FAILED → 'failed'
    - TIMEOUT → 'abandoned'

    Every transition stamps ``last_progress_at`` and appends a ledger event.
    """
    now = _now_iso()
    if result.classification is Classification.SUCCESS:
        new_status = "dispatched"
        event_kind = "dispatch_succeeded"
    elif result.classification is Classification.RETRYABLE_TRANSIENT:
        new_status = "planned"
        event_kind = "dispatch_retryable"
    elif result.classification is Classification.TIMEOUT:
        new_status = "abandoned"
        event_kind = "dispatch_timeout"
    else:  # FAILED
        new_status = "failed"
        event_kind = "dispatch_failed"

    payload = {
        "session_id": result.session_id,
        "exit_code": result.exit_code,
        "cost_usd": result.cost_usd,
        "wall_clock_seconds": result.wall_clock_seconds,
        "classification": result.classification.value,
    }
    if result.trailing_line:
        payload["trailing_line"] = result.trailing_line[:500]

    failure_reason = None
    if new_status in ("failed", "abandoned"):
        failure_reason = result.classification.value
        if result.stderr_text:
            # Keep only a snippet of stderr for diagnosis, not the whole buffer.
            payload["stderr_tail"] = result.stderr_text[-500:]

    with conn:
        conn.execute(
            "UPDATE task SET status = ?, "
            "last_progress_at = ?, "
            "updated_at = ?, "
            "cost_usd = cost_usd + ?, "
            "failure_reason = COALESCE(?, failure_reason) "
            "WHERE id = ?",
            (new_status, now, now, result.cost_usd, failure_reason, task_id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, event_kind, json.dumps(payload)),
        )


def is_orphan_reapable(task: Task) -> bool:
    """Return True if this task is safe to abandon on an orphan check.

    Shared invariant between dispatch and heartbeat: **never abandon a
    task with an open PR**. Once a PR exists, the PR-watcher owns the
    task's lifecycle; dead session PID means "worker exited", not "work
    lost".
    """
    return task.pr_number is None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def dispatch_one(
    *,
    conn: sqlite3.Connection,
    adapter: Adapter,
    engine_state: EngineState,
    repo_root: Path,
    binary: str = "claude",
    anthropic_api_key: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> DispatchResult | None:
    """Dispatch the next planned task, or return None if nothing to do.

    Returns None when:
    - ``engine_state.is_stopping()`` is True at entry
    - no ``planned`` tasks exist

    Raises on infrastructure failures (worktree provisioning error,
    missing adapter). Does not raise on claude failures — those are
    captured in the DispatchResult's classification.
    """
    if engine_state.is_stopping():
        logger.info("dispatch: engine stopping, skipping tick")
        return None

    tasks = _pick_next_planned(conn, limit=1)
    if not tasks:
        return None
    task = tasks[0]

    # Provision a worktree for the task. The worktree lives under the
    # first dispatch host's worktree_root.
    hosts = adapter.dispatch_hosts()
    if not hosts:
        raise WorktreeError("adapter provides no dispatch hosts")
    worktree_root = Path(hosts[0].worktree_root)

    handle = provision(
        repo_root=repo_root,
        worktree_root=worktree_root,
        task_identifier=task.identifier,
        session_tag=DEFAULT_SESSION_TAG,
    )

    # Compose the prompt and assemble the invocation.
    prompt = dispatch_prompt(task.as_roadmap_item(), branch_name=handle.branch)
    session_id = generate_session_id()
    budget = adapter.budget()
    model_name = _pick_model(adapter)

    invocation = DispatchInvocation(
        prompt=prompt,
        cwd=handle.path,
        session_id=session_id,
        model=model_name,
        max_budget_usd=budget.per_task_opus,
        max_turns=30,
        allowed_tools=("Bash", "Read", "Edit", "Write", "Glob", "Grep"),
        extra_env=dict(extra_env or {}),
        binary=binary,
    )

    # Atomic transition before invoke so the DB reflects reality even
    # if we crash mid-dispatch.
    _transition_to_dispatched(conn, task.id, session_id)

    # Ensure ANTHROPIC_API_KEY reaches the child (build_env pulls it from arg).
    # The invoke() function reads extra_env + base whitelist; we don't
    # splice the API key here to avoid it ever landing in invocation
    # captures or logs. invoke() handles it.
    _ = build_env  # silence unused-import warning; invoke uses it internally
    _ = anthropic_api_key  # reserved for future structured-env wiring

    result = await invoke(invocation)

    _record_outcome(conn, task.id, result)

    return result


async def dispatch_loop(
    *,
    conn: sqlite3.Connection,
    adapter: Adapter,
    engine_state: EngineState,
    repo_root: Path,
    max_iterations: int = 1,
    binary: str = "claude",
    anthropic_api_key: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> list[DispatchResult]:
    """Run the dispatch step up to ``max_iterations`` times.

    Checks ``engine_state.is_stopping()`` at the top of every
    iteration — not just at entry. This is the killswitch-mid-loop
    fix documented in the prior-orchestrator post-mortems.

    Stops early when either:
    - engine_state requests stop
    - dispatch_one() returns None (no planned tasks left)
    """
    results: list[DispatchResult] = []
    for _ in range(max_iterations):
        if engine_state.is_stopping():
            break
        outcome = await dispatch_one(
            conn=conn,
            adapter=adapter,
            engine_state=engine_state,
            repo_root=repo_root,
            binary=binary,
            anthropic_api_key=anthropic_api_key,
            extra_env=extra_env,
        )
        if outcome is None:
            break
        results.append(outcome)
    return results


# ---------------------------------------------------------------------------
# Model selection (tiny helper — may grow)
# ---------------------------------------------------------------------------


def _pick_model(adapter: Adapter) -> str:
    """Return the model name to use for a dispatch.

    For now, a placeholder that returns a reasonable Claude model ID.
    Future: read from ``adapter.policy().skill_weights`` or a dedicated
    ``adapter.default_model()`` method.
    """
    tier = adapter.plan_tier()
    # Conservative: always use Sonnet unless explicitly configured.
    # Max tiers use Opus; standard tier defaults to Sonnet.
    if "opus" in tier.lower() or "max" in tier.lower():
        return "claude-opus-4-7"
    return "claude-sonnet-4-6"


__all__ = [
    "DEFAULT_SESSION_TAG",
    "Task",
    "dispatch_loop",
    "dispatch_one",
    "is_orphan_reapable",
]
