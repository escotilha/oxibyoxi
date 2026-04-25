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
import os
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
from .engine_health import EngineHealth
from .engine_state import EngineState
from .ledger_events import LedgerEvent
from .pr_overlap import OverlapChecker, OverlapReport, check_overlap
from .worktree_provision import WorktreeError, provision

logger = logging.getLogger(__name__)


# Session tag used to embed per-session identifiers into branch names.
# Oxi uses "oxi" as the default for its own dispatches; forks can set
# this via an adapter extension point (to be added when needed).
DEFAULT_SESSION_TAG = "oxi"

# ---------------------------------------------------------------------------
# Repeat-failure escalation constants
# ---------------------------------------------------------------------------

#: Number of consecutive terminal failures before decompose-first mode kicks in.
DECOMPOSE_FIRST_FAILURE_THRESHOLD = 3

#: max_turns used for decompose-first escalated dispatches (double the normal 60).
ESCALATED_MAX_TURNS = 120

#: Prompt note prepended when a task has failed DECOMPOSE_FIRST_FAILURE_THRESHOLD+
#: times in a row. Instructs the worker to write a plan commit first, then
#: implement piece by piece — and to open a draft PR if the task is still too
#: large after planning.
DECOMPOSE_FIRST_NOTE = """\
**This task has failed 3+ times. Take a different approach this attempt:**

Before writing any production code, write a SHORT plan as your first
commit:
- Create a file `.oxi/plan-{identifier}.md` with: scope (5 bullets
  max), file list, commit ordering.
- `git add` and `git commit -m "{identifier}: plan"` — that's commit 1.
- Then implement piece by piece, committing each piece as you go.

If the task still feels too large after planning, stop after the plan
commit and open a draft PR titled
`{identifier}: plan only — needs split`. The orchestrator will see the
draft PR and dispatch follow-up tasks for the split."""


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
                cwd=os.fspath(worktree_path),
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
    # files_touched: JSON array of paths this task is expected to modify.
    # Populated by the planner / ingest step. Used by the overlap gate.
    files_touched: tuple[str, ...] = ()
    # last_deferred_at: ISO timestamp set when the overlap gate skips this
    # task. Cleared (set to NULL) when the task is actually dispatched.
    last_deferred_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        # Parse files_touched from JSON; treat malformed values as empty.
        raw_files = _safe_column(row, "files_touched") or "[]"
        try:
            files_list = json.loads(raw_files)
            files_touched: tuple[str, ...] = tuple(
                f for f in files_list if isinstance(f, str) and f
            )
        except (TypeError, ValueError):
            files_touched = ()

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
            files_touched=files_touched,
            last_deferred_at=_safe_column(row, "last_deferred_at"),
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

    The overlap gate calls this with limit > 1 so it can find a
    non-overlapping alternative when the first candidate is deferred.
    """
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'planned' "
        "ORDER BY tier ASC, identifier ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _defer_task(
    conn: sqlite3.Connection,
    task_id: int,
    report: OverlapReport,
) -> None:
    """Stamp ``last_deferred_at`` on a planned task whose files overlap with open PRs.

    The task remains ``planned``; only the timestamp and a ledger event
    are written. The next call to ``_pick_next_planned`` will return this
    task again in priority order, so it will be re-evaluated on the next
    tick once overlapping PRs are merged.

    Parameters
    ----------
    conn:
        SQLite connection (transaction is managed here).
    task_id:
        The task's primary key.
    report:
        The :class:`OverlapReport` that triggered the deferral; embedded
        in the ledger event payload for observability.
    """
    now = _now_iso()
    payload = {
        "overlapping_prs": list(report.overlapping_prs),
        "overlapping_files": list(report.overlapping_files),
    }
    with conn:
        conn.execute(
            "UPDATE task SET last_deferred_at = ?, updated_at = ? "
            "WHERE id = ?",
            (now, now, task_id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, LedgerEvent.DISPATCH_DEFERRED, json.dumps(payload)),
        )


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
                LedgerEvent.DISPATCH_STARTED,
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
        event_kind = LedgerEvent.DISPATCH_SUCCEEDED
    elif result.classification is Classification.RETRYABLE_TRANSIENT:
        new_status = "planned"
        event_kind = LedgerEvent.DISPATCH_RETRYABLE
    elif result.classification is Classification.TIMEOUT:
        new_status = "abandoned"
        event_kind = LedgerEvent.DISPATCH_TIMEOUT
    else:  # FAILED
        new_status = "failed"
        event_kind = LedgerEvent.DISPATCH_FAILED

    payload = {
        "session_id": result.session_id,
        "exit_code": result.exit_code,
        "cost_usd": result.cost_usd,
        "wall_clock_seconds": result.wall_clock_seconds,
        "classification": result.classification.value,
    }
    if result.trailing_line:
        payload["trailing_line"] = result.trailing_line[:500]
    # Surface rate-limit exhaustion in the ledger so _pick_model can
    # walk the model fallback chain on the next dispatch of this task.
    if getattr(result, "rate_limit_exhausted", False):
        payload["rate_limit_exhausted"] = True

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


def _fetch_open_pr_numbers(repo: str) -> tuple[int, ...]:
    """Return open PR numbers for ``repo`` via ``gh pr list``.

    Used by the overlap gate to enumerate what's currently open so it
    can then fetch file lists for each.  Returns an empty tuple on any
    error so the gate fails open (no overlap = let the dispatch proceed).

    The limit of 100 matches ``GhCliClient.list_open_prs`` — open PRs
    beyond 100 are rare and the risk of missing an overlap there is
    lower than the risk of blocking the engine unnecessarily.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--state", "open",
                "--json", "number",
                "--limit", "100",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "dispatch.overlap_gate: gh pr list failed (%d); "
                "skipping overlap check (fail-open)",
                proc.returncode,
            )
            return ()
        items = json.loads(proc.stdout.strip() or "[]")
        return tuple(int(item["number"]) for item in items if "number" in item)
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        logger.warning(
            "dispatch.overlap_gate: could not fetch open PRs (%s); "
            "skipping overlap check (fail-open)",
            exc,
        )
        return ()


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
    engine_health: EngineHealth | None = None,
    repo_root: Path,
    binary: str = "claude",
    anthropic_api_key: str | None = None,
    extra_env: dict[str, str] | None = None,
    # Injectable for tests: override the two checks inside
    # _maybe_upgrade_to_success without needing a real git remote or gh CLI.
    _check_commits: Callable[[Path, str], bool] | None = None,
    _check_pr: Callable[[str, str], bool] | None = None,
    # Injectable for tests: supply a pre-built OverlapChecker so tests
    # can inject fake PR file-lists without shelling to gh.
    _overlap_checker: OverlapChecker | None = None,
    # Injectable for tests: override open-PR number discovery so tests
    # don't need a live GitHub API.
    _open_pr_numbers: tuple[int, ...] | None = None,
) -> DispatchResult | None:
    """Dispatch the next planned task, or return None if nothing to do.

    Returns None when:
    - ``engine_state.is_stopping()`` is True at entry
    - ``engine_health.is_unhealthy()`` is True (paused after consecutive failures)
    - no ``planned`` tasks exist
    - all planned tasks are deferred by the file-overlap gate

    Raises on infrastructure failures (worktree provisioning error,
    missing adapter). Does not raise on claude failures — those are
    captured in the DispatchResult's classification.

    The ``_check_commits`` and ``_check_pr`` parameters are for testing
    only; production callers should leave them as None (defaults apply).

    ``engine_health`` is optional for backwards compatibility with callers
    that don't yet pass it.  If None, health-gating is skipped.

    File-overlap gate (T0-104)
    --------------------------
    Before claiming a task, we query the open PRs on the target repo and
    compare the task's ``files_touched`` against each PR's file list. If
    any file overlaps we defer the task (stamp ``last_deferred_at``, emit
    ``dispatch_deferred``) and try the next candidate in priority order.
    If all candidates overlap we return None (nothing to dispatch this
    tick). The gate is **fail-open**: any error fetching PR file lists is
    logged and treated as "no overlap" so a gh outage never blocks the
    engine entirely.
    """
    if engine_state.is_stopping():
        logger.info("dispatch: engine stopping, skipping tick")
        return None

    # Health gate — refuse to dispatch if consecutive failures have
    # triggered the unhealthy state.  Operator resets via `oxi v3 heal`.
    if engine_health is not None and engine_health.is_unhealthy():
        logger.warning(
            "dispatch: engine unhealthy (consecutive failures), skipping tick. "
            "Run `oxi v3 heal` to resume."
        )
        return None

    # Budget gate — refuse to spend more if the hard cap is hit.
    from . import budget as budget_mod
    if budget_mod.is_hard_stopped(conn):
        logger.info("dispatch: budget hard-stop reached, skipping tick")
        return None

    # ---------------------------------------------------------------------------
    # File-overlap gate (T0-104)
    # ---------------------------------------------------------------------------
    # Fetch up to _OVERLAP_SCAN_LIMIT candidates and walk them in priority
    # order, deferring any whose files_touched overlaps with an open PR.
    # Pick the first non-overlapping one for dispatch.
    _OVERLAP_SCAN_LIMIT = 20  # safety cap: avoid unbounded DB scan
    candidates = _pick_next_planned(conn, limit=_OVERLAP_SCAN_LIMIT)
    if not candidates:
        return None

    repo = adapter.github_repo()

    # Build or reuse the overlap checker (injectable for tests).
    if _overlap_checker is None:
        try:
            from .pr_overlap import GhCliPrFilesClient
            gh_client = GhCliPrFilesClient()
            _overlap_checker = OverlapChecker(client=gh_client, repo=repo)
        except Exception as exc:  # pragma: no cover — defensive only
            logger.warning(
                "dispatch: could not build OverlapChecker (%s); "
                "skipping overlap gate (fail-open)",
                exc,
            )
            _overlap_checker = None

    # Discover open PR numbers (injectable for tests).
    if _open_pr_numbers is None:
        open_pr_numbers = _fetch_open_pr_numbers(repo)
    else:
        open_pr_numbers = _open_pr_numbers

    task: Task | None = None
    for candidate in candidates:
        if not candidate.files_touched or _overlap_checker is None:
            # No expected file scope or no checker → skip gate, accept.
            task = candidate
            break
        report = check_overlap(
            planned_files=candidate.files_touched,
            open_pr_numbers=open_pr_numbers,
            checker=_overlap_checker,
        )
        if report.overlaps:
            logger.info(
                "dispatch.overlap_gate: deferring task=%s "
                "overlapping_prs=%s overlapping_files=%s",
                candidate.identifier,
                report.overlapping_prs,
                report.overlapping_files,
            )
            _defer_task(conn, candidate.id, report)
        else:
            task = candidate
            break

    if task is None:
        logger.info(
            "dispatch: all %d candidates deferred by overlap gate; "
            "nothing to dispatch this tick",
            len(candidates),
        )
        return None

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

    # Repeat-failure escalation: if this task has failed terminally 3+ times
    # in a row without a success in between, and there is no active deep_fix
    # escalation already overriding the prompt, synthesise a decompose-first
    # override so the worker takes a different approach this attempt.
    if escalation is None:
        terminal_failures = _count_recent_terminal_failures(conn, task.id)
        if terminal_failures >= DECOMPOSE_FIRST_FAILURE_THRESHOLD:
            from .deep_fix import EscalationRecipe as _EscalationRecipe
            _note = DECOMPOSE_FIRST_NOTE.replace(
                "{identifier}", task.identifier
            )
            escalation = _EscalationRecipe(
                model=None,  # keep the adapter's normal model selection
                prompt_note=_note,
                label="repeat_failure_decompose_first",
            )
            logger.info(
                "dispatch.escalation.repeat_failure task_identifier=%s "
                "failure_count=%d max_turns_used=%d",
                task.identifier,
                terminal_failures,
                ESCALATED_MAX_TURNS,
            )

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
        # Walk the model fallback chain based on prior rate-limit
        # exhaustion for this task. First dispatch starts at the
        # preferred model; each rate-limit strike bumps one rung down.
        rate_limit_strikes = _count_recent_rate_limit_failures(conn, task.id)
        model_name = _pick_model(adapter, fallback_steps=rate_limit_strikes)

    # Escalated dispatches (repeat-failure mode) get a higher turn ceiling
    # to give the worker headroom for planning + incremental commits.
    is_repeat_failure_escalated = (
        escalation is not None
        and escalation.label == "repeat_failure_decompose_first"
    )
    max_turns = ESCALATED_MAX_TURNS if is_repeat_failure_escalated else 60

    invocation = DispatchInvocation(
        prompt=prompt,
        cwd=handle.path,
        session_id=session_id,
        model=model_name,
        max_budget_usd=budget.per_task_opus,
        # 60 turns matches the dollar cap (per_task_opus=$2) — at ~$0.03/turn
        # observed on T1-B sweep, 30 turns burned $1 mid-implementation and
        # workers exited before commit/push/PR. Doubling the turn ceiling
        # gives the model headroom to finish the loop instead of starving.
        # Escalated (repeat-failure) dispatches use ESCALATED_MAX_TURNS (120).
        max_turns=max_turns,
        allowed_tools=("Bash", "Read", "Edit", "Write", "Glob", "Grep"),
        extra_env=dict(extra_env or {}),
        anthropic_api_key=anthropic_api_key,
        binary=binary,
        ssh_alias=hosts[0].ssh_alias,
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

    # Health tracking — update consecutive-failure counter AFTER the DB
    # write so the counter only moves when the outcome is durably recorded.
    if engine_health is not None:
        if result.classification is Classification.SUCCESS:
            engine_health.record_success()
        elif result.classification in (
            Classification.FAILED,
            Classification.TIMEOUT,
        ):
            engine_health.record_failure(
                conn,
                reason=result.classification.value,
            )
        # RETRYABLE_TRANSIENT is not a terminal failure — it's a transient
        # condition (rate limit, OOM). We do NOT increment the failure
        # counter for it so retryable signals don't trigger the unhealthy
        # state during normal rate-limit backoff.

    return result


async def dispatch_loop(
    *,
    conn: sqlite3.Connection,
    adapter: Adapter,
    engine_state: EngineState,
    engine_health: EngineHealth | None = None,
    repo_root: Path,
    max_iterations: int = 1,
    concurrency: int | None = None,
    binary: str = "claude",
    anthropic_api_key: str | None = None,
    extra_env: dict[str, str] | None = None,
    # Injectable for tests: forwarded to dispatch_one → _maybe_upgrade_to_success.
    _check_commits: Callable[[Path, str], bool] | None = None,
    _check_pr: Callable[[str, str], bool] | None = None,
    # Injectable for tests: forwarded to dispatch_one → overlap gate.
    _overlap_checker: OverlapChecker | None = None,
    _open_pr_numbers: tuple[int, ...] | None = None,
) -> list[DispatchResult]:
    """Run the dispatch step up to ``max_iterations`` times.

    Checks ``engine_state.is_stopping()`` at the top of every
    iteration — not just at entry. This is the killswitch-mid-loop
    fix documented in the prior-orchestrator post-mortems.

    Stops early when any of:
    - engine_state requests stop
    - engine_health is unhealthy (consecutive failures exceeded threshold)
    - dispatch_one() returns None (no planned tasks left or all deferred)

    The ``concurrency`` parameter controls how many dispatches run in
    parallel:
    - ``None`` (default) — read the cap from the adapter's
      ``DispatchHost.max_concurrent`` (summed across hosts). This is
      the behavior fork operators expect; the adapter is the source
      of truth.
    - ``1`` — fully serial. Identical to the pre-parallel behavior.
    - any other int — explicit override (used by tests).

    Task claim is atomic at the SQLite level (``WHERE status =
    'planned'`` in dispatch_one), so concurrent calls never race onto
    the same task — at most one winner per row.

    The ``_check_commits``, ``_check_pr``, ``_overlap_checker``, and
    ``_open_pr_numbers`` parameters are for testing only; production
    callers should leave them as None (defaults apply).  They are
    forwarded verbatim to each ``dispatch_one`` call.
    """
    if concurrency is None:
        # Sum across all hosts; the dispatch_pool picks per-host
        # placement on its own. We just need to know how many we can
        # run total.
        hosts = adapter.dispatch_hosts()
        concurrency = sum(h.max_concurrent for h in hosts) if hosts else 1
    if concurrency <= 1:
        return await _dispatch_loop_serial(
            conn=conn,
            adapter=adapter,
            engine_state=engine_state,
            engine_health=engine_health,
            repo_root=repo_root,
            max_iterations=max_iterations,
            binary=binary,
            anthropic_api_key=anthropic_api_key,
            extra_env=extra_env,
            _check_commits=_check_commits,
            _check_pr=_check_pr,
            _overlap_checker=_overlap_checker,
            _open_pr_numbers=_open_pr_numbers,
        )
    return await _dispatch_loop_parallel(
        conn=conn,
        adapter=adapter,
        engine_state=engine_state,
        engine_health=engine_health,
        repo_root=repo_root,
        max_iterations=max_iterations,
        concurrency=concurrency,
        binary=binary,
        anthropic_api_key=anthropic_api_key,
        extra_env=extra_env,
        _check_commits=_check_commits,
        _check_pr=_check_pr,
        _overlap_checker=_overlap_checker,
        _open_pr_numbers=_open_pr_numbers,
    )


async def _dispatch_loop_serial(
    *,
    conn: sqlite3.Connection,
    adapter: Adapter,
    engine_state: EngineState,
    engine_health: EngineHealth | None,
    repo_root: Path,
    max_iterations: int,
    binary: str,
    anthropic_api_key: str | None,
    extra_env: dict[str, str] | None,
    _check_commits: Callable[[Path, str], bool] | None,
    _check_pr: Callable[[str, str], bool] | None,
    _overlap_checker: OverlapChecker | None = None,
    _open_pr_numbers: tuple[int, ...] | None = None,
) -> list[DispatchResult]:
    """The original serial dispatch loop.

    Kept as the implementation for ``concurrency=1`` so existing
    tests, fixtures, and call sites that never asked for parallelism
    keep their exact behavior.
    """
    results: list[DispatchResult] = []
    for _ in range(max_iterations):
        if engine_state.is_stopping():
            break
        if engine_health is not None and engine_health.is_unhealthy():
            break
        outcome = await dispatch_one(
            conn=conn,
            adapter=adapter,
            engine_state=engine_state,
            engine_health=engine_health,
            repo_root=repo_root,
            binary=binary,
            anthropic_api_key=anthropic_api_key,
            extra_env=extra_env,
            _check_commits=_check_commits,
            _check_pr=_check_pr,
            _overlap_checker=_overlap_checker,
            _open_pr_numbers=_open_pr_numbers,
        )
        if outcome is None:
            break
        results.append(outcome)
    return results


async def _dispatch_loop_parallel(
    *,
    conn: sqlite3.Connection,
    adapter: Adapter,
    engine_state: EngineState,
    engine_health: EngineHealth | None,
    repo_root: Path,
    max_iterations: int,
    concurrency: int,
    binary: str,
    anthropic_api_key: str | None,
    extra_env: dict[str, str] | None,
    _check_commits: Callable[[Path, str], bool] | None,
    _check_pr: Callable[[str, str], bool] | None,
    _overlap_checker: OverlapChecker | None = None,
    _open_pr_numbers: tuple[int, ...] | None = None,
) -> list[DispatchResult]:
    """Bounded-parallel dispatch loop.

    Maintains ``concurrency`` in-flight dispatch_one() coroutines.
    When one finishes, immediately spawns the next (up to
    ``max_iterations`` total). Stops when:
    - the spawn budget is exhausted
    - dispatch_one returns None (no planned tasks left or all deferred)
    - engine_state.is_stopping() turns true
    - engine_health.is_unhealthy() turns true

    Why use a manual scheduler instead of asyncio.gather? gather
    requires the full task list up-front; we don't know it because
    each dispatch_one's outcome can affect whether the next
    iteration should run (e.g., mid-loop killswitch). The
    spawn-as-you-finish pattern below honors those gates the same
    way the serial loop does.
    """
    import asyncio

    results: list[DispatchResult] = []
    in_flight: set[asyncio.Task[DispatchResult | None]] = set()
    spawned = 0
    drained = False  # True once dispatch_one() has returned None

    def _should_stop() -> bool:
        if engine_state.is_stopping():
            return True
        if engine_health is not None and engine_health.is_unhealthy():
            return True
        return False

    def _spawn_one() -> bool:
        """Spawn one dispatch_one task. Return True if spawned."""
        nonlocal spawned
        if spawned >= max_iterations or drained or _should_stop():
            return False
        coro = dispatch_one(
            conn=conn,
            adapter=adapter,
            engine_state=engine_state,
            engine_health=engine_health,
            repo_root=repo_root,
            binary=binary,
            anthropic_api_key=anthropic_api_key,
            extra_env=extra_env,
            _check_commits=_check_commits,
            _check_pr=_check_pr,
            _overlap_checker=_overlap_checker,
            _open_pr_numbers=_open_pr_numbers,
        )
        in_flight.add(asyncio.create_task(coro))
        spawned += 1
        return True

    # Prime: fill the in-flight pool up to concurrency.
    for _ in range(concurrency):
        if not _spawn_one():
            break

    # Drain + replenish loop. Each completed task either yields a
    # DispatchResult (running fine, spawn one more) or None (queue
    # empty — stop spawning, drain remaining).
    while in_flight:
        done, in_flight = await asyncio.wait(
            in_flight, return_when=asyncio.FIRST_COMPLETED
        )
        for finished in done:
            outcome = await finished
            if outcome is None:
                drained = True
                continue
            results.append(outcome)
        # Try to refill, unless something flipped the gates.
        if not drained and not _should_stop():
            while len(in_flight) < concurrency and _spawn_one():
                pass
    return results


# ---------------------------------------------------------------------------
# Model selection (tiny helper — may grow)
# ---------------------------------------------------------------------------


#: Per-tier model fallback chain. First entry is the preferred model;
#: subsequent entries are tried when prior dispatches hit rate-limit
#: exhaustion. Ordered cheapest-fallback-last so we never silently
#: downgrade quality without operator visibility (the fallback is
#: surfaced in ledger events).
_MODEL_CHAIN_OPUS_FIRST: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)

_MODEL_CHAIN_SONNET_FIRST: tuple[str, ...] = (
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)


def _pick_model(adapter: Adapter, *, fallback_steps: int = 0) -> str:
    """Return the model name to use for a dispatch.

    Arguments:
        adapter: active adapter; plan_tier() picks the chain.
        fallback_steps: count of recent rate-limit-exhausted dispatches
            for the same task. Each step bumps one rung down the chain
            (Opus → Sonnet → Haiku, or Sonnet → Haiku). Out-of-bounds
            values clamp at the cheapest model in the chain.

    The chain is tier-specific:
      max-plan operators start at Opus and fall through Sonnet → Haiku;
      standard-plan operators start at Sonnet and fall to Haiku.
    """
    tier = adapter.plan_tier()
    if "opus" in tier.lower() or "max" in tier.lower():
        chain = _MODEL_CHAIN_OPUS_FIRST
    else:
        chain = _MODEL_CHAIN_SONNET_FIRST

    idx = max(0, min(fallback_steps, len(chain) - 1))
    return chain[idx]


def _count_recent_rate_limit_failures(
    conn: sqlite3.Connection, task_id: int, *, window_hours: int = 6
) -> int:
    """Count dispatch_retryable events with rate_limit_exhausted=True.

    Used by the dispatcher to decide how far to walk the model fallback
    chain. The window keeps stale rate-limit state from holding a task
    on Haiku forever — a 3 AM rate-limit on Sonnet shouldn't keep the
    task on Haiku at 9 AM.
    """
    rows = conn.execute(
        "SELECT payload FROM event WHERE task_id = ? "
        "AND kind = 'dispatch_retryable' "
        "AND created_at > datetime('now', ?) "
        "ORDER BY id DESC",
        (task_id, f"-{window_hours} hours"),
    ).fetchall()

    count = 0
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            continue
        # Only count retryables flagged by classify() as rate-limit
        # exhaustion. Other transients (SIGTERM, infra hiccup) don't
        # warrant a model change.
        if payload.get("rate_limit_exhausted"):
            count += 1
    return count


def _count_recent_terminal_failures(
    conn: sqlite3.Connection, task_id: int
) -> int:
    """Count how many times this exact task has failed terminally without success in between.

    Walks the event log for this task in reverse chronological order. Each
    ``dispatch_failed`` event counts +1. Stops on the first
    ``dispatch_success`` (chain broken — task succeeded once, then failed
    again; that is a fresh streak). ``dispatch_retryable`` and
    ``dispatch_timeout`` events are ignored: retryable is transient and
    timeout is a wall-clock kill, neither represents a semantic failure of
    the worker's approach.
    """
    rows = conn.execute(
        "SELECT kind FROM event "
        "WHERE task_id = ? AND kind IN ('dispatch_failed', 'dispatch_succeeded') "
        "ORDER BY id DESC",
        (task_id,),
    ).fetchall()

    count = 0
    for row in rows:
        kind = row[0]
        if kind == LedgerEvent.DISPATCH_FAILED:
            count += 1
        elif kind == LedgerEvent.DISPATCH_SUCCEEDED:
            # Streak broken by a prior success — stop counting.
            break
    return count


__all__ = [
    "DEFAULT_SESSION_TAG",
    "DECOMPOSE_FIRST_FAILURE_THRESHOLD",
    "DECOMPOSE_FIRST_NOTE",
    "ESCALATED_MAX_TURNS",
    "Task",
    "_branch_has_commits_ahead",
    "_count_recent_terminal_failures",
    "_defer_task",
    "_fetch_open_pr_numbers",
    "_maybe_upgrade_to_success",
    "_pr_exists_for_branch",
    "dispatch_loop",
    "dispatch_one",
    "is_orphan_reapable",
    # Re-exported so callers can import EngineHealth from this module.
    "EngineHealth",
]
