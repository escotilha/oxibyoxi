r"""ci_issue_filer — surface CI failures in the ledger.

On every tick, ci_issue_filer:

1. Loads every dispatched task whose ``pr_number IS NOT NULL`` (i.e. tasks
   with an open PR that pr_watcher has already discovered).
2. For each task, fetches the current PR from GitHub and inspects the
   individual check runs attached to its HEAD commit.
3. For every check run in a failure state, emits a ``ci_failure_observed``
   ledger event — **at most once per (task, check_run_name) combination**.
   The idempotency guard prevents ledger spam on subsequent ticks.
4. Returns a :class:`FilingReport` summarising what happened.

Why a separate module from pr_watcher?
---------------------------------------
``pr_watcher`` owns lifecycle state-machine transitions (dispatched →
merged, dispatched → failed). ``ci_issue_filer`` owns *observability*:
it surfaces individual check-run failures so operators can see in the
dashboard *which* check failed and *when*.  Keeping the two concerns
separate means neither module grows a responsibility it wasn't designed
for.

Idempotency
-----------
A check run name is considered "already filed" for a task when the
``event`` table already has a row with ``kind='ci_failure_observed'``
**and** a JSON payload whose ``check_name`` field matches the current
check run's name.  This is checked in-process (via a set built from a
single SELECT per task) — no extra columns needed.

Invariants
----------
- Shares ``engine_state.is_stopping()`` discipline with every other loop.
- Every event write is atomic with ``last_progress_at`` + ``updated_at``
  on the task row (consistent with the stamp-on-every-transition rule).
- Never operates on tasks in terminal states (merged, failed, abandoned).
- Uses a ``GitHubClient`` protocol — tests inject a fake without touching
  real GitHub.  Production wires ``GhCliClient``.

What this module does NOT do
-----------------------------
- Does not transition task status. pr_watcher / auto_merge own those.
- Does not re-dispatch or recover from CI failures. auto_recover owns that.
- Does not merge PRs. auto_merge owns that.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from ..adapter import get_active_adapter
from ._timefmt import now_iso as _now_iso
from .dispatch import Task
from .engine_state import EngineState
from .github_client import GhCliClient, GitHubClient, PRState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilingReport:
    """Summary of one ci_issue_filer pass.

    Attributes:
        prs_inspected: number of tasks/PRs examined this pass.
        new_failures_filed: number of ``ci_failure_observed`` events
            written (only new ones — already-filed checks are skipped).
        checks_already_filed: number of check-run failures already in
            the ledger (idempotency guard fired).
        prs_skipped: tasks whose PR could not be fetched (permissions
            change, PR deleted, etc.).
    """

    prs_inspected: int
    new_failures_filed: int
    checks_already_filed: int
    prs_skipped: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_dispatched_with_pr(conn: sqlite3.Connection) -> list[Task]:
    """Return dispatched tasks that have a pr_number stamped."""
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'dispatched' AND pr_number IS NOT NULL"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _already_filed_check_names(conn: sqlite3.Connection, task_id: int) -> set[str]:
    """Return the set of check_names already filed for this task.

    Reads ``kind='ci_failure_observed'`` events and extracts the
    ``check_name`` field from their JSON payload.  O(events-per-task)
    — tasks typically have a handful of events, so this is cheap.
    """
    rows = conn.execute(
        "SELECT payload FROM event WHERE task_id = ? AND kind = 'ci_failure_observed'",
        (task_id,),
    ).fetchall()
    names: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
            name = payload.get("check_name")
            if name:
                names.add(name)
        except (json.JSONDecodeError, TypeError):
            pass
    return names


def _file_failure(
    conn: sqlite3.Connection,
    task: Task,
    pr_number: int,
    check_name: str,
    conclusion: str,
) -> None:
    """Atomically emit a ``ci_failure_observed`` event and bump freshness."""
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
                "ci_failure_observed",
                json.dumps(
                    {
                        "pr_number": pr_number,
                        "check_name": check_name,
                        "conclusion": conclusion,
                    }
                ),
            ),
        )
    logger.info(
        "ci_issue_filer: filed ci_failure_observed for task %s "
        "(pr #%d, check %r, conclusion %r)",
        task.identifier,
        pr_number,
        check_name,
        conclusion,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def file_failures(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    client: GitHubClient | None = None,
    repo: str | None = None,
) -> FilingReport:
    """Run one ci_issue_filer pass.

    Arguments:
        conn: SQLite connection.
        engine_state: shared stopping flag; the pass is a no-op when
            ``engine_state.is_stopping()`` is True.
        client: GitHub client.  Defaults to :class:`.GhCliClient`;
            tests inject :class:`tests.fixtures.fake_github.FakeGitHubClient`.
        repo: ``owner/name`` slug.  Defaults to ``adapter.github_repo()``.

    Returns a :class:`FilingReport`.  Individual PR fetch failures are
    logged and counted as ``prs_skipped`` — they do **not** raise.
    """
    empty = FilingReport(
        prs_inspected=0,
        new_failures_filed=0,
        checks_already_filed=0,
        prs_skipped=0,
    )

    if engine_state.is_stopping():
        logger.info("ci_issue_filer: engine stopping, skipping pass")
        return empty

    if client is None:
        client = GhCliClient()
    if repo is None:
        adapter = get_active_adapter()
        repo = adapter.github_repo()

    tasks = _load_dispatched_with_pr(conn)

    prs_inspected = 0
    new_filed = 0
    already_filed = 0
    skipped = 0

    for task in tasks:
        if engine_state.is_stopping():
            break

        if task.pr_number is None:  # defensive — query above guarantees NOT NULL
            continue

        pr = client.get_pr(repo, task.pr_number)
        if pr is None:
            # PR was deleted or permissions changed — skip and let pr_watcher
            # reconcile the task on its next pass.
            logger.debug(
                "ci_issue_filer: PR #%d not found for task %s, skipping",
                task.pr_number,
                task.identifier,
            )
            skipped += 1
            continue

        # Only inspect open PRs — merged/closed PRs have no actionable CI.
        if pr.state is not PRState.OPEN:
            continue

        prs_inspected += 1

        # Fetch individual check runs for this PR's HEAD commit.
        check_runs = client.list_check_runs(repo, task.pr_number)

        if not check_runs:
            continue

        # Build idempotency set for this task in one query.
        filed_names = _already_filed_check_names(conn, task.id)

        for run in check_runs:
            if run.conclusion not in _FAILURE_CONCLUSIONS:
                continue
            if run.name in filed_names:
                already_filed += 1
                logger.debug(
                    "ci_issue_filer: check %r already filed for task %s, skipping",
                    run.name,
                    task.identifier,
                )
                continue
            _file_failure(conn, task, task.pr_number, run.name, run.conclusion)
            filed_names.add(run.name)  # keep local set current for this pass
            new_filed += 1

    return FilingReport(
        prs_inspected=prs_inspected,
        new_failures_filed=new_filed,
        checks_already_filed=already_filed,
        prs_skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Check-run conclusions that count as failures and trigger event filing.
_FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
    }
)


__all__ = [
    "FilingReport",
    "file_failures",
]
