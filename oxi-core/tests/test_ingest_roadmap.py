"""Tests for oxi_core.v3.ingest_roadmap."""

from __future__ import annotations

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
from oxi_core.v3.ingest_roadmap import IngestReport, ingest


@dataclass
class _Adapter:
    db_path_value: str
    repo_root_value: str

    def naming(self): return NamingConfig()
    def paths(self):
        return PathsConfig(
            db_path=self.db_path_value,
            repo_root=self.repo_root_value,
        )
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
    (tmp_path / "roadmap.md").write_text(
        "## Tier 0\n\n"
        "**T0-1 · first**\n_one_\n\n"
        "**T0-2 · second**\n\n"
        "## Tier 1\n\n"
        "**T1-1 · third**\n_three_\n"
    )
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "root": tmp_path,
    }
    handle.connection.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_ingest_inserts_all_items(env):
    report = ingest(env["conn"])
    assert report.inserted == 3
    assert report.updated == 0
    assert report.total == 3
    rows = list(
        env["conn"].execute(
            "SELECT identifier, tier, title FROM front ORDER BY identifier"
        )
    )
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("T0-1", 0, "first"),
        ("T0-2", 0, "second"),
        ("T1-1", 1, "third"),
    ]


def test_ingest_is_idempotent(env):
    ingest(env["conn"])
    report2 = ingest(env["conn"])
    assert report2.inserted == 0
    assert report2.updated == 3
    count = env["conn"].execute(
        "SELECT COUNT(*) FROM front WHERE identifier IS NOT NULL"
    ).fetchone()[0]
    assert count == 3


# ---------------------------------------------------------------------------
# Upsert preserves last_seeded_at
# ---------------------------------------------------------------------------


def test_reingest_preserves_last_seeded_at(env):
    ingest(env["conn"])
    env["conn"].execute(
        "UPDATE front SET last_seeded_at = '2026-04-20 10:00:00' "
        "WHERE identifier = 'T0-1'"
    )
    env["conn"].commit()

    ingest(env["conn"])
    row = env["conn"].execute(
        "SELECT last_seeded_at FROM front WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row[0] == "2026-04-20 10:00:00"


# ---------------------------------------------------------------------------
# Updates tier + title on re-ingest
# ---------------------------------------------------------------------------


def test_reingest_updates_tier_when_roadmap_reorders(env):
    """If an item is promoted from tier 1 to tier 0, re-ingest reflects it."""
    ingest(env["conn"])
    # Rewrite roadmap with T1-1 moved to tier 0.
    (env["root"] / "roadmap.md").write_text(
        "## Tier 0\n\n"
        "**T0-1 · first**\n_one_\n\n"
        "**T1-1 · third**\n_three_\n"
    )
    ingest(env["conn"])
    row = env["conn"].execute(
        "SELECT tier FROM front WHERE identifier = 'T1-1'"
    ).fetchone()
    assert row[0] == 0


def test_reingest_updates_title(env):
    ingest(env["conn"])
    (env["root"] / "roadmap.md").write_text(
        "## Tier 0\n\n**T0-1 · renamed**\n"
    )
    ingest(env["conn"])
    row = env["conn"].execute(
        "SELECT title FROM front WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row[0] == "renamed"


# ---------------------------------------------------------------------------
# Missing roadmap
# ---------------------------------------------------------------------------


def test_missing_roadmap_raises(tmp_path: Path):
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    handle = db.connect()
    try:
        with pytest.raises(FileNotFoundError):
            ingest(handle.connection)
    finally:
        handle.connection.close()


# ---------------------------------------------------------------------------
# Ledger event
# ---------------------------------------------------------------------------


def test_ingest_emits_ledger_event(env):
    ingest(env["conn"])
    kinds = [r[0] for r in env["conn"].execute("SELECT kind FROM event")]
    assert "roadmap_ingested" in kinds


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


def test_report_dataclass_is_frozen():
    from dataclasses import FrozenInstanceError
    r = IngestReport(inserted=0, updated=0, total=0)
    with pytest.raises(FrozenInstanceError):
        r.inserted = 1  # type: ignore[misc]
