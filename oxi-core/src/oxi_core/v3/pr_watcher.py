r"""PR watcher — reconciles DB task state with GitHub PR state.

On every tick, pr_watcher:

1. Lists open PRs in the target repo whose head branch follows the
   oxi convention (``<branch_prefix><session_tag>-<slug>``).
2. For each PR, finds the matching ``task`` row by branch identifier
   and stamps ``task.pr_number`` if not already set.
3. For tasks that already have a ``pr_number``, checks the PR's
   current state:
   - Still open + CI pending → stays ``dispatched``, stamp freshness
   - Still open + CI success → stays ``dispatched`` (auto_merge's job
     to actually merge it)
   - Merged → transition to ``merged``
   - Closed without merge → transition to ``failed``

Invariants:

- Shares \`engine_state.is_stopping()\` discipline with dispatch + heartbeat.
- Every transition stamps ``last_progress_at`` + ``updated_at`` atomically
  with ``status`` in one SQLite transaction, same as dispatch.
- Never operates on non-dispatched tasks. pr_watcher is purely about
  in-flight work.
- Uses a ``GitHubClient`` protocol so tests can inject a fake without
  touching real GitHub. Production wires \`GhCliClient\`.

What this module does NOT do:

- Does not merge PRs. ``auto_merge.py`` owns the critic-gated merge.
- Does not create PRs. Workers create PRs from inside their dispatched
  claude session.
- Does not infer branch → task mapping from fuzzy string matching.
  pr_watcher expects a deterministic identifier-in-branch convention
  (\`feat/<tag>-<slug>\` as produced by ``worktree_provision``).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from ..adapter import get_active_adapter
from ._timefmt import now_iso as _now_iso
from .dispatch import Task
from .engine_state import EngineState
from .github_client import GhCliClient, GitHubClient, PRCheckStatus, PRState, PullRequest

logger = logging.getLogger(__name__)


# Branch identifier extractor. Matches the convention composed by
# ``worktree_provision.compose_branch_name``: ``<prefix><tag>-<slug>``.
# We don't hardcode the prefix; callers pass it in (it comes from the
# adapter's branch_prefixes). The slug is the slugified task identifier
# — the planner's ``T0-1`` becomes ``t0-1`` in the branch.
_BRANCH_IDENT_RE = re.compile(
    r"^[a-z]+/[a-z0-9]+-(?P<slug>[a-z0-9][a-z0-9\-]*)$"
)


def _branch_to_identifier_slug(branch: str) -> str | None:
    """Extract the slugified task identifier from a branch name.

    Returns None if the branch doesn't match the oxi convention.
    Callers filter PRs via this — an unrelated human-pushed branch
    is ignored.
    """
    m = _BRANCH_IDENT_RE.match(branch)
    if not m:
        return None
    return m.group("slug")


def _identifier_to_slug(identifier: str) -> str:
    """Inverse of the slugification worktree_provision does.

    Only used for matching DB rows → PR branches. Kept local to this
    module to avoid a cross-module coupling where a change to
    slugification in one place silently breaks the other.
    """
    # Mirror worktree_provision._slugify: lower + non-alnum→hyphen + trim.
    import re as _re
    slug = _re.sub(r"[^a-z0-9\-]+", "-", identifier.lower()).strip("-")
    return slug or "task"


@dataclass(frozen=True)
class WatchReport:
    """Summary of one watch pass."""

    pr_numbers_stamped: int
    tasks_transitioned_merged: int
    tasks_transitioned_failed: int
    open_prs_observed: int
    unmatched_prs: int


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def _stamp_pr_number(conn: sqlite3.Connection, task: Task, pr_number: int) -> None:
    """First-observation of a PR for a task. Stamps pr_number atomically."""
    now = _now_iso()
    with conn:
        conn.execute(
            "UPDATE task SET pr_number = ?, last_progress_at = ?, "
            "updated_at = ? WHERE id = ? AND pr_number IS NULL",
            (pr_number, now, now, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task.id,
                "pr_observed",
                json.dumps({"pr_number": pr_number}),
            ),
        )


def _transition_to_merged(conn: sqlite3.Connection, task: Task) -> None:
    """Task's PR was merged. Move to terminal success."""
    now = _now_iso()
    with conn:
        conn.execute(
            "UPDATE task SET status = 'merged', "
            "last_progress_at = ?, updated_at = ?, "
            "merged_at = ? "
            "WHERE id = ? AND status = 'dispatched'",
            (now, now, now, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task.id,
                "pr_merged",
                json.dumps({"pr_number": task.pr_number}),
            ),
        )


def _transition_to_failed(
    conn: sqlite3.Connection, task: Task, reason: str
) -> None:
    """Task's PR was closed without merge, or CI failed terminally."""
    now = _now_iso()
    with conn:
        conn.execute(
            "UPDATE task SET status = 'failed', "
            "last_progress_at = ?, updated_at = ?, "
            "failed_at = ?, failure_reason = ? "
            "WHERE id = ? AND status = 'dispatched'",
            (now, now, now, reason, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task.id,
                "pr_failed",
                json.dumps({"pr_number": task.pr_number, "reason": reason}),
            ),
        )


def _stamp_progress(conn: sqlite3.Connection, task: Task, kind: str, payload: dict) -> None:
    """Refresh freshness without changing status."""
    now = _now_iso()
    with conn:
        conn.execute(
            "UPDATE task SET last_progress_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task.id, kind, json.dumps(payload)),
        )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_dispatched_without_pr(conn: sqlite3.Connection) -> list[Task]:
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'dispatched' AND pr_number IS NULL"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _load_dispatched_with_pr(conn: sqlite3.Connection) -> list[Task]:
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'dispatched' AND pr_number IS NOT NULL"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def watch(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    client: GitHubClient | None = None,
    repo: str | None = None,
    branch_prefix: str | None = None,
) -> WatchReport:
    """Run one reconciliation pass.

    Arguments:
        conn: SQLite connection.
        engine_state: shared stopping flag.
        client: GitHub client. Defaults to GhCliClient; tests inject a fake.
        repo: owner/name slug. Defaults to adapter.github_repo().
        branch_prefix: branch prefix to filter PRs. Defaults to the first
            entry of adapter.branch_prefixes().

    Returns a WatchReport of what happened. No exceptions for PR
    state — only for configuration errors (missing adapter, etc).
    """
    if engine_state.is_stopping():
        logger.info("pr_watcher: engine stopping, skipping pass")
        return WatchReport(
            pr_numbers_stamped=0,
            tasks_transitioned_merged=0,
            tasks_transitioned_failed=0,
            open_prs_observed=0,
            unmatched_prs=0,
        )

    if client is None:
        client = GhCliClient()
    if repo is None or branch_prefix is None:
        adapter = get_active_adapter()
        if repo is None:
            repo = adapter.github_repo()
        if branch_prefix is None:
            prefixes = adapter.branch_prefixes()
            if not prefixes:
                raise ValueError("adapter.branch_prefixes() returned empty")
            branch_prefix = prefixes[0]

    pr_stamped = 0
    merged = 0
    failed = 0
    unmatched = 0

    # --- Phase 1: Discover PRs for tasks that don't have pr_number yet.
    open_prs = client.list_open_prs(repo, head_prefix=branch_prefix)

    # Index open PRs by slug for O(1) lookup.
    prs_by_slug: dict[str, PullRequest] = {}
    for pr in open_prs:
        slug = _branch_to_identifier_slug(pr.head_branch)
        if slug is None:
            unmatched += 1
            continue
        prs_by_slug[slug] = pr

    for task in _load_dispatched_without_pr(conn):
        slug = _identifier_to_slug(task.identifier)
        pr = prs_by_slug.get(slug)
        if pr is None:
            continue
        _stamp_pr_number(conn, task, pr.number)
        pr_stamped += 1

    # --- Phase 2: Update tasks that already have a pr_number.
    for task in _load_dispatched_with_pr(conn):
        pr = client.get_pr(repo, task.pr_number) if task.pr_number else None
        if pr is None:
            # PR disappeared (deleted? permissions?). Keep the task in
            # dispatched; heartbeat will reap if it goes stale.
            continue
        if pr.state is PRState.MERGED:
            _transition_to_merged(conn, task)
            merged += 1
        elif pr.state is PRState.CLOSED:
            _transition_to_failed(conn, task, reason="pr_closed_unmerged")
            failed += 1
        else:
            # Still open. Stamp freshness so heartbeat doesn't reap.
            _stamp_progress(
                conn, task,
                "pr_open_observed",
                {
                    "pr_number": pr.number,
                    "check_status": pr.check_status.value,
                    # Surface auto-merge arm state so operators can verify
                    # T2-44 enforcement: approved PRs should show True here
                    # on the tick after auto_merge.run() arms them.
                    "auto_merge_armed": pr.auto_merge_armed,
                },
            )
            if pr.check_status is PRCheckStatus.FAILURE:
                # Transition is not pr_watcher's call — auto_merge decides
                # whether to re-dispatch or fail. For now, just record it.
                # In P1-6c auto_merge will consume this signal.
                pass

    return WatchReport(
        pr_numbers_stamped=pr_stamped,
        tasks_transitioned_merged=merged,
        tasks_transitioned_failed=failed,
        open_prs_observed=len(open_prs),
        unmatched_prs=unmatched,
    )


__all__ = [
    "WatchReport",
    "watch",
]
