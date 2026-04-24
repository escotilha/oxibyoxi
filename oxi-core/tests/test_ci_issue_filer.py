"""Tests for oxi_core.v3.ci_issue_filer — CI failure ledger surfacing.

Test strategy:
- Pure DB + FakeGitHubClient: no subprocess, no real GitHub.
- Every observable effect is checked: DB event rows, FilingReport counters.

Coverage matrix:
  happy path — single failing check run files ci_failure_observed event
  idempotency — second pass does NOT re-file already-filed checks
  multiple check runs on one PR — each failing run filed separately
  mixed conclusions — only failure conclusions are filed, passing skipped
  non-open PR — merged/closed PR is skipped (no events, prs_inspected=0)
  pr disappeared — get_pr returns None → prs_skipped += 1
  no check runs — empty list → nothing filed
  engine stopping — noop, returns empty FilingReport
  task without pr_number — ignored (already filtered by query)
  multiple tasks — each task's checks filed independently
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# Make tests/fixtures importable.
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from fake_github import FakeGitHubClient  # noqa: E402

from oxi_core import db  # noqa: E402
from oxi_core.adapter import (  # noqa: E402
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3.ci_issue_filer import FilingReport, file_failures  # noqa: E402
from oxi_core.v3.engine_state import EngineState  # noqa: E402
from oxi_core.v3.github_client import (  # noqa: E402
    CheckRun,
    PRCheckStatus,
    PRState,
    PullRequest,
)

# ---------------------------------------------------------------------------
# Minimal adapter
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    db_path_value: str

    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig(db_path=self.db_path_value)
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (DispatchHost(name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"),)
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def env(tmp_path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "state": EngineState(plan_tier="standard", max_concurrent=4),
        "client": FakeGitHubClient(),
        "repo": "owner/repo",
    }
    handle.connection.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_dispatched(conn, identifier: str, *, pr_number: int | None = None) -> int:
    """Insert a dispatched task row; return its id."""
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES (?, 0, 't', 'dispatched', ?)",
        (identifier, pr_number),
    )
    conn.commit()
    return cur.lastrowid


def _pr(
    number: int,
    head: str,
    *,
    state: PRState = PRState.OPEN,
    check: PRCheckStatus = PRCheckStatus.FAILURE,
    mergeable: bool | None = True,
) -> PullRequest:
    return PullRequest(
        number=number, title=f"pr {number}", head_branch=head,
        state=state, check_status=check, mergeable=mergeable,
    )


def _run(name: str, conclusion: str = "failure") -> CheckRun:
    return CheckRun(name=name, status="completed", conclusion=conclusion)


def _events(conn, task_id: int, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM event WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# Core happy path
# ---------------------------------------------------------------------------


def test_single_failing_check_run_files_event(env):
    task_id = _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr("owner/repo", _pr(10, "feat/oxi-t1-1"))
    env["client"].set_check_runs(
        "owner/repo", 10,
        (_run("tests"),),
    )

    report = file_failures(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"],
    )

    assert report.prs_inspected == 1
    assert report.new_failures_filed == 1
    assert report.checks_already_filed == 0
    assert report.prs_skipped == 0

    events = _events(env["conn"], task_id, "ci_failure_observed")
    assert len(events) == 1
    assert events[0]["pr_number"] == 10
    assert events[0]["check_name"] == "tests"
    assert events[0]["conclusion"] == "failure"


def test_idempotency_second_pass_does_not_refile(env):
    task_id = _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr("owner/repo", _pr(10, "feat/oxi-t1-1"))
    env["client"].set_check_runs("owner/repo", 10, (_run("tests"),))

    # First pass — files the event.
    file_failures(env["conn"], env["state"], client=env["client"], repo=env["repo"])
    # Second pass — should be a no-op for this check.
    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report.new_failures_filed == 0
    assert report.checks_already_filed == 1
    # Still only one event in the ledger.
    assert len(_events(env["conn"], task_id, "ci_failure_observed")) == 1


def test_multiple_failing_check_runs_filed_separately(env):
    task_id = _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr("owner/repo", _pr(10, "feat/oxi-t1-1"))
    env["client"].set_check_runs(
        "owner/repo", 10,
        (
            _run("tests"),
            _run("lint"),
            _run("build"),
        ),
    )

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report.new_failures_filed == 3
    events = _events(env["conn"], task_id, "ci_failure_observed")
    assert len(events) == 3
    names = {e["check_name"] for e in events}
    assert names == {"tests", "lint", "build"}


def test_only_failure_conclusions_are_filed(env):
    task_id = _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr(
        "owner/repo",
        _pr(10, "feat/oxi-t1-1", check=PRCheckStatus.FAILURE),
    )
    env["client"].set_check_runs(
        "owner/repo", 10,
        (
            _run("tests", conclusion="failure"),
            _run("lint", conclusion="success"),
            _run("deploy", conclusion="cancelled"),
            _run("docs", conclusion="skipped"),
            _run("perf", conclusion="timed_out"),
        ),
    )

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    # "failure", "cancelled", "timed_out" are failure conclusions; "success" and
    # "skipped" are not.
    assert report.new_failures_filed == 3
    events = _events(env["conn"], task_id, "ci_failure_observed")
    filed_names = {e["check_name"] for e in events}
    assert filed_names == {"tests", "deploy", "perf"}


# ---------------------------------------------------------------------------
# Non-open PRs are skipped
# ---------------------------------------------------------------------------


def test_merged_pr_not_inspected(env):
    _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr(
        "owner/repo",
        _pr(10, "feat/oxi-t1-1", state=PRState.MERGED),
    )
    env["client"].set_check_runs("owner/repo", 10, (_run("tests"),))

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report.prs_inspected == 0
    assert report.new_failures_filed == 0


def test_closed_pr_not_inspected(env):
    _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr(
        "owner/repo",
        _pr(10, "feat/oxi-t1-1", state=PRState.CLOSED),
    )
    env["client"].set_check_runs("owner/repo", 10, (_run("tests"),))

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report.prs_inspected == 0
    assert report.new_failures_filed == 0


# ---------------------------------------------------------------------------
# PR disappeared
# ---------------------------------------------------------------------------


def test_missing_pr_counts_as_skipped(env):
    _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    # No PR added to the fake — get_pr returns None.

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report.prs_skipped == 1
    assert report.new_failures_filed == 0


# ---------------------------------------------------------------------------
# No check runs
# ---------------------------------------------------------------------------


def test_no_check_runs_files_nothing(env):
    _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr("owner/repo", _pr(10, "feat/oxi-t1-1"))
    # No check runs registered — list_check_runs returns ().

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report.prs_inspected == 1
    assert report.new_failures_filed == 0


# ---------------------------------------------------------------------------
# Task without pr_number is ignored
# ---------------------------------------------------------------------------


def test_task_without_pr_number_ignored(env):
    _seed_dispatched(env["conn"], identifier="T1-1")  # no pr_number

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report.prs_inspected == 0
    assert report.new_failures_filed == 0


# ---------------------------------------------------------------------------
# Engine stop
# ---------------------------------------------------------------------------


def test_file_failures_is_noop_when_stopping(env):
    _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr("owner/repo", _pr(10, "feat/oxi-t1-1"))
    env["client"].set_check_runs("owner/repo", 10, (_run("tests"),))
    env["state"].request_stop()

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report == FilingReport(
        prs_inspected=0,
        new_failures_filed=0,
        checks_already_filed=0,
        prs_skipped=0,
    )


# ---------------------------------------------------------------------------
# Multiple tasks
# ---------------------------------------------------------------------------


def test_multiple_tasks_each_filed_independently(env):
    task_a = _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    task_b = _seed_dispatched(env["conn"], identifier="T1-2", pr_number=11)

    env["client"].add_pr("owner/repo", _pr(10, "feat/oxi-t1-1"))
    env["client"].add_pr("owner/repo", _pr(11, "feat/oxi-t1-2"))
    env["client"].set_check_runs("owner/repo", 10, (_run("tests"),))
    env["client"].set_check_runs(
        "owner/repo", 11,
        (_run("lint"), _run("build")),
    )

    report = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )

    assert report.prs_inspected == 2
    assert report.new_failures_filed == 3  # 1 for T1-1, 2 for T1-2

    assert len(_events(env["conn"], task_a, "ci_failure_observed")) == 1
    assert len(_events(env["conn"], task_b, "ci_failure_observed")) == 2


# ---------------------------------------------------------------------------
# Adapter-default repo pathway
# ---------------------------------------------------------------------------


def test_file_failures_defaults_repo_from_adapter(env):
    """Omitting repo reads it from the active adapter."""
    task_id = _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr("owner/repo", _pr(10, "feat/oxi-t1-1"))
    env["client"].set_check_runs("owner/repo", 10, (_run("tests"),))

    # No repo passed — should pull from adapter.
    report = file_failures(env["conn"], env["state"], client=env["client"])

    assert report.new_failures_filed == 1
    assert len(_events(env["conn"], task_id, "ci_failure_observed")) == 1


# ---------------------------------------------------------------------------
# Idempotency across passes with a new failure appearing mid-run
# ---------------------------------------------------------------------------


def test_new_failure_filed_after_first_pass(env):
    """A check that passes on pass 1 and fails on pass 2 gets filed on pass 2."""
    task_id = _seed_dispatched(env["conn"], identifier="T1-1", pr_number=10)
    env["client"].add_pr("owner/repo", _pr(10, "feat/oxi-t1-1"))

    # Pass 1: only "tests" is failing; "lint" passes.
    env["client"].set_check_runs(
        "owner/repo", 10,
        (_run("tests", "failure"), _run("lint", "success")),
    )
    report1 = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )
    assert report1.new_failures_filed == 1

    # Pass 2: now "lint" has also failed.
    env["client"].set_check_runs(
        "owner/repo", 10,
        (_run("tests", "failure"), _run("lint", "failure")),
    )
    report2 = file_failures(
        env["conn"], env["state"], client=env["client"], repo=env["repo"]
    )
    assert report2.new_failures_filed == 1     # only lint is new
    assert report2.checks_already_filed == 1   # tests already filed

    events = _events(env["conn"], task_id, "ci_failure_observed")
    assert len(events) == 2
    names = {e["check_name"] for e in events}
    assert names == {"tests", "lint"}
