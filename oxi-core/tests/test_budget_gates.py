"""Tests for the budget gate in dispatch + auto_merge.

Confirms dispatch + auto_merge refuse to spend when ``budget_hard_stop``
is in the ledger. Works against the fake claude binary.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
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
from oxi_core.v3 import auto_merge, budget, dispatch  # noqa: E402
from oxi_core.v3.critic import AlwaysApproveBackend  # noqa: E402
from oxi_core.v3.engine_state import EngineState  # noqa: E402
from oxi_core.v3.github_client import (  # noqa: E402
    PRCheckStatus,
    PRState,
    PullRequest,
)

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"


@dataclass
class _Adapter:
    db_path_value: str
    worktree_root_value: str = "/tmp"
    auto_merge_enabled: bool = True

    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig(db_path=self.db_path_value)
    def budget(self):
        return BudgetCaps(
            daily_soft_warn=1.0, daily_hard_cap=5.0,
            per_task_opus=0.5, per_task_sonnet=0.2,
        )
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (
            DispatchHost(
                name="local", ssh_alias=None,
                max_concurrent=1, worktree_root=self.worktree_root_value,
            ),
        )
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self):
        return DispatchPolicy(auto_merge=self.auto_merge_enabled)


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _seed_over_cap(conn) -> None:
    """Create a task with $10 cost_usd → triggers HARD_STOP."""
    now = datetime.now(tz=UTC)
    conn.execute(
        "INSERT INTO task (identifier, tier, title, cost_usd, updated_at) "
        "VALUES ('T0-9', 0, 'already spent', 10.0, ?)",
        (_iso(now),),
    )
    conn.commit()


def _seed_planned(conn, identifier: str) -> None:
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES (?, 0, 't', 'planned')",
        (identifier,),
    )
    conn.commit()


def _seed_dispatched_with_pr(conn, identifier: str, pr_number: int) -> None:
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES (?, 0, 't', 'dispatched', ?)",
        (identifier, pr_number),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# dispatch gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_noop_when_hard_stopped(tmp_path: Path):
    from oxi_core.adapter import get_active_adapter

    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        worktree_root_value=str(tmp_path / "wt"),
    ))
    handle = db.connect()
    try:
        conn = handle.connection
        _seed_over_cap(conn)
        _seed_planned(conn, "T0-1")
        # Trigger HARD_STOP.
        budget.check(conn)
        state = EngineState(plan_tier="standard", max_concurrent=4)

        result = await dispatch.dispatch_one(
            conn=conn,
            adapter=get_active_adapter(),
            engine_state=state,
            repo_root=tmp_path,
            binary=str(FAKE_CLAUDE),
            extra_env={"FAKE_CLAUDE_SCENARIO": "happy"},
        )
        assert result is None
        # Task stayed planned.
        row = conn.execute(
            "SELECT status FROM task WHERE identifier = 'T0-1'"
        ).fetchone()
        assert row["status"] == "planned"
    finally:
        handle.connection.close()


# ---------------------------------------------------------------------------
# auto_merge gate
# ---------------------------------------------------------------------------


def test_auto_merge_noop_when_hard_stopped(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        conn = handle.connection
        _seed_over_cap(conn)
        _seed_dispatched_with_pr(conn, "T0-1", pr_number=42)
        budget.check(conn)  # fires HARD_STOP

        gh_client = FakeGitHubClient()
        gh_client.add_pr("owner/repo", PullRequest(
            number=42, title="pr", head_branch="feat/sa-t0-1",
            state=PRState.OPEN, check_status=PRCheckStatus.SUCCESS,
            mergeable=True,
        ))
        state = EngineState(plan_tier="standard", max_concurrent=4)

        report = auto_merge.run(
            conn, state, AlwaysApproveBackend(),
            client=gh_client, repo="owner/repo", force=True,
        )
        assert report.considered == 0
        assert report.merged == 0
        # PR was not merged.
        assert len(gh_client.merge_calls) == 0
    finally:
        handle.connection.close()


# ---------------------------------------------------------------------------
# Happy path — budget OK, both operate normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_proceeds_when_budget_ok(tmp_path: Path):
    """Sanity: with no spend recorded, dispatch still works."""
    # Need a real git repo for worktree_provision.
    import subprocess

    from oxi_core.adapter import get_active_adapter
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main"],
        cwd=upstream, check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    for cmd in [
        ["git", "init", "--initial-branch=main"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
    ]:
        subprocess.run(cmd, cwd=seed, check=True, capture_output=True)
    (seed / "README.md").write_text("x")
    for cmd in [
        ["git", "add", "."], ["git", "commit", "-m", "i"],
        ["git", "remote", "add", "origin", str(upstream)],
        ["git", "push", "-u", "origin", "main"],
    ]:
        subprocess.run(cmd, cwd=seed, check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(upstream), str(clone)],
        cwd=tmp_path, check=True, capture_output=True,
    )
    for cmd in [
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
    ]:
        subprocess.run(cmd, cwd=clone, check=True, capture_output=True)

    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        worktree_root_value=str(tmp_path / "wt"),
    ))
    handle = db.connect()
    try:
        conn = handle.connection
        _seed_planned(conn, "T0-1")
        state = EngineState(plan_tier="standard", max_concurrent=4)

        result = await dispatch.dispatch_one(
            conn=conn,
            adapter=get_active_adapter(),
            engine_state=state,
            repo_root=clone,
            binary=str(FAKE_CLAUDE),
            extra_env={"FAKE_CLAUDE_SCENARIO": "happy"},
        )
        assert result is not None
        assert result.classification.value == "success"
    finally:
        handle.connection.close()
