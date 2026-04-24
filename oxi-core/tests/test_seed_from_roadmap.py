"""Tests for oxi_core.v3.seed_from_roadmap."""

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
from oxi_core.v3.engine_state import EngineState
from oxi_core.v3.ingest_roadmap import ingest
from oxi_core.v3.seed_from_roadmap import seed


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
        "**T0-1 · first**\n\n"
        "**T0-2 · second**\n\n"
        "## Tier 1\n\n"
        "**T1-1 · third**\n\n"
        "**T1-2 · fourth**\n"
    )
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    handle = db.connect()
    ingest(handle.connection)
    yield {
        "conn": handle.connection,
        "root": tmp_path,
        "state": EngineState(plan_tier="standard", max_concurrent=4),
    }
    handle.connection.close()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Target threshold
# ---------------------------------------------------------------------------


def test_seed_noop_when_planned_count_at_target(env):
    """If planned count already meets target, no seed happens."""
    # Manually mark all fronts as recently seeded so they're not eligible.
    # Then insert 10 planned tasks so threshold is met.
    for i in range(10):
        env["conn"].execute(
            "INSERT INTO task (identifier, tier, title, status) "
            "VALUES (?, 0, 't', 'planned')",
            (f"FILLER-{i}",),
        )
    env["conn"].commit()

    report = seed(env["conn"], env["state"], target_planned_count=10)
    assert report.seeded == 0


def test_seed_inserts_one_task_under_threshold(env):
    report = seed(env["conn"], env["state"], target_planned_count=10)
    assert report.seeded == 1
    assert report.planned_before == 0
    assert report.planned_after == 1


def test_seed_respects_batch_size(env):
    report = seed(
        env["conn"], env["state"],
        target_planned_count=10, batch_size=3,
    )
    assert report.seeded == 3
    assert report.planned_after == 3


def test_seed_never_exceeds_target(env):
    """batch_size=99 still caps at target - planned_before slots."""
    report = seed(
        env["conn"], env["state"],
        target_planned_count=2, batch_size=99,
    )
    assert report.seeded == 2
    assert report.planned_after == 2


# ---------------------------------------------------------------------------
# Tier priority
# ---------------------------------------------------------------------------


def test_seed_picks_tier_0_first(env):
    seed(env["conn"], env["state"], batch_size=1)
    row = env["conn"].execute(
        "SELECT identifier, tier FROM task WHERE status = 'planned' "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    assert row["tier"] == 0
    assert row["identifier"] in ("T0-1", "T0-2")


def test_seed_exhausts_tier_0_before_tier_1(env):
    # Seed up to 2 — fills both tier-0 fronts.
    seed(env["conn"], env["state"], batch_size=2)
    tiers = [
        r[0] for r in env["conn"].execute(
            "SELECT tier FROM task WHERE status = 'planned' ORDER BY id"
        )
    ]
    assert tiers == [0, 0]


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_seed_skips_fronts_within_cooldown(env):
    now = datetime.now(tz=UTC)
    recent = _iso(now - timedelta(hours=1))
    # Mark every tier-0 front as just-seeded.
    env["conn"].execute(
        "UPDATE front SET last_seeded_at = ? WHERE tier = 0",
        (recent,),
    )
    env["conn"].commit()

    report = seed(env["conn"], env["state"], batch_size=10, now=now)
    # Only tier-1 fronts are eligible.
    tiers = {
        r[0] for r in env["conn"].execute(
            "SELECT tier FROM task WHERE status = 'planned'"
        )
    }
    assert 0 not in tiers
    assert report.eligible == 2  # two tier-1 fronts


def test_seed_admits_fronts_past_cooldown(env):
    now = datetime.now(tz=UTC)
    long_ago = _iso(now - timedelta(days=2))
    env["conn"].execute(
        "UPDATE front SET last_seeded_at = ? WHERE tier = 0",
        (long_ago,),
    )
    env["conn"].commit()

    seed(env["conn"], env["state"], batch_size=1, now=now)
    row = env["conn"].execute(
        "SELECT identifier FROM task WHERE status = 'planned' LIMIT 1"
    ).fetchone()
    assert row["identifier"] in ("T0-1", "T0-2")


# ---------------------------------------------------------------------------
# Never duplicate
# ---------------------------------------------------------------------------


def test_seed_skips_identifiers_already_in_task(env):
    # Pre-seed T0-1 manually.
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T0-1', 0, 'first', 'dispatched')"
    )
    env["conn"].commit()

    seed(env["conn"], env["state"], batch_size=10)
    # T0-1 should not appear twice.
    count = env["conn"].execute(
        "SELECT COUNT(*) FROM task WHERE identifier = 'T0-1'"
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Engine stop
# ---------------------------------------------------------------------------


def test_seed_noop_when_engine_stopping(env):
    env["state"].request_stop()
    report = seed(env["conn"], env["state"])
    assert report.seeded == 0
    count = env["conn"].execute(
        "SELECT COUNT(*) FROM task"
    ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Ledger + last_seeded_at stamping
# ---------------------------------------------------------------------------


def test_seed_emits_front_seeded_event(env):
    seed(env["conn"], env["state"], batch_size=1)
    kinds = [r[0] for r in env["conn"].execute("SELECT kind FROM event")]
    assert "front_seeded" in kinds
    assert "seed_pass_complete" in kinds


def test_seed_stamps_last_seeded_at(env):
    seed(env["conn"], env["state"], batch_size=1)
    seeded_identifier = env["conn"].execute(
        "SELECT identifier FROM task WHERE status = 'planned' LIMIT 1"
    ).fetchone()[0]
    row = env["conn"].execute(
        "SELECT last_seeded_at FROM front WHERE identifier = ?",
        (seeded_identifier,),
    ).fetchone()
    assert row[0] is not None


# ---------------------------------------------------------------------------
# No eligible fronts
# ---------------------------------------------------------------------------


def test_seed_noop_when_no_eligible_fronts(env):
    """All fronts already seeded and in cooldown → no-op."""
    # Put every identifier into task so existing-set blocks seeding.
    for ident in ("T0-1", "T0-2", "T1-1", "T1-2"):
        env["conn"].execute(
            "INSERT INTO task (identifier, tier, title, status) "
            "VALUES (?, 0, 't', 'dispatched')",
            (ident,),
        )
    env["conn"].commit()

    report = seed(env["conn"], env["state"], batch_size=10)
    assert report.seeded == 0
    assert report.eligible == 0
