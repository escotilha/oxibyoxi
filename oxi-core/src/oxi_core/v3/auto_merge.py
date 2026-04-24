r"""Auto-merge — post-CI critic gate.

On every tick, auto_merge finds dispatched tasks with:

- ``pr_number IS NOT NULL`` (pr_watcher has observed the PR)
- PR state is OPEN
- PR CI status is SUCCESS

For each such task, auto_merge invokes the critic and:

- APPROVE → merge the PR via the \`gh\` client (or whatever \`GitHubClient\`
  is wired). The task transitions to ``merged`` on the next pr_watcher
  tick, or directly if the merge-call succeeds synchronously.
- REJECT → transition the task to ``failed`` with
  ``failure_reason=critic_rejected``, record the reason in a ledger
  event so operators can see why.

Terminal-state discipline (anti-pattern #7 from the post-mortems):
MERGED PRs are never rejected. If auto_merge runs and the PR has
already been merged (by a human or another tool), the module emits a
``merge_already_done`` ledger event and moves on. No false-positive
rejection counter.

Invariants:

- Shares ``engine_state.is_stopping()`` with every other loop.
- Every state transition is atomic with ``last_progress_at`` +
  ``updated_at``.
- Never operates on terminal-status tasks (\`merged\`, \`abandoned\`,
  \`failed\`).

Adapter gate: auto_merge only runs if ``adapter.policy().auto_merge``
is True. Reference adapter returns False (safe default for demos).
Forks opt in explicitly.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from ..adapter import RoadmapItem, get_active_adapter
from .critic import CriticBackend, CriticInput, Verdict
from .dispatch import Task
from .engine_state import EngineState
from .github_client import GhCliClient, GitHubClient, PRCheckStatus, PRState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoMergeReport:
    """Summary of one auto_merge pass.

    Attributes:
        considered: tasks that passed the pre-filter (dispatched + pr + open + CI green).
        merged: PRs auto_merge successfully merged.
        already_merged: PRs a human/other tool already merged (noop).
        critic_rejected: tasks that the critic rejected.
        merge_failed: PRs where the gh merge call returned failure.
    """

    considered: int
    merged: int
    already_merged: int
    critic_rejected: int
    merge_failed: int


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _load_candidates(conn: sqlite3.Connection) -> list[Task]:
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'dispatched' "
        "AND pr_number IS NOT NULL ORDER BY id ASC"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _transition_to_merged(conn: sqlite3.Connection, task: Task, by: str) -> None:
    now = _now_iso()
    with conn:
        conn.execute(
            "UPDATE task SET status = 'merged', "
            "last_progress_at = ?, updated_at = ?, merged_at = ? "
            "WHERE id = ? AND status = 'dispatched'",
            (now, now, now, task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task.id,
                "merged_by_auto_merge",
                json.dumps({"pr_number": task.pr_number, "merged_by": by}),
            ),
        )


def _transition_to_failed(
    conn: sqlite3.Connection, task: Task, reason: str, detail: str
) -> None:
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
                "auto_merge_rejected",
                json.dumps({
                    "pr_number": task.pr_number,
                    "reason": reason,
                    "detail": detail,
                }),
            ),
        )


def _emit_noop(conn: sqlite3.Connection, task: Task, kind: str, payload: dict) -> None:
    """Record a ledger event without changing status."""
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task.id, kind, json.dumps(payload)),
        )


def _task_to_item(task: Task) -> RoadmapItem:
    return RoadmapItem(
        identifier=task.identifier,
        tier=task.tier,
        title=task.title,
        subtitle=task.subtitle,
        target_repo=task.target_repo,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    critic: CriticBackend,
    *,
    client: GitHubClient | None = None,
    repo: str | None = None,
    merge_method: str = "squash",
    force: bool = False,
) -> AutoMergeReport:
    """Run one auto-merge pass.

    Arguments:
        conn: SQLite connection.
        engine_state: shared stopping flag.
        critic: backend used to approve/reject each candidate PR.
        client: GitHub client; defaults to GhCliClient.
        repo: target repo slug; defaults to adapter.github_repo().
        merge_method: one of 'squash', 'merge', 'rebase'. Default squash.
        force: bypass the adapter's ``policy().auto_merge`` gate. Tests
            set this True; production never should.
    """
    empty = AutoMergeReport(
        considered=0, merged=0, already_merged=0,
        critic_rejected=0, merge_failed=0,
    )

    if engine_state.is_stopping():
        logger.info("auto_merge: engine stopping, skipping pass")
        return empty

    adapter = get_active_adapter()
    if not force and not adapter.policy().auto_merge:
        logger.info("auto_merge: adapter.policy().auto_merge is False; skipping")
        return empty

    # Budget gate — each critic invocation spends money. Refuse to start
    # a pass if the hard cap is hit; let heartbeat continue reaping.
    from . import budget as budget_mod
    if budget_mod.is_hard_stopped(conn):
        logger.info("auto_merge: budget hard-stop reached, skipping pass")
        return empty

    if client is None:
        client = GhCliClient()
    if repo is None:
        repo = adapter.github_repo()

    candidates = _load_candidates(conn)
    considered = 0
    merged = 0
    already_merged = 0
    critic_rejected = 0
    merge_failed = 0

    for task in candidates:
        if task.pr_number is None:  # defensive — filter above should guarantee
            continue
        pr = client.get_pr(repo, task.pr_number)
        if pr is None:
            # PR deleted or permissions changed. Skip; pr_watcher will
            # eventually reconcile.
            continue

        # Terminal-state discipline: a MERGED PR is never rejected.
        if pr.state is PRState.MERGED:
            _transition_to_merged(conn, task, by="observed_already_merged")
            already_merged += 1
            _emit_noop(
                conn, task,
                "merge_already_done",
                {"pr_number": pr.number},
            )
            continue

        # Skip non-open and non-CI-green PRs; they're not candidates.
        if pr.state is not PRState.OPEN:
            continue
        if pr.check_status is not PRCheckStatus.SUCCESS:
            continue

        considered += 1

        # Compute a cheap CI summary for the critic (avoids re-fetching).
        ci_summary = "pass"
        critic_input = CriticInput(
            item=_task_to_item(task),
            pr_number=pr.number,
            diff="",  # diff fetch is the future work; for now critic sees metadata only
            ci_status=ci_summary,
            head_branch=pr.head_branch,
        )
        verdict = critic.review(critic_input)

        if verdict.verdict is Verdict.REJECT:
            _transition_to_failed(
                conn, task,
                reason="critic_rejected",
                detail=verdict.reason,
            )
            critic_rejected += 1
            continue

        # APPROVE — attempt merge.
        ok = client.merge_pr(repo, pr.number, method=merge_method)
        if ok:
            _transition_to_merged(conn, task, by="auto_merge")
            merged += 1
        else:
            merge_failed += 1
            _emit_noop(
                conn, task,
                "auto_merge_gh_failure",
                {"pr_number": pr.number},
            )

    return AutoMergeReport(
        considered=considered,
        merged=merged,
        already_merged=already_merged,
        critic_rejected=critic_rejected,
        merge_failed=merge_failed,
    )


__all__ = [
    "AutoMergeReport",
    "run",
]
