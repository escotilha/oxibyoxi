"""Tests for oxi_core.v3.auto_merge — critic-gated merge orchestration."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

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
from oxi_core.v3.auto_merge import AutoMergeReport, run  # noqa: E402
from oxi_core.v3.critic import (  # noqa: E402
    AlwaysApproveBackend,
    AlwaysRejectBackend,
    CriticInput,
    CriticVerdict,
    FunctionCriticBackend,
    Verdict,
)
from oxi_core.v3.engine_state import EngineState  # noqa: E402
from oxi_core.v3.github_client import (  # noqa: E402
    PRCheckStatus,
    PRState,
    PullRequest,
)

# ---------------------------------------------------------------------------
# Adapter with toggleable auto_merge
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    db_path_value: str
    auto_merge_enabled: bool = True

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
    def policy(self):
        return DispatchPolicy(auto_merge=self.auto_merge_enabled)


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


def _seed(conn, identifier: str, pr_number: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES (?, 0, 't', 'dispatched', ?)",
        (identifier, pr_number),
    )
    conn.commit()
    return cur.lastrowid


def _pr(
    number: int, head: str = "feat/sa-t0-1",
    *, state: PRState = PRState.OPEN,
    check: PRCheckStatus = PRCheckStatus.SUCCESS,
    mergeable: bool | None = True,
) -> PullRequest:
    return PullRequest(
        number=number, title=f"pr {number}", head_branch=head,
        state=state, check_status=check, mergeable=mergeable,
    )


# ---------------------------------------------------------------------------
# Gate: engine stopping + adapter policy
# ---------------------------------------------------------------------------


def test_engine_stopping_is_noop(env):
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _pr(42))
    env["state"].request_stop()

    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report == AutoMergeReport(0, 0, 0, 0, 0)


def test_adapter_auto_merge_disabled_is_noop(tmp_path):
    register_adapter(
        _Adapter(db_path_value=str(tmp_path / "oxi.db"), auto_merge_enabled=False)
    )
    handle = db.connect()
    try:
        _seed(handle.connection, "T0-1", 42)
        client = FakeGitHubClient()
        client.add_pr("owner/repo", _pr(42))
        state = EngineState(plan_tier="standard", max_concurrent=4)

        report = run(
            handle.connection, state, AlwaysApproveBackend(),
            client=client, repo="owner/repo",
        )
        assert report.merged == 0
        # Task untouched.
        row = handle.connection.execute(
            "SELECT status FROM task WHERE identifier='T0-1'"
        ).fetchone()
        assert row["status"] == "dispatched"
    finally:
        handle.connection.close()


def test_force_bypasses_adapter_gate(tmp_path):
    register_adapter(
        _Adapter(db_path_value=str(tmp_path / "oxi.db"), auto_merge_enabled=False)
    )
    handle = db.connect()
    try:
        _seed(handle.connection, "T0-1", 42)
        client = FakeGitHubClient()
        client.add_pr("owner/repo", _pr(42))
        state = EngineState(plan_tier="standard", max_concurrent=4)

        report = run(
            handle.connection, state, AlwaysApproveBackend(),
            client=client, repo="owner/repo", force=True,
        )
        assert report.merged == 1
    finally:
        handle.connection.close()


# ---------------------------------------------------------------------------
# Happy path: approve + merge
# ---------------------------------------------------------------------------


def test_approve_merges_and_transitions_task(env):
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _pr(42))

    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.considered == 1
    assert report.merged == 1
    row = env["conn"].execute(
        "SELECT status, merged_at FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "merged"
    assert row["merged_at"] is not None
    # gh merge was invoked.
    assert len(env["client"].merge_calls) == 1


def test_merge_uses_configured_method(env):
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _pr(42))

    run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo", merge_method="rebase",
    )
    _, _, method = env["client"].merge_calls[0]
    assert method == "rebase"


# ---------------------------------------------------------------------------
# Rejection path
# ---------------------------------------------------------------------------


def test_reject_transitions_task_to_failed(env):
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _pr(42))

    report = run(
        env["conn"], env["state"],
        AlwaysRejectBackend(reason="test weakened"),
        client=env["client"], repo="owner/repo",
    )
    assert report.critic_rejected == 1
    row = env["conn"].execute(
        "SELECT status, failure_reason FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["failure_reason"] == "critic_rejected"
    # Never tried to merge.
    assert len(env["client"].merge_calls) == 0


def test_reject_records_reason_in_event(env):
    task_id = _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _pr(42))

    run(
        env["conn"], env["state"],
        AlwaysRejectBackend(reason="commented out tests"),
        client=env["client"], repo="owner/repo",
    )
    ev = env["conn"].execute(
        "SELECT payload FROM event WHERE task_id=? AND kind='auto_merge_rejected'",
        (task_id,),
    ).fetchone()
    assert "commented out tests" in ev["payload"]


# ---------------------------------------------------------------------------
# Terminal-state discipline (anti-pattern #7)
# ---------------------------------------------------------------------------


def test_already_merged_pr_is_not_rejected(env):
    """If someone else (or a previous tick) merged the PR, auto_merge must
    recognize it as terminal success, not as a rejection.

    Prior orchestrators' bug: state=MERGED matched the same branch as
    CLOSED, and everything non-OPEN was rejected. Pollutes the dashboard
    reject counter and masks real rejections.
    """
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _pr(42, state=PRState.MERGED))

    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.already_merged == 1
    assert report.critic_rejected == 0
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "merged"
    # Critic wasn't invoked (no merge_call either, since it was already merged).
    assert len(env["client"].merge_calls) == 0


# ---------------------------------------------------------------------------
# Filter: only SUCCESS CI + OPEN state
# ---------------------------------------------------------------------------


def test_pending_ci_is_skipped(env):
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr(
        "owner/repo",
        _pr(42, check=PRCheckStatus.PENDING),
    )
    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.considered == 0
    assert report.merged == 0


def test_failing_ci_is_skipped(env):
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr(
        "owner/repo",
        _pr(42, check=PRCheckStatus.FAILURE),
    )
    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.considered == 0
    assert report.merged == 0


def test_closed_unmerged_pr_is_ignored(env):
    """Closed-without-merge PRs are pr_watcher's job, not auto_merge's."""
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr(
        "owner/repo",
        _pr(42, state=PRState.CLOSED),
    )
    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.considered == 0
    assert report.merged == 0
    assert report.already_merged == 0
    # Task untouched.
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "dispatched"


def test_tasks_without_pr_are_ignored(env):
    _seed(env["conn"], "T0-1", None)  # no pr_number
    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.considered == 0


def test_missing_pr_from_gh_is_skipped(env):
    """Task has pr_number, but gh returns None (deleted/permissions)."""
    _seed(env["conn"], "T0-1", 42)
    # No PR added to fake — get_pr returns None.
    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.considered == 0
    assert report.merged == 0


# ---------------------------------------------------------------------------
# Merge-call failure
# ---------------------------------------------------------------------------


def test_merge_call_failure_records_event(env):
    """If gh merge returns False, record the failure without state change."""
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _pr(42))
    # Override merge_pr to always fail.
    env["client"].merge_pr = lambda repo, num, method="squash": False

    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.merge_failed == 1
    assert report.merged == 0
    # Task stays dispatched — next tick retries.
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Critic receives correct input
# ---------------------------------------------------------------------------


def test_critic_receives_task_identifier_and_pr_number(env):
    captured: list[CriticInput] = []

    def reviewer(inp: CriticInput) -> CriticVerdict:
        captured.append(inp)
        return CriticVerdict(verdict=Verdict.APPROVE, reason="ok")

    _seed(env["conn"], "T0-7", 77)
    env["client"].add_pr("owner/repo", _pr(77))

    run(
        env["conn"], env["state"], FunctionCriticBackend(fn=reviewer),
        client=env["client"], repo="owner/repo",
    )
    assert len(captured) == 1
    inp = captured[0]
    assert inp.item.identifier == "T0-7"
    assert inp.pr_number == 77


# ---------------------------------------------------------------------------
# Mixed batch
# ---------------------------------------------------------------------------


def test_mixed_batch(env):
    """Approve one, reject one, skip-pending one, skip-already-merged one."""
    _seed(env["conn"], "T0-1", 11)  # will approve
    _seed(env["conn"], "T0-2", 12)  # will reject
    _seed(env["conn"], "T0-3", 13)  # pending CI
    _seed(env["conn"], "T0-4", 14)  # already merged

    env["client"].add_pr("owner/repo", _pr(11))
    env["client"].add_pr("owner/repo", _pr(12))
    env["client"].add_pr("owner/repo", _pr(13, check=PRCheckStatus.PENDING))
    env["client"].add_pr("owner/repo", _pr(14, state=PRState.MERGED))

    def reviewer(inp: CriticInput) -> CriticVerdict:
        if inp.pr_number == 11:
            return CriticVerdict(verdict=Verdict.APPROVE, reason="ok")
        return CriticVerdict(verdict=Verdict.REJECT, reason="nope")

    report = run(
        env["conn"], env["state"], FunctionCriticBackend(fn=reviewer),
        client=env["client"], repo="owner/repo",
    )
    assert report.considered == 2  # only 11 and 12 made it through filter
    assert report.merged == 1
    assert report.critic_rejected == 1
    assert report.already_merged == 1
