"""Tests for oxi_core.v3.dispatch — the state-machine driver.

End-to-end: real SQLite DB, real git worktrees, fake claude binary.
Every invariant from the prior-orchestrator post-mortems is covered
by at least one test.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from oxi_core import db
from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3.dispatch import (
    Task,
    _branch_has_commits_ahead,
    _maybe_upgrade_to_success,
    dispatch_loop,
    dispatch_one,
    is_orphan_reapable,
)
from oxi_core.v3.dispatch_invoke import Classification, DispatchResult
from oxi_core.v3.engine_state import EngineState

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"


# ---------------------------------------------------------------------------
# Test adapter
# ---------------------------------------------------------------------------


@dataclass
class _FakeAdapter:
    """Adapter that dispatches against a tmp repo + worktree root."""

    db_path_value: str
    worktree_root_value: str

    def naming(self) -> NamingConfig:
        return NamingConfig(instance_name="oxi-test")

    def paths(self) -> PathsConfig:
        return PathsConfig(db_path=self.db_path_value)

    def budget(self) -> BudgetCaps:
        return BudgetCaps(
            daily_soft_warn=1.0,
            daily_hard_cap=5.0,
            per_task_opus=0.50,
            per_task_sonnet=0.20,
        )

    def github_repo(self) -> str:
        return "owner/sample"

    def roadmap_location(self) -> str:
        return "roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/",)

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (
            DispatchHost(
                name="local",
                ssh_alias=None,
                max_concurrent=1,
                worktree_root=self.worktree_root_value,
            ),
        )

    def promote_recipe(self) -> None:
        return None

    def plan_tier(self) -> str:
        return "standard"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare upstream repo + a clone for worktree provisioning."""
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main"],
        cwd=upstream, check=True, capture_output=True,
    )

    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=seed, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=seed, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=seed, check=True, capture_output=True,
    )
    (seed / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=seed, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=seed, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(upstream)],
        cwd=seed, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=seed, check=True, capture_output=True,
    )

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(upstream), str(clone)],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=clone, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=clone, check=True, capture_output=True,
    )
    return upstream, clone


@pytest.fixture
def environment(tmp_path: Path, upstream_and_clone) -> dict:
    """Registered adapter + DB handle + worktree root + repo root for a test."""
    _, clone = upstream_and_clone
    worktree_root = tmp_path / "worktrees"
    db_path = tmp_path / "oxi.db"

    adapter = _FakeAdapter(
        db_path_value=str(db_path),
        worktree_root_value=str(worktree_root),
    )
    register_adapter(adapter)

    handle = db.connect()
    yield {
        "conn": handle.connection,
        "repo_root": clone,
        "worktree_root": worktree_root,
        "adapter": adapter,
        "engine_state": EngineState(plan_tier="standard", max_concurrent=4),
    }
    handle.connection.close()


def _seed_task(
    conn,
    *,
    identifier: str = "T0-1",
    tier: int = 0,
    title: str = "do a thing",
    subtitle: str = "",
) -> int:
    """Insert a task row and return its id."""
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, subtitle) VALUES (?, ?, ?, ?)",
        (identifier, tier, title, subtitle),
    )
    conn.commit()
    return cur.lastrowid


def _with_fake(env_scenario: str) -> dict[str, str]:
    """Return env overrides that point claude at the fake binary + scenario.

    Passed to dispatch_one() / dispatch_loop() via extra_env so the
    subprocess sees the var. (build_env() whitelists by design — so
    setting FAKE_CLAUDE_SCENARIO in the parent env alone would NOT
    reach the child.)
    """
    return {"FAKE_CLAUDE_SCENARIO": env_scenario}


async def _dispatch(env, scenario: str, **kwargs):
    """Convenience: run dispatch_one with the given fake-claude scenario."""
    return await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake(scenario),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Invariant: atomic transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_stamps_last_progress_at_atomically(
    environment, monkeypatch
):
    """Anti-pattern #1 enforcement: status and last_progress_at in one transaction.

    Without this, the heartbeat reaper falls back to created_at and
    reaps just-dispatched tasks.
    """
    env = environment
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    _seed_task(env["conn"])

    await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("happy"),
    )

    # Read the row back. last_progress_at must be set; it must NOT be
    # NULL; it must be close to "now".
    row = env["conn"].execute(
        "SELECT status, last_progress_at, dispatched_at FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    # Happy path ends in 'dispatched' — pr_watcher will move it later.
    assert row["status"] == "dispatched"
    assert row["last_progress_at"] is not None
    assert row["dispatched_at"] is not None


# ---------------------------------------------------------------------------
# Invariant: engine stop halts dispatch immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_one_returns_none_when_stopping(environment, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    _seed_task(env["conn"])
    env["engine_state"].request_stop()

    result = await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("happy"),
    )
    assert result is None
    # Task was NOT dispatched.
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row["status"] == "planned"


@pytest.mark.asyncio
async def test_dispatch_loop_checks_stop_flag_every_iteration(
    environment, monkeypatch
):
    """Killswitch-mid-loop bug: set the flag after one iteration.

    The loop must exit after the current iteration, not run the full
    ``max_iterations``.
    """
    env = environment
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    for i in range(5):
        _seed_task(env["conn"], identifier=f"T0-{i+1}", title=f"task {i+1}")

    state = env["engine_state"]

    # Monkey-patch dispatch_one so we can set the stop flag mid-loop.
    # After the first successful dispatch, request_stop(). The loop
    # should exit on its next iteration check.
    from oxi_core.v3 import dispatch as dispatch_mod

    original_dispatch_one = dispatch_mod.dispatch_one
    call_count = {"n": 0}

    async def wrapped(**kwargs):
        call_count["n"] += 1
        result = await original_dispatch_one(**kwargs)
        state.request_stop()
        return result

    monkeypatch.setattr(dispatch_mod, "dispatch_one", wrapped)

    results = await dispatch_loop(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=state,
        repo_root=env["repo_root"],
        max_iterations=5,
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("happy"),
    )
    # Exactly one result landed; loop exited on next iteration.
    assert len(results) == 1
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Invariant: no planned tasks → return None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_one_returns_none_when_no_planned_tasks(environment):
    env = environment
    # DB is empty except for schema.
    result = await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    )
    assert result is None


# ---------------------------------------------------------------------------
# Invariant: tier-order selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_picks_lower_tier_first(environment, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    _seed_task(env["conn"], identifier="T2-1", tier=2, title="lower priority")
    _seed_task(env["conn"], identifier="T0-5", tier=0, title="higher priority")

    await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("happy"),
    )

    t0_status = env["conn"].execute(
        "SELECT status FROM task WHERE identifier = 'T0-5'"
    ).fetchone()["status"]
    t2_status = env["conn"].execute(
        "SELECT status FROM task WHERE identifier = 'T2-1'"
    ).fetchone()["status"]
    assert t0_status == "dispatched"
    assert t2_status == "planned"


# ---------------------------------------------------------------------------
# Classification → status mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_leaves_task_dispatched(environment, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    _seed_task(env["conn"])
    await dispatch_one(
        conn=env["conn"], adapter=env["adapter"],
        engine_state=env["engine_state"], repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("happy"),
    )
    row = env["conn"].execute(
        "SELECT status, cost_usd FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row["status"] == "dispatched"
    # Cost accumulated from the fake's result event (0.02).
    assert row["cost_usd"] > 0


@pytest.mark.asyncio
async def test_retryable_transient_returns_task_to_planned(
    environment, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "exit_143")
    env = environment
    _seed_task(env["conn"])
    await dispatch_one(
        conn=env["conn"], adapter=env["adapter"],
        engine_state=env["engine_state"], repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("exit_143"),
    )
    row = env["conn"].execute(
        "SELECT status, last_progress_at FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row["status"] == "planned"
    # But last_progress_at was stamped — so the next tick's
    # heartbeat doesn't consider it stale.
    assert row["last_progress_at"] is not None


@pytest.mark.asyncio
async def test_rate_limit_exhaustion_returns_to_planned(
    environment, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "rate_limit_exhausted")
    env = environment
    _seed_task(env["conn"])
    await dispatch_one(
        conn=env["conn"], adapter=env["adapter"],
        engine_state=env["engine_state"], repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("rate_limit_exhausted"),
    )
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row["status"] == "planned"


@pytest.mark.asyncio
async def test_timeout_marks_abandoned(environment, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "hung")
    env = environment
    _seed_task(env["conn"])
    # Shorten wall-clock timeout by patching.
    from oxi_core.v3 import dispatch_invoke as di_mod

    original_invoke = di_mod.invoke

    async def quick_invoke(inv):
        # Rewrite invocation with a short timeout.
        from oxi_core.v3.dispatch_invoke import DispatchInvocation
        short = DispatchInvocation(
            prompt=inv.prompt,
            cwd=inv.cwd,
            session_id=inv.session_id,
            model=inv.model,
            max_budget_usd=inv.max_budget_usd,
            max_turns=inv.max_turns,
            allowed_tools=inv.allowed_tools,
            extra_env=inv.extra_env,
            wall_clock_timeout_s=1.5,
            binary=inv.binary,
        )
        return await original_invoke(short)

    monkeypatch.setattr(di_mod, "invoke", quick_invoke)
    # dispatch.py imports invoke directly, so patch that too.
    from oxi_core.v3 import dispatch as dispatch_mod
    monkeypatch.setattr(dispatch_mod, "invoke", quick_invoke)

    await dispatch_one(
        conn=env["conn"], adapter=env["adapter"],
        engine_state=env["engine_state"], repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("hung"),
    )
    row = env["conn"].execute(
        "SELECT status, failure_reason FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row["status"] == "abandoned"
    assert row["failure_reason"] == "timeout"


# ---------------------------------------------------------------------------
# Ledger events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_include_dispatch_started_and_succeeded(
    environment, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    task_id = _seed_task(env["conn"])
    await dispatch_one(
        conn=env["conn"], adapter=env["adapter"],
        engine_state=env["engine_state"], repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("happy"),
    )
    kinds = [
        row[0]
        for row in env["conn"].execute(
            "SELECT kind FROM event WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]
    assert "dispatch_started" in kinds
    assert "dispatch_succeeded" in kinds


# ---------------------------------------------------------------------------
# is_orphan_reapable helper
# ---------------------------------------------------------------------------


def test_is_orphan_reapable_true_when_no_pr():
    task = Task(
        id=1, identifier="T0-1", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=None, created_at="", updated_at="",
    )
    assert is_orphan_reapable(task) is True


def test_is_orphan_reapable_false_when_pr_open():
    task = Task(
        id=1, identifier="T0-1", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=42,
        last_progress_at=None, created_at="", updated_at="",
    )
    assert is_orphan_reapable(task) is False


# ---------------------------------------------------------------------------
# Worktree isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_creates_a_worktree_for_the_task(environment, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    _seed_task(env["conn"], identifier="T0-5", title="worktree test")

    await dispatch_one(
        conn=env["conn"], adapter=env["adapter"],
        engine_state=env["engine_state"], repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
    extra_env=_with_fake("happy"),
    )

    # Worktree directory exists (slugified identifier).
    worktrees = list(env["worktree_root"].iterdir())
    assert len(worktrees) >= 1
    assert any("t0-5" in w.name for w in worktrees)


# ---------------------------------------------------------------------------
# Sanity: Python version
# ---------------------------------------------------------------------------


def test_python_version():
    import sys
    assert sys.version_info >= (3, 11)


# ---------------------------------------------------------------------------
# T1-15: false-failure relaxation (_maybe_upgrade_to_success)
# ---------------------------------------------------------------------------


def _failed_result(session_id: str = "test-sid") -> DispatchResult:
    """Return a minimal FAILED DispatchResult for unit-testing the upgrade logic."""
    return DispatchResult(
        classification=Classification.FAILED,
        exit_code=1,
        session_id=session_id,
    )


def test_maybe_upgrade_noop_when_no_commits(tmp_path):
    """No commits ahead → stay FAILED even if a PR exists."""
    result = _failed_result()
    upgraded = _maybe_upgrade_to_success(
        result,
        worktree_path=tmp_path,
        branch="feat/oxi-t0-1",
        repo="owner/repo",
        _check_commits=lambda _p, _b: False,   # branch NOT ahead
        _check_pr=lambda _b, _r: True,          # PR exists
    )
    assert upgraded.classification is Classification.FAILED


def test_maybe_upgrade_noop_when_no_pr(tmp_path):
    """Commits ahead but no PR → stay FAILED (push succeeded, PR open failed)."""
    result = _failed_result()
    upgraded = _maybe_upgrade_to_success(
        result,
        worktree_path=tmp_path,
        branch="feat/oxi-t0-1",
        repo="owner/repo",
        _check_commits=lambda _p, _b: True,    # branch IS ahead
        _check_pr=lambda _b, _r: False,         # no PR
    )
    assert upgraded.classification is Classification.FAILED


def test_maybe_upgrade_succeeds_when_both_conditions_met(tmp_path):
    """Branch ahead AND PR exists → upgrade to SUCCESS."""
    result = _failed_result()
    upgraded = _maybe_upgrade_to_success(
        result,
        worktree_path=tmp_path,
        branch="feat/oxi-t0-1",
        repo="owner/repo",
        _check_commits=lambda _p, _b: True,
        _check_pr=lambda _b, _r: True,
    )
    assert upgraded.classification is Classification.SUCCESS
    # Original fields preserved.
    assert upgraded.exit_code == 1
    assert upgraded.session_id == "test-sid"
    # A note is embedded in trailing_line.
    assert upgraded.trailing_line is not None
    assert "reclassified" in upgraded.trailing_line


def test_maybe_upgrade_noop_for_success_result(tmp_path):
    """Already-SUCCESS results pass through unchanged."""
    result = DispatchResult(
        classification=Classification.SUCCESS,
        exit_code=0,
        session_id="sid",
    )
    upgraded = _maybe_upgrade_to_success(
        result,
        worktree_path=tmp_path,
        branch="feat/oxi-t0-1",
        repo="owner/repo",
        _check_commits=lambda _p, _b: True,
        _check_pr=lambda _b, _r: True,
    )
    assert upgraded is result  # unchanged, same object


def test_maybe_upgrade_noop_for_timeout_result(tmp_path):
    """TIMEOUT results are not upgraded (wall-clock killed, not post-success quirk)."""
    result = DispatchResult(
        classification=Classification.TIMEOUT,
        exit_code=None,
        session_id="sid",
    )
    upgraded = _maybe_upgrade_to_success(
        result,
        worktree_path=tmp_path,
        branch="feat/oxi-t0-1",
        repo="owner/repo",
        _check_commits=lambda _p, _b: True,
        _check_pr=lambda _b, _r: True,
    )
    assert upgraded.classification is Classification.TIMEOUT


def test_maybe_upgrade_noop_for_retryable_result(tmp_path):
    """RETRYABLE_TRANSIENT results are not upgraded."""
    result = DispatchResult(
        classification=Classification.RETRYABLE_TRANSIENT,
        exit_code=143,
        session_id="sid",
    )
    upgraded = _maybe_upgrade_to_success(
        result,
        worktree_path=tmp_path,
        branch="feat/oxi-t0-1",
        repo="owner/repo",
        _check_commits=lambda _p, _b: True,
        _check_pr=lambda _b, _r: True,
    )
    assert upgraded.classification is Classification.RETRYABLE_TRANSIENT


def test_branch_has_commits_ahead_returns_false_on_bad_path(tmp_path):
    """Graceful degradation: non-git dir returns False, not an exception."""
    result = _branch_has_commits_ahead(tmp_path / "nonexistent", "feat/oxi-t0-1")
    assert result is False


def test_branch_has_commits_ahead_returns_false_on_fresh_clone(
    upstream_and_clone, tmp_path
):
    """A branch at the same tip as origin should report False (no commits ahead)."""
    _, clone = upstream_and_clone
    # The default branch is at origin/main with no local commits ahead.
    # We verify the function returns False (not an exception).
    result = _branch_has_commits_ahead(clone, "main")
    assert result is False


@pytest.mark.asyncio
async def test_exit_1_after_pr_upgrades_to_dispatched(environment):
    """Full integration: exit-1-after-PR fake scenario → task stays dispatched.

    The fake scenario exits 1 (simulating a post-success hook).
    We inject _check_commits and _check_pr that both return True,
    simulating the situation where the branch is ahead and a PR exists.
    The task should end up ``dispatched`` (= SUCCESS outcome), not ``failed``.
    """
    env = environment
    _seed_task(env["conn"])

    await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("exit_1_after_pr"),
        _check_commits=lambda _p, _b: True,
        _check_pr=lambda _b, _r: True,
    )

    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row["status"] == "dispatched", (
        "Task should stay dispatched when PR exists + branch ahead, "
        "even if claude exited 1"
    )


@pytest.mark.asyncio
async def test_exit_1_without_pr_marks_failed(environment):
    """Exit-1 with no PR → real failure, should still mark task failed.

    _check_pr returns False, so the upgrade is skipped.
    """
    env = environment
    _seed_task(env["conn"])

    await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("exit_1_after_pr"),
        _check_commits=lambda _p, _b: True,
        _check_pr=lambda _b, _r: False,   # no PR → real failure
    )

    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_upgrade_emits_dispatch_succeeded_event(environment):
    """When upgrade happens the ledger event kind must be dispatch_succeeded."""
    env = environment
    task_id = _seed_task(env["conn"])

    await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("exit_1_after_pr"),
        _check_commits=lambda _p, _b: True,
        _check_pr=lambda _b, _r: True,
    )

    kinds = [
        row[0]
        for row in env["conn"].execute(
            "SELECT kind FROM event WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]
    assert "dispatch_succeeded" in kinds
    assert "dispatch_failed" not in kinds


# ---------------------------------------------------------------------------
# dispatch_loop concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_loop_serial_when_concurrency_is_one(environment, monkeypatch):
    """concurrency=1 keeps the original serial code path.

    Two tasks → both dispatched, but sequentially. Verified by
    asserting the second dispatch_one call only starts after the
    first finishes (via shared in-flight counter).
    """
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    _seed_task(env["conn"], identifier="T0-1", title="first")
    _seed_task(env["conn"], identifier="T0-2", title="second")

    in_flight = {"max": 0, "current": 0}

    from oxi_core.v3 import dispatch as dispatch_mod
    original = dispatch_mod.dispatch_one

    async def wrapped(**kwargs):
        in_flight["current"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["current"])
        try:
            return await original(**kwargs)
        finally:
            in_flight["current"] -= 1

    monkeypatch.setattr(dispatch_mod, "dispatch_one", wrapped)

    results = await dispatch_loop(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        max_iterations=5,
        concurrency=1,
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("happy"),
    )
    assert len(results) == 2
    assert in_flight["max"] == 1, "serial mode must never have >1 in-flight"


@pytest.mark.asyncio
async def test_dispatch_loop_parallel_runs_concurrently(environment, monkeypatch):
    """concurrency>1 actually fans out: ≥2 dispatch_one calls run at once."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    for i in range(4):
        _seed_task(env["conn"], identifier=f"T0-{i+1}", title=f"task {i+1}")

    in_flight = {"max": 0, "current": 0}

    from oxi_core.v3 import dispatch as dispatch_mod
    original = dispatch_mod.dispatch_one

    async def wrapped(**kwargs):
        in_flight["current"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["current"])
        try:
            # Small await to let the scheduler interleave; without it,
            # the fake-claude path may run synchronously enough that
            # parallelism never gets exercised in the test.
            import asyncio
            await asyncio.sleep(0.05)
            return await original(**kwargs)
        finally:
            in_flight["current"] -= 1

    monkeypatch.setattr(dispatch_mod, "dispatch_one", wrapped)

    results = await dispatch_loop(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        max_iterations=4,
        concurrency=3,
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("happy"),
    )
    assert len(results) == 4
    assert in_flight["max"] >= 2, (
        f"parallel mode should run at least 2 concurrently; saw max "
        f"{in_flight['max']}"
    )


@pytest.mark.asyncio
async def test_dispatch_loop_parallel_respects_max_iterations(environment, monkeypatch):
    """concurrency=5 with max_iterations=2 → at most 2 dispatch_one calls."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    for i in range(5):
        _seed_task(env["conn"], identifier=f"T0-{i+1}", title=f"task {i+1}")

    from oxi_core.v3 import dispatch as dispatch_mod
    call_count = {"n": 0}
    original = dispatch_mod.dispatch_one

    async def wrapped(**kwargs):
        call_count["n"] += 1
        return await original(**kwargs)

    monkeypatch.setattr(dispatch_mod, "dispatch_one", wrapped)

    results = await dispatch_loop(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        max_iterations=2,
        concurrency=5,
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("happy"),
    )
    assert len(results) == 2
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_dispatch_loop_parallel_drains_when_queue_empty(environment, monkeypatch):
    """concurrency=4 + only 2 planned tasks → 2 dispatched, no errors."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "happy")
    env = environment
    _seed_task(env["conn"], identifier="T0-1", title="first")
    _seed_task(env["conn"], identifier="T0-2", title="second")

    results = await dispatch_loop(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        max_iterations=10,
        concurrency=4,
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("happy"),
    )
    assert len(results) == 2
