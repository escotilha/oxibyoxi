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
- **False-failure relaxation** — when claude exits non-zero but the
  expected branch is ahead of ``origin/<default>`` AND a PR exists,
  the exit code most likely came from a pre-commit hook tail or a
  ``gh pr create`` post-success quirk.  In that case the result is
  upgraded to SUCCESS so the task stays ``dispatched`` and pr_watcher
  picks it up normally.  Dogfood evidence: 4-of-4 dispatches in
  session 2026-04-24 false-failed this way (T0-11, T1-12, T1-13).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..adapter import Adapter, RoadmapItem
from ..prompts import dispatch_prompt
from ._timefmt import now_iso as _now_iso
from .dispatch_invoke import (
    Classification,
    DispatchInvocation,
    DispatchResult,
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
# False-failure relaxation helpers (T1-15)
# ---------------------------------------------------------------------------


def _branch_has_commits_ahead(worktree_path: Path, branch: str) -> bool:
    """Return True if ``branch`` has at least one commit not in ``origin/main``.

    Uses ``git rev-list --count origin/HEAD...<branch>`` so it works
    regardless of whether the default branch is called ``main`` or
    ``master``.  Falls back to ``origin/main`` if ``origin/HEAD`` is
    not configured.

    A non-zero count means the worker pushed at least one commit, which
    is strong evidence that the substantive work landed even if claude
    subsequently exited non-zero.

    Returns False on any subprocess error so the caller degrades
    gracefully (keeps FAILED classification) rather than crashing.
    """
    # Try origin/HEAD first (adapter-agnostic), fall back to origin/main.
    for remote_ref in ("origin/HEAD", "origin/main"):
        try:
            proc = subprocess.run(
                ["git", "rev-list", "--count", f"{remote_ref}...{branch}"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                count_str = proc.stdout.strip()
                return count_str.isdigit() and int(count_str) > 0
        except (FileNotFoundError, OSError):
            return False
    return False


def _pr_exists_for_branch(branch: str, repo: str) -> bool:
    """Return True if at least one open PR targets ``branch`` in ``repo``.

    Shells to ``gh pr list`` — consistent with ``GhCliClient`` and
    ``ship_recovery``.  Returns False on any error so the caller
    degrades gracefully.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--head", branch,
                "--state", "open",
                "--json", "number",
                "--limit", "1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return False
        import json as _json
        items = _json.loads(proc.stdout.strip() or "[]")
        return bool(items)
    except (FileNotFoundError, OSError, ValueError):
        return False


def _maybe_upgrade_to_success(
    result: DispatchResult,
    worktree_path: Path,
    branch: str,
    repo: str,
    *,
    # Injectable for tests — default to production implementations.
    _check_commits: Callable[[Path, str], bool] = _branch_has_commits_ahead,
    _check_pr: Callable[[str, str], bool] = _pr_exists_for_branch,
) -> DispatchResult:
    """Upgrade a FAILED result to SUCCESS when the work clearly landed.

    Condition: classification is FAILED **and** the branch has commits
    ahead of origin **and** a PR already exists.  Both checks must pass
    to avoid promoting genuine failures (no commits = worker never
    shipped; no PR = git push may have failed too).

    Returns a new ``DispatchResult`` with classification overridden to
    ``SUCCESS`` and a note in the trailing_line field; all other fields
    are unchanged.
    """
    if result.classification is not Classification.FAILED:
        return result

    has_commits = _check_commits(worktree_path, branch)
    has_pr = _check_pr(branch, repo)

    if not (has_commits and has_pr):
        return result

    logger.info(
        "dispatch: upgrading FAILED→SUCCESS for branch %s "
        "(branch ahead of origin, PR exists; likely post-success exit quirk)",
        branch,
    )

    note = (
        f"[oxi] exit-code non-zero but branch {branch!r} is ahead of origin "
        f"and a PR exists — reclassified as SUCCESS"
    )
    return DispatchResult(
        classification=Classification.SUCCESS,
        exit_code=result.exit_code,
        session_id=result.session_id,
        events=result.events,
        trailing_line=note,
        stderr_text=result.stderr_text,
        cost_usd=result.cost_usd,
        wall_clock_seconds=result.wall_clock_seconds,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


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
    # Optional columns — present in the schema but not always populated.
    failed_at: str | None = None
    failure_reason: str | None = None

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
            failed_at=_safe_column(row, "failed_at"),
            failure_reason=_safe_column(row, "failure_reason"),
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

    As of migration v2, the ``pr_number`` column exists on the ``task``
    table. Rows pre-dating the migration have NULL. The defensive
    try/except also covers adapter-provided DBs that haven't run
    migrations yet — calling modules should not need to care.
    """
    try:
        return row["pr_number"]
    except (IndexError, KeyError):
        return None


def _safe_column(row: sqlite3.Row, column: str) -> str | None:
    """Return ``row[column]`` if the column exists, else None.

    Mirrors ``_safe_pr_number`` for other optional columns that may be
    absent in older schema versions or adapter-provided DBs.
    """
    try:
        return row[column]
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
    # Injectable for tests: override the two checks inside
    # _maybe_upgrade_to_success without needing a real git remote or gh CLI.
    _check_commits: Callable[[Path, str], bool] | None = None,
    _check_pr: Callable[[str, str], bool] | None = None,
) -> DispatchResult | None:
    """Dispatch the next planned task, or return None if nothing to do.

    Returns None when:
    - ``engine_state.is_stopping()`` is True at entry
    - no ``planned`` tasks exist

    Raises on infrastructure failures (worktree provisioning error,
    missing adapter). Does not raise on claude failures — those are
    captured in the DispatchResult's classification.

    The ``_check_commits`` and ``_check_pr`` parameters are for testing
    only; production callers should leave them as None (defaults apply).
    """
    if engine_state.is_stopping():
        logger.info("dispatch: engine stopping, skipping tick")
        return None

    # Budget gate — refuse to spend more if the hard cap is hit.
    from . import budget as budget_mod
    if budget_mod.is_hard_stopped(conn):
        logger.info("dispatch: budget hard-stop reached, skipping tick")
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

    # Check for a deep_fix escalation override for this task.
    # ``deep_fix.run`` resets failed tasks to ``planned`` and writes a
    # ``deep_fix_escalated`` event with a model override and prompt note.
    # We read that here and adjust the invocation accordingly.
    from . import deep_fix as deep_fix_mod
    escalation = deep_fix_mod.get_escalation_override(conn, task.id)

    # Compose the prompt and assemble the invocation.
    base_prompt = dispatch_prompt(task.as_roadmap_item(), branch_name=handle.branch)
    if escalation and escalation.prompt_note:
        prompt = f"{escalation.prompt_note}\n\n{base_prompt}"
    else:
        prompt = base_prompt

    session_id = generate_session_id()
    budget = adapter.budget()
    if escalation and escalation.model:
        model_name = escalation.model
    else:
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
        anthropic_api_key=anthropic_api_key,
        binary=binary,
    )

    # Atomic transition before invoke so the DB reflects reality even
    # if we crash mid-dispatch.
    _transition_to_dispatched(conn, task.id, session_id)

    result = await invoke(invocation)

    # False-failure relaxation (T1-15): if claude exited non-zero but
    # the branch already has commits ahead of origin AND a PR exists,
    # the exit most likely came from a post-success hook or a
    # gh-pr-create quirk. Upgrade to SUCCESS so pr_watcher takes over.
    upgrade_kwargs: dict = {}
    if _check_commits is not None:
        upgrade_kwargs["_check_commits"] = _check_commits
    if _check_pr is not None:
        upgrade_kwargs["_check_pr"] = _check_pr
    repo = adapter.github_repo()
    result = _maybe_upgrade_to_success(
        result, handle.path, handle.branch, repo, **upgrade_kwargs
    )

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
    "_branch_has_commits_ahead",
    "_maybe_upgrade_to_success",
    "_pr_exists_for_branch",
    "dispatch_loop",
    "dispatch_one",
    "is_orphan_reapable",
]
