"""Tests for repeat-failure escalation in oxi_core.v3.dispatch.

Covers:
1. _count_recent_terminal_failures helper — pure DB unit tests.
2. dispatch_one integration — verifies that a task with 3 prior
   dispatch_failed events gets the DECOMPOSE_FIRST_NOTE prepended and
   max_turns=120 on its next invocation.
"""

from __future__ import annotations

import json
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
    DECOMPOSE_FIRST_FAILURE_THRESHOLD,
    ESCALATED_MAX_TURNS,
    _count_recent_terminal_failures,
    dispatch_one,
)
from oxi_core.v3.dispatch_invoke import Classification, DispatchInvocation, DispatchResult
from oxi_core.v3.engine_state import EngineState
from oxi_core.v3.ledger_events import LedgerEvent

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"


# ---------------------------------------------------------------------------
# Minimal fake adapter (copied from test_dispatch.py so this file is
# self-contained and does not import from test_dispatch).
# ---------------------------------------------------------------------------


@dataclass
class _FakeAdapter:
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
    subprocess.run(["git", "add", "README.md"], cwd=seed, check=True, capture_output=True)
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


def _seed_task(conn, *, identifier: str = "T0-1", tier: int = 0, title: str = "do a thing") -> int:
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, subtitle) VALUES (?, ?, ?, ?)",
        (identifier, tier, title, ""),
    )
    conn.commit()
    return cur.lastrowid


def _insert_event(conn, task_id: int, kind: str) -> None:
    """Insert a bare event row for testing."""
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
        (task_id, kind, json.dumps({})),
    )
    conn.commit()


def _with_fake(scenario: str) -> dict[str, str]:
    return {"FAKE_CLAUDE_SCENARIO": scenario}


# ---------------------------------------------------------------------------
# Unit tests for _count_recent_terminal_failures
# ---------------------------------------------------------------------------


def test_count_terminal_failures_three_failures(environment):
    """3 dispatch_failed events → returns 3."""
    conn = environment["conn"]
    task_id = _seed_task(conn)

    for _ in range(3):
        _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)

    assert _count_recent_terminal_failures(conn, task_id) == 3


def test_count_terminal_failures_chain_break(environment):
    """2 fails → 1 success → 2 fails → returns 2 (only recent streak)."""
    conn = environment["conn"]
    task_id = _seed_task(conn)

    # Oldest events first (IDs ascend in insertion order).
    _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)
    _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)
    _insert_event(conn, task_id, LedgerEvent.DISPATCH_SUCCEEDED)
    _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)
    _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)

    # Walking in reverse order: 2 failures, then a success stops the scan.
    assert _count_recent_terminal_failures(conn, task_id) == 2


def test_count_terminal_failures_zero_on_fresh_task(environment):
    """No events → 0."""
    conn = environment["conn"]
    task_id = _seed_task(conn)
    assert _count_recent_terminal_failures(conn, task_id) == 0


def test_count_terminal_failures_retryable_not_counted(environment):
    """dispatch_retryable events are NOT counted — only dispatch_failed."""
    conn = environment["conn"]
    task_id = _seed_task(conn)

    _insert_event(conn, task_id, LedgerEvent.DISPATCH_RETRYABLE)
    _insert_event(conn, task_id, LedgerEvent.DISPATCH_RETRYABLE)
    _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)

    assert _count_recent_terminal_failures(conn, task_id) == 1


def test_count_terminal_failures_single_success_breaks_chain(environment):
    """3 fails → 1 success → 0 new fails → returns 0."""
    conn = environment["conn"]
    task_id = _seed_task(conn)

    for _ in range(3):
        _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)
    _insert_event(conn, task_id, LedgerEvent.DISPATCH_SUCCEEDED)

    assert _count_recent_terminal_failures(conn, task_id) == 0


# ---------------------------------------------------------------------------
# Integration test: dispatch_one escalates on 3 prior fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_one_escalates_after_three_failures(environment, monkeypatch):
    """A task with 3 prior dispatch_failed events gets decompose-first treatment.

    Verifies:
    - The prompt passed to the invocation contains DECOMPOSE_FIRST_NOTE text.
    - max_turns == ESCALATED_MAX_TURNS (120).
    """
    env = environment
    conn = env["conn"]
    task_id = _seed_task(conn, identifier="T0-esc")

    # Pre-seed 3 dispatch_failed events to simulate 3 prior failures.
    # Also insert a dispatch_started before each failure (mirrors the real
    # dispatch flow) so the event sequence is realistic.
    for _ in range(3):
        _insert_event(conn, task_id, LedgerEvent.DISPATCH_STARTED)
        _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)

    # Reset the task to planned so dispatch_one picks it up.
    conn.execute("UPDATE task SET status = 'planned' WHERE id = ?", (task_id,))
    conn.commit()

    # Capture the DispatchInvocation that dispatch_one builds.
    captured: list[DispatchInvocation] = []

    from oxi_core.v3 import dispatch as dispatch_mod

    async def capturing_invoke(inv: DispatchInvocation) -> DispatchResult:
        captured.append(inv)
        # Return a happy result so dispatch_one completes cleanly.
        return DispatchResult(
            classification=Classification.SUCCESS,
            exit_code=0,
            session_id=inv.session_id,
        )

    monkeypatch.setattr(dispatch_mod, "invoke", capturing_invoke)

    await dispatch_one(
        conn=conn,
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("happy"),
    )

    assert len(captured) == 1, "Expected exactly one invocation"
    inv = captured[0]

    # The note must be in the prompt (with identifier substituted).
    assert "T0-esc" in inv.prompt, (
        "DECOMPOSE_FIRST_NOTE should have {identifier} substituted to T0-esc"
    )
    # Check a distinctive phrase from the note.
    assert "plan only — needs split" in inv.prompt, (
        "DECOMPOSE_FIRST_NOTE text not found in prompt"
    )

    # max_turns must be escalated.
    assert inv.max_turns == ESCALATED_MAX_TURNS, (
        f"Expected max_turns={ESCALATED_MAX_TURNS}, got {inv.max_turns}"
    )


@pytest.mark.asyncio
async def test_dispatch_one_no_escalation_below_threshold(environment, monkeypatch):
    """A task with only 2 prior failures does NOT get escalated."""
    env = environment
    conn = env["conn"]
    task_id = _seed_task(conn, identifier="T0-nesc")

    for _ in range(DECOMPOSE_FIRST_FAILURE_THRESHOLD - 1):
        _insert_event(conn, task_id, LedgerEvent.DISPATCH_FAILED)

    conn.execute("UPDATE task SET status = 'planned' WHERE id = ?", (task_id,))
    conn.commit()

    captured: list[DispatchInvocation] = []

    from oxi_core.v3 import dispatch as dispatch_mod

    async def capturing_invoke(inv: DispatchInvocation) -> DispatchResult:
        captured.append(inv)
        return DispatchResult(
            classification=Classification.SUCCESS,
            exit_code=0,
            session_id=inv.session_id,
        )

    monkeypatch.setattr(dispatch_mod, "invoke", capturing_invoke)

    await dispatch_one(
        conn=conn,
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_with_fake("happy"),
    )

    assert len(captured) == 1
    inv = captured[0]

    # No escalation note in prompt.
    assert "plan only — needs split" not in inv.prompt

    # Normal max_turns (60), not escalated.
    assert inv.max_turns == 60
