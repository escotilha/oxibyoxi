"""Tests for oxi_core.v3.branch_janitor.

Uses real git repos in tmp dirs (same pattern as worktree_provision).
Verifies the ancestry check correctly distinguishes merged from
unmerged branches.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from fake_github import FakeGitHubClient  # noqa: E402

from oxi_core.adapter import (  # noqa: E402
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3 import branch_janitor  # noqa: E402
from oxi_core.v3.github_client import (  # noqa: E402
    PRCheckStatus,
    PRState,
    PullRequest,
)


@dataclass
class _Adapter:
    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig()
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/", "fix/")
    def dispatch_hosts(self):
        return (
            DispatchHost(
                name="local", ssh_alias=None,
                max_concurrent=1, worktree_root="/tmp",
            ),
        )
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    register_adapter(_Adapter())
    yield
    clear_adapter()


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path):
    """Build a bare upstream + a local clone. Return (upstream, clone)."""
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _run(["git", "init", "--bare", "--initial-branch=main"], cwd=upstream)

    clone = tmp_path / "clone"
    clone.mkdir()
    _run(["git", "init", "--initial-branch=main"], cwd=clone)
    _run(["git", "config", "user.email", "t@e.com"], cwd=clone)
    _run(["git", "config", "user.name", "t"], cwd=clone)
    (clone / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=clone)
    _run(["git", "commit", "-m", "initial"], cwd=clone)
    _run(["git", "remote", "add", "origin", str(upstream)], cwd=clone)
    _run(["git", "push", "-u", "origin", "main"], cwd=clone)
    return {"upstream": upstream, "clone": clone}


def _push_branch(
    clone: Path, branch: str, *, merged_to_main: bool,
) -> None:
    """Push a branch. If merged_to_main=True, also merge it into main.

    After a squash-merge the branch's tip becomes reachable from main,
    so the ancestry check treats it as safe to delete.
    """
    # Create branch from main.
    _run(["git", "checkout", "-b", branch, "main"], cwd=clone)
    commit_file = clone / f"{branch.replace('/', '-')}.txt"
    commit_file.write_text("x")
    _run(["git", "add", str(commit_file.name)], cwd=clone)
    _run(["git", "commit", "-m", f"work on {branch}"], cwd=clone)
    _run(["git", "push", "-u", "origin", branch], cwd=clone)
    _run(["git", "checkout", "main"], cwd=clone)

    if merged_to_main:
        _run(["git", "merge", "--no-ff", branch, "-m", f"merge {branch}"],
             cwd=clone)
        _run(["git", "push", "origin", "main"], cwd=clone)


# ---------------------------------------------------------------------------
# Empty case
# ---------------------------------------------------------------------------


def test_no_remote_branches_except_main(repo):
    client = FakeGitHubClient()
    report = branch_janitor.run(
        repo_root=repo["clone"],
        client=client,
        repo="owner/repo",
        default_branch="main",
    )
    assert report.considered == 0
    assert report.deleted == 0


# ---------------------------------------------------------------------------
# Merged branch — gets deleted
# ---------------------------------------------------------------------------


def test_deletes_merged_branch_with_no_open_pr(repo):
    client = FakeGitHubClient()
    _push_branch(repo["clone"], "feat/sa-done", merged_to_main=True)

    report = branch_janitor.run(
        repo_root=repo["clone"],
        client=client,
        repo="owner/repo",
        default_branch="main",
    )
    assert report.considered == 1
    assert report.deleted == 1
    assert "feat/sa-done" in report.deleted_branches

    # Remote branch gone.
    _run(["git", "fetch", "--prune", "origin"], cwd=repo["clone"])
    proc = subprocess.run(
        ["git", "branch", "-r"],
        cwd=str(repo["clone"]), capture_output=True, text=True, check=True,
    )
    assert "feat/sa-done" not in proc.stdout


# ---------------------------------------------------------------------------
# Unmerged branch — preserved
# ---------------------------------------------------------------------------


def test_preserves_unmerged_branch(repo):
    client = FakeGitHubClient()
    _push_branch(repo["clone"], "feat/sa-in-progress", merged_to_main=False)

    report = branch_janitor.run(
        repo_root=repo["clone"],
        client=client,
        repo="owner/repo",
        default_branch="main",
    )
    assert report.considered == 1
    assert report.deleted == 0
    assert report.skipped_unmerged_commits == 1


# ---------------------------------------------------------------------------
# Open PR — preserved
# ---------------------------------------------------------------------------


def test_preserves_branch_with_open_pr(repo):
    client = FakeGitHubClient()
    _push_branch(repo["clone"], "feat/sa-with-pr", merged_to_main=True)
    client.add_pr("owner/repo", PullRequest(
        number=42, title="pr", head_branch="feat/sa-with-pr",
        state=PRState.OPEN, check_status=PRCheckStatus.SUCCESS,
        mergeable=True,
    ))

    report = branch_janitor.run(
        repo_root=repo["clone"],
        client=client,
        repo="owner/repo",
        default_branch="main",
    )
    assert report.considered == 1
    assert report.deleted == 0
    assert report.skipped_has_open_pr == 1


# ---------------------------------------------------------------------------
# Non-oxi-prefix branch — ignored
# ---------------------------------------------------------------------------


def test_ignores_human_prefix_branches(repo):
    client = FakeGitHubClient()
    _push_branch(repo["clone"], "random/human-work", merged_to_main=True)

    report = branch_janitor.run(
        repo_root=repo["clone"],
        client=client,
        repo="owner/repo",
        default_branch="main",
    )
    # Not oxi-prefixed — doesn't count as 'considered'.
    assert report.considered == 0
    assert report.skipped_not_oxi_prefix == 1


def test_accepts_custom_prefix_tuple(repo):
    client = FakeGitHubClient()
    _push_branch(repo["clone"], "bot/sa-thing", merged_to_main=True)

    report = branch_janitor.run(
        repo_root=repo["clone"],
        client=client,
        repo="owner/repo",
        default_branch="main",
        branch_prefixes=("bot/",),
    )
    assert report.considered == 1
    assert report.deleted == 1


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def test_dry_run_reports_without_deleting(repo):
    client = FakeGitHubClient()
    _push_branch(repo["clone"], "feat/sa-done", merged_to_main=True)

    report = branch_janitor.run(
        repo_root=repo["clone"],
        client=client,
        repo="owner/repo",
        default_branch="main",
        delete=False,
    )
    # Report says deleted=1 but the branch is still on the remote.
    assert report.deleted == 1
    assert report.deleted_branches == ("feat/sa-done",)

    _run(["git", "fetch", "--prune", "origin"], cwd=repo["clone"])
    proc = subprocess.run(
        ["git", "branch", "-r"],
        cwd=str(repo["clone"]), capture_output=True, text=True, check=True,
    )
    assert "feat/sa-done" in proc.stdout


# ---------------------------------------------------------------------------
# Mixed batch
# ---------------------------------------------------------------------------


def test_mixed_batch(repo):
    client = FakeGitHubClient()
    _push_branch(repo["clone"], "feat/sa-done1", merged_to_main=True)
    _push_branch(repo["clone"], "feat/sa-done2", merged_to_main=True)
    _push_branch(repo["clone"], "feat/sa-ongoing", merged_to_main=False)
    _push_branch(repo["clone"], "feat/sa-reviewed", merged_to_main=True)
    client.add_pr("owner/repo", PullRequest(
        number=99, title="still open", head_branch="feat/sa-reviewed",
        state=PRState.OPEN, check_status=PRCheckStatus.SUCCESS,
        mergeable=True,
    ))
    _push_branch(repo["clone"], "chore/0424-cleanup", merged_to_main=True)
    # Unknown prefix.
    _push_branch(repo["clone"], "human/sandbox", merged_to_main=True)

    report = branch_janitor.run(
        repo_root=repo["clone"],
        client=client,
        repo="owner/repo",
        default_branch="main",
    )
    # 4 oxi-prefixed considered (feat/sa-done1, done2, ongoing, reviewed);
    # chore/ not in default prefix tuple so counted as not-oxi.
    # human/sandbox also not-oxi.
    assert report.considered == 4
    # Two merged + no-open-PR → deleted.
    assert report.deleted == 2
    assert set(report.deleted_branches) == {"feat/sa-done1", "feat/sa-done2"}
    assert report.skipped_unmerged_commits == 1
    assert report.skipped_has_open_pr == 1
    assert report.skipped_not_oxi_prefix == 2


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


def test_report_is_frozen():
    from dataclasses import FrozenInstanceError
    r = branch_janitor.JanitorReport(
        considered=0, deleted=0,
        skipped_not_oxi_prefix=0, skipped_has_open_pr=0,
        skipped_unmerged_commits=0,
    )
    with pytest.raises(FrozenInstanceError):
        r.deleted = 1  # type: ignore[misc]
