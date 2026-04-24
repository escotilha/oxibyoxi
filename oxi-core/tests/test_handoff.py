"""Tests for oxi_core.v3.handoff."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from oxi_core.v3 import handoff


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
    yield
    clear_adapter()


@pytest.fixture
def env(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    yield {
        "conn": handle.connection,
        "worktree": worktree,
        "tmp": tmp_path,
    }
    handle.connection.close()


def _seed_task(conn, identifier: str = "T0-1") -> int:
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES (?, 0, 'do thing', 'dispatched')",
        (identifier,),
    )
    conn.commit()
    return cur.lastrowid


def _seed_events(conn, task_id: int, kinds: list[str]) -> None:
    for kind in kinds:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, kind, json.dumps({"k": "v"})),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# write_snapshot
# ---------------------------------------------------------------------------


def test_write_creates_snapshot_file(env):
    task_id = _seed_task(env["conn"])
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    report = handoff.write_snapshot(
        env["conn"],
        worktree=env["worktree"],
        task_id=task_id,
        identifier="T0-1",
        content="# resume\n\nbody here\n",
        now=now,
    )
    assert report.path.exists()
    assert report.path.name == "resume-T0-1-20260424T120000Z.md"
    assert "body here" in report.path.read_text()


def test_write_emits_ledger_event(env):
    task_id = _seed_task(env["conn"])
    handoff.write_snapshot(
        env["conn"],
        worktree=env["worktree"],
        task_id=task_id,
        identifier="T0-1",
        content="x",
    )
    kinds = [
        r[0] for r in env["conn"].execute(
            "SELECT kind FROM event WHERE task_id = ?", (task_id,)
        )
    ]
    assert "handoff_written" in kinds


def test_write_raises_on_missing_worktree(env):
    task_id = _seed_task(env["conn"])
    with pytest.raises(FileNotFoundError):
        handoff.write_snapshot(
            env["conn"],
            worktree=env["tmp"] / "does-not-exist",
            task_id=task_id,
            identifier="T0-1",
            content="x",
        )


def test_write_rejects_keep_below_one(env):
    task_id = _seed_task(env["conn"])
    with pytest.raises(ValueError, match=">=[ ]*1"):
        handoff.write_snapshot(
            env["conn"],
            worktree=env["worktree"],
            task_id=task_id,
            identifier="T0-1",
            content="x",
            keep=0,
        )


# ---------------------------------------------------------------------------
# Rolling retention
# ---------------------------------------------------------------------------


def test_prune_keeps_most_recent_three(env):
    task_id = _seed_task(env["conn"])
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    for i in range(5):
        handoff.write_snapshot(
            env["conn"],
            worktree=env["worktree"],
            task_id=task_id,
            identifier="T0-1",
            content=f"snap {i}",
            keep=3,
            now=now + timedelta(minutes=i),
        )
    remaining = sorted(
        p for p in env["worktree"].iterdir()
        if p.name.startswith("resume-")
    )
    assert len(remaining) == 3
    # Newest three minutes (2, 3, 4).
    names = [p.name for p in remaining]
    assert any("120200Z" in n for n in names)
    assert any("120400Z" in n for n in names)


def test_prune_across_different_identifiers(env):
    """Retention is per-worktree, not per-identifier."""
    task_id = _seed_task(env["conn"])
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    for i, ident in enumerate(["T0-1", "T0-2", "T0-1", "T0-3"]):
        handoff.write_snapshot(
            env["conn"],
            worktree=env["worktree"],
            task_id=task_id,
            identifier=ident,
            content=f"snap {i}",
            keep=2,
            now=now + timedelta(minutes=i),
        )
    remaining = sorted(
        p for p in env["worktree"].iterdir()
        if p.name.startswith("resume-")
    )
    assert len(remaining) == 2


def test_pruned_paths_reported(env):
    task_id = _seed_task(env["conn"])
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    # Write 3 with keep=2 — the third call prunes the first.
    r1 = handoff.write_snapshot(
        env["conn"], worktree=env["worktree"], task_id=task_id,
        identifier="T0-1", content="1", keep=2,
        now=now,
    )
    assert r1.pruned == ()
    handoff.write_snapshot(
        env["conn"], worktree=env["worktree"], task_id=task_id,
        identifier="T0-1", content="2", keep=2,
        now=now + timedelta(minutes=1),
    )
    r3 = handoff.write_snapshot(
        env["conn"], worktree=env["worktree"], task_id=task_id,
        identifier="T0-1", content="3", keep=2,
        now=now + timedelta(minutes=2),
    )
    assert len(r3.pruned) == 1
    # The oldest one (the first write) was pruned.
    assert r1.path in r3.pruned


def test_prune_does_not_touch_unrelated_files(env):
    task_id = _seed_task(env["conn"])
    (env["worktree"] / "README.md").write_text("hello")
    (env["worktree"] / "random.txt").write_text("x")
    handoff.write_snapshot(
        env["conn"], worktree=env["worktree"], task_id=task_id,
        identifier="T0-1", content="c", keep=1,
    )
    assert (env["worktree"] / "README.md").exists()
    assert (env["worktree"] / "random.txt").exists()


# ---------------------------------------------------------------------------
# read_latest
# ---------------------------------------------------------------------------


def test_read_latest_returns_most_recent(env):
    task_id = _seed_task(env["conn"])
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    handoff.write_snapshot(
        env["conn"], worktree=env["worktree"], task_id=task_id,
        identifier="T0-1", content="older", now=now,
    )
    handoff.write_snapshot(
        env["conn"], worktree=env["worktree"], task_id=task_id,
        identifier="T0-1", content="newer",
        now=now + timedelta(minutes=5),
    )
    body = handoff.read_latest(env["worktree"])
    assert body == "newer"


def test_read_latest_returns_none_when_no_snapshots(env):
    assert handoff.read_latest(env["worktree"]) is None


def test_read_latest_returns_none_for_missing_worktree(env):
    assert handoff.read_latest(env["tmp"] / "nope") is None


# ---------------------------------------------------------------------------
# build_snapshot
# ---------------------------------------------------------------------------


def test_build_snapshot_includes_identity(env):
    task_id = _seed_task(env["conn"], "T2-5")
    _seed_events(env["conn"], task_id,
                 ["dispatch_started", "pr_observed"])
    content = handoff.build_snapshot(
        env["conn"],
        task_id=task_id,
        identifier="T2-5",
        title="do thing",
        branch="feat/sa-t2-5",
    )
    assert "T2-5" in content
    assert "do thing" in content
    assert "`feat/sa-t2-5`" in content
    assert "dispatch_started" in content
    assert "pr_observed" in content


def test_build_snapshot_handles_no_events(env):
    task_id = _seed_task(env["conn"])
    content = handoff.build_snapshot(
        env["conn"],
        task_id=task_id,
        identifier="T0-1",
        title="t",
        branch="feat/x",
    )
    assert "no events yet" in content


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


def test_report_is_frozen(env):
    from dataclasses import FrozenInstanceError
    task_id = _seed_task(env["conn"])
    report = handoff.write_snapshot(
        env["conn"], worktree=env["worktree"], task_id=task_id,
        identifier="T0-1", content="c",
    )
    with pytest.raises(FrozenInstanceError):
        report.path = env["tmp"]  # type: ignore[misc]
