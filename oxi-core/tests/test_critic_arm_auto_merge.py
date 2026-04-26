"""Tests for T2-44: critic APPROVE arms GitHub auto-merge.

Verifies that ``auto_merge.run()`` calls ``enable_auto_merge`` on every
critic-approved PR, that it is idempotent, and that the ledger events and
report counters accurately reflect the outcome.

This file focuses exclusively on the arming behaviour added in T2-44. The
broader auto_merge happy/reject paths live in ``test_auto_merge.py``.
"""

from __future__ import annotations

import json
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
from oxi_core.v3.critic import AlwaysApproveBackend, AlwaysRejectBackend  # noqa: E402
from oxi_core.v3.engine_state import EngineState  # noqa: E402
from oxi_core.v3.github_client import PRCheckStatus, PRState, PullRequest  # noqa: E402


# ---------------------------------------------------------------------------
# Adapter + fixtures (mirrors test_auto_merge.py)
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


def _open_pr(
    number: int,
    head: str = "feat/oxi-t0-1",
    *,
    check: PRCheckStatus = PRCheckStatus.SUCCESS,
) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"pr {number}",
        head_branch=head,
        state=PRState.OPEN,
        check_status=check,
        mergeable=True,
    )


# ---------------------------------------------------------------------------
# Core: arming happens on APPROVE
# ---------------------------------------------------------------------------


def test_approve_arms_auto_merge(env):
    """Critic APPROVE → enable_auto_merge is called before merge_pr."""
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _open_pr(42))

    run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )

    # enable_auto_merge was called.
    assert len(env["client"].enable_auto_merge_calls) == 1
    repo, pr_num, method = env["client"].enable_auto_merge_calls[0]
    assert repo == "owner/repo"
    assert pr_num == 42
    assert method == "squash"


def test_arm_uses_configured_merge_method(env):
    """enable_auto_merge is called with the same method as merge_pr."""
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _open_pr(42))

    run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
        merge_method="rebase",
    )

    _, _, method = env["client"].enable_auto_merge_calls[0]
    assert method == "rebase"
    # merge_pr should also use the same method.
    _, _, merge_method = env["client"].merge_calls[0]
    assert merge_method == "rebase"


def test_reject_does_not_arm_auto_merge(env):
    """Critic REJECT → enable_auto_merge must NOT be called."""
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _open_pr(42))

    run(
        env["conn"], env["state"], AlwaysRejectBackend(),
        client=env["client"], repo="owner/repo",
    )

    assert len(env["client"].enable_auto_merge_calls) == 0
    assert len(env["client"].merge_calls) == 0


def test_approve_records_auto_merge_armed_event(env):
    """A ledger event ``auto_merge_armed`` is written on successful arm."""
    task_id = _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _open_pr(42))

    run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )

    ev = env["conn"].execute(
        "SELECT payload FROM event WHERE task_id=? AND kind='auto_merge_armed'",
        (task_id,),
    ).fetchone()
    assert ev is not None
    payload = json.loads(ev["payload"])
    assert payload["pr_number"] == 42


def test_approve_report_counts_auto_merge_armed(env):
    """AutoMergeReport.auto_merge_armed is incremented when arming succeeds."""
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _open_pr(42))

    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )
    assert report.auto_merge_armed == 1
    assert report.auto_merge_arm_failed == 0


# ---------------------------------------------------------------------------
# Idempotency: already-armed PRs no-op cleanly
# ---------------------------------------------------------------------------


def test_already_armed_pr_is_idempotent(env):
    """enable_auto_merge on an already-armed PR returns True (no-op from gh).

    The FakeGitHubClient reflects ``auto_merge_arm_succeeds=True`` even
    when the PR already has ``auto_merge_armed=True``; the fake models
    GitHub's idempotent behaviour.
    """
    _seed(env["conn"], "T0-1", 42)
    # Seed the PR as already armed.
    env["client"].add_pr(
        "owner/repo",
        PullRequest(
            number=42, title="pr 42", head_branch="feat/oxi-t0-1",
            state=PRState.OPEN, check_status=PRCheckStatus.SUCCESS,
            mergeable=True, auto_merge_armed=True,
        ),
    )

    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )

    # Arming was attempted (idempotent call).
    assert len(env["client"].enable_auto_merge_calls) == 1
    # Counted as armed (not failed) since the call succeeded.
    assert report.auto_merge_armed == 1
    assert report.auto_merge_arm_failed == 0


# ---------------------------------------------------------------------------
# Arm failure: fallback to direct merge
# ---------------------------------------------------------------------------


def test_arm_failure_falls_back_to_direct_merge(env):
    """If enable_auto_merge fails, the pass still attempts a direct merge."""
    _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _open_pr(42))
    env["client"].auto_merge_arm_succeeds = False  # simulate disabled repo setting

    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )

    # Arm was attempted.
    assert len(env["client"].enable_auto_merge_calls) == 1
    # Report correctly tallies the failure.
    assert report.auto_merge_arm_failed == 1
    assert report.auto_merge_armed == 0
    # Direct merge still proceeded.
    assert len(env["client"].merge_calls) == 1
    assert report.merged == 1


def test_arm_failure_records_ledger_event(env):
    """A ledger event ``auto_merge_arm_failed`` is written when arming fails."""
    task_id = _seed(env["conn"], "T0-1", 42)
    env["client"].add_pr("owner/repo", _open_pr(42))
    env["client"].auto_merge_arm_succeeds = False

    run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )

    ev = env["conn"].execute(
        "SELECT payload FROM event WHERE task_id=? AND kind='auto_merge_arm_failed'",
        (task_id,),
    ).fetchone()
    assert ev is not None
    payload = json.loads(ev["payload"])
    assert payload["pr_number"] == 42


# ---------------------------------------------------------------------------
# Batch: multiple PRs, each approved, each gets armed
# ---------------------------------------------------------------------------


def test_multiple_approved_prs_all_armed(env):
    """Each approved PR in a batch gets enable_auto_merge called."""
    for identifier, pr_num in [("T0-1", 11), ("T0-2", 22), ("T0-3", 33)]:
        _seed(env["conn"], identifier, pr_num)
        env["client"].add_pr("owner/repo", _open_pr(pr_num))

    report = run(
        env["conn"], env["state"], AlwaysApproveBackend(),
        client=env["client"], repo="owner/repo",
    )

    assert report.considered == 3
    assert report.merged == 3
    assert report.auto_merge_armed == 3
    assert len(env["client"].enable_auto_merge_calls) == 3
    armed_prs = {call[1] for call in env["client"].enable_auto_merge_calls}
    assert armed_prs == {11, 22, 33}


# ---------------------------------------------------------------------------
# GitHub PullRequest.auto_merge_armed field parsing
# ---------------------------------------------------------------------------


def test_pull_request_auto_merge_armed_default_false():
    """PullRequest defaults to auto_merge_armed=False for backward compat."""
    pr = PullRequest(
        number=1, title="t", head_branch="feat/x",
        state=PRState.OPEN, check_status=PRCheckStatus.SUCCESS,
        mergeable=True,
    )
    assert pr.auto_merge_armed is False


def test_pull_request_auto_merge_armed_explicit_true():
    pr = PullRequest(
        number=1, title="t", head_branch="feat/x",
        state=PRState.OPEN, check_status=PRCheckStatus.SUCCESS,
        mergeable=True, auto_merge_armed=True,
    )
    assert pr.auto_merge_armed is True


# ---------------------------------------------------------------------------
# GhCliClient.enable_auto_merge: unit test of flag assembly
# ---------------------------------------------------------------------------


def test_gh_cli_enable_auto_merge_assembles_correct_argv():
    """Verify the exact argv GhCliClient passes to gh for each merge method."""
    import subprocess

    from oxi_core.v3.github_client import GhCliClient

    calls: list[list[str]] = []

    class _FakeRun:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **_kw):
        calls.append(argv)
        return _FakeRun()

    client = GhCliClient()
    original = subprocess.run
    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        result = client.enable_auto_merge("owner/repo", 7, method="squash")
    finally:
        subprocess.run = original

    assert result is True
    assert len(calls) == 1
    argv = calls[0]
    assert "pr" in argv
    assert "merge" in argv
    assert "--auto" in argv
    assert "--delete-branch" in argv
    assert "--squash" in argv
    assert "7" in argv
    assert "--repo" in argv
    assert "owner/repo" in argv


def test_gh_cli_enable_auto_merge_returns_false_on_error():
    """GitHubError from gh is caught and returns False."""
    import subprocess

    from oxi_core.v3.github_client import GhCliClient

    class _FailedRun:
        returncode = 1
        stdout = ""
        stderr = "auto-merge not enabled for this repository"

    def fake_run(argv, **_kw):
        return _FailedRun()

    client = GhCliClient()
    original = subprocess.run
    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        result = client.enable_auto_merge("owner/repo", 7)
    finally:
        subprocess.run = original

    assert result is False


def test_gh_cli_enable_auto_merge_invalid_method():
    """Passing an unknown method raises ValueError (same as merge_pr)."""
    from oxi_core.v3.github_client import GhCliClient

    client = GhCliClient()
    with pytest.raises(ValueError, match="unknown merge method"):
        client.enable_auto_merge("owner/repo", 1, method="force-push")
