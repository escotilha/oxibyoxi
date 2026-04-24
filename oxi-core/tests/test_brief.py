"""Tests for oxi_core.v3.brief — daily summary generation."""

from __future__ import annotations

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
from oxi_core.v3.brief import Brief, generate


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
def conn(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield handle.connection
    handle.connection.close()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _seed(
    conn,
    identifier: str,
    status: str,
    *,
    title: str = "t",
    updated_at: datetime | None = None,
    merged_at: datetime | None = None,
    failure_reason: str | None = None,
    cost_usd: float = 0.0,
) -> None:
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, updated_at, "
        "merged_at, failure_reason, cost_usd) "
        "VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
        (
            identifier, title, status,
            _iso(updated_at) if updated_at else _iso(datetime.now(tz=UTC)),
            _iso(merged_at) if merged_at else None,
            failure_reason,
            cost_usd,
        ),
    )
    conn.commit()


def test_empty_db_produces_empty_brief(conn):
    b = generate(conn)
    assert isinstance(b, Brief)
    assert b.status_counts == {}
    assert b.total_cost_usd == 0.0
    assert b.recent_merges == []
    assert b.recent_failures == []


def test_status_counts(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", "merged", updated_at=now)
    _seed(conn, "T0-2", "merged", updated_at=now)
    _seed(conn, "T0-3", "dispatched", updated_at=now)
    _seed(conn, "T0-4", "failed", updated_at=now)

    b = generate(conn, now=now)
    assert b.status_counts["merged"] == 2
    assert b.status_counts["dispatched"] == 1
    assert b.status_counts["failed"] == 1


def test_out_of_window_tasks_excluded(conn):
    now = datetime.now(tz=UTC)
    old = now - timedelta(hours=48)
    recent = now - timedelta(hours=2)
    _seed(conn, "T0-1", "merged", updated_at=recent)
    _seed(conn, "T0-2", "merged", updated_at=old)

    b = generate(conn, window_hours=24, now=now)
    assert b.status_counts.get("merged", 0) == 1


def test_spend_sums_costs(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", "merged", updated_at=now, cost_usd=0.05)
    _seed(conn, "T0-2", "merged", updated_at=now, cost_usd=0.02)
    b = generate(conn, now=now)
    assert b.total_cost_usd == pytest.approx(0.07)


def test_recent_merges_ordered_by_merged_at_desc(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", "merged", title="first",
          updated_at=now, merged_at=now - timedelta(hours=2))
    _seed(conn, "T0-2", "merged", title="second",
          updated_at=now, merged_at=now - timedelta(hours=1))
    _seed(conn, "T0-3", "merged", title="third",
          updated_at=now, merged_at=now)
    b = generate(conn, now=now)
    assert [(i, t) for i, t in b.recent_merges][:3] == [
        ("T0-3", "third"), ("T0-2", "second"), ("T0-1", "first"),
    ]


def test_recent_failures_include_reason(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", "failed", updated_at=now,
          failure_reason="critic_rejected")
    _seed(conn, "T0-2", "abandoned", updated_at=now,
          failure_reason="stale_no_progress")
    b = generate(conn, now=now)
    reasons = {r for _, _, r in b.recent_failures}
    assert "critic_rejected" in reasons
    assert "stale_no_progress" in reasons


def test_render_produces_readable_markdown(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", "merged", title="example merge",
          updated_at=now, merged_at=now)
    b = generate(conn, now=now)
    text = b.render()
    assert "# oxi brief" in text
    assert "## Status counts" in text
    assert "example merge" in text


def test_render_shows_none_placeholders_on_empty(conn):
    b = generate(conn)
    text = b.render()
    assert "_(no tasks)_" in text
    assert "_(none)_" in text
