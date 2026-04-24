"""Tests for oxi_core.v3.deadman — silent-failure detection."""

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
from oxi_core.v3 import deadman
from oxi_core.v3.engine_state import EngineState
from oxi_core.v3.notification import Level, RecordingBackend


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
    yield {
        "conn": handle.connection,
        "state": EngineState(plan_tier="standard", max_concurrent=4),
        "notifier": RecordingBackend(),
    }
    handle.connection.close()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _seed_dispatch_event(conn, when: datetime) -> None:
    """Insert a dispatch_started event with a specific timestamp.

    Uses explicit created_at (bypassing the default) so tests can
    simulate prior dispatches.
    """
    conn.execute(
        "INSERT INTO event (task_id, kind, payload, created_at) "
        "VALUES (NULL, 'dispatch_started', '{}', ?)",
        (_iso(when),),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# No prior dispatch → no alert
# ---------------------------------------------------------------------------


def test_no_dispatch_history_does_not_fire(env):
    report = deadman.run(
        env["conn"], env["state"], env["notifier"],
    )
    assert report.level is None
    assert report.notified is False
    assert env["notifier"].count == 0


# ---------------------------------------------------------------------------
# Below threshold → no alert
# ---------------------------------------------------------------------------


def test_recent_dispatch_does_not_fire(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(minutes=5))
    report = deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, now=now,
    )
    assert report.level is None
    assert env["notifier"].count == 0


# ---------------------------------------------------------------------------
# Threshold crossings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("silence,expected_level", [
    (45, Level.INFO),
    (120, Level.WARN),
    (210, Level.ALERT),
    (400, Level.DEAD),
])
def test_threshold_crossings(env, silence, expected_level):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(minutes=silence))
    report = deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, warn_minutes=90,
        alert_minutes=180, dead_minutes=360,
        now=now,
    )
    assert report.level is expected_level
    assert report.notified is True
    assert env["notifier"].last().level is expected_level


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_does_not_renotify_same_level_in_window(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(minutes=45))

    # First pass: fires INFO.
    first = deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, now=now,
    )
    assert first.notified is True

    # Second pass, same window: does NOT fire again.
    second = deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30,
        notification_rate_limit_minutes=90,
        now=now + timedelta(minutes=10),
    )
    assert second.level is Level.INFO
    assert second.notified is False
    assert env["notifier"].count == 1


def test_fires_new_level_even_within_rate_limit(env):
    """Rate-limit is per-level. Escalating to a higher level fires."""
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(minutes=45))

    deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, warn_minutes=90,
        now=now,
    )
    assert env["notifier"].count == 1

    # Silence grows; now at WARN threshold. Should fire despite
    # INFO having fired recently.
    later = now + timedelta(minutes=60)  # silence now 105m
    report = deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, warn_minutes=90,
        now=later,
    )
    assert report.level is Level.WARN
    assert report.notified is True
    assert env["notifier"].count == 2


# ---------------------------------------------------------------------------
# Engine stop
# ---------------------------------------------------------------------------


def test_engine_stopping_does_not_fire(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(hours=10))
    env["state"].request_stop()

    report = deadman.run(
        env["conn"], env["state"], env["notifier"], now=now,
    )
    assert report.notified is False
    assert report.level is None
    assert env["notifier"].count == 0


# ---------------------------------------------------------------------------
# Budget hard-stop as additional context
# ---------------------------------------------------------------------------


def test_budget_hard_stop_tag_added(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(hours=2))
    # Seed a budget_hard_stop event today.
    env["conn"].execute(
        "INSERT INTO event (task_id, kind, payload, created_at) "
        "VALUES (NULL, 'budget_hard_stop', '{}', ?)",
        (_iso(now - timedelta(hours=1)),),
    )
    env["conn"].commit()

    deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, warn_minutes=90,
        now=now,
    )
    n = env["notifier"].last()
    assert "budget" in n.tags
    assert "budget hard-stop is active" in n.body.lower()


def test_budget_hard_stop_not_reflected_if_not_today(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(hours=2))
    # Budget hard-stop from yesterday — not within today's UTC window.
    env["conn"].execute(
        "INSERT INTO event (task_id, kind, payload, created_at) "
        "VALUES (NULL, 'budget_hard_stop', '{}', ?)",
        (_iso(now - timedelta(days=2)),),
    )
    env["conn"].commit()

    deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, warn_minutes=90,
        now=now,
    )
    n = env["notifier"].last()
    assert "budget" not in n.tags


# ---------------------------------------------------------------------------
# Ledger event emission
# ---------------------------------------------------------------------------


def test_notification_emits_ledger_event(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(minutes=45))
    deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, now=now,
    )
    kinds = [r[0] for r in env["conn"].execute("SELECT kind FROM event")]
    assert "deadman_info" in kinds


def test_ledger_event_includes_thresholds(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    _seed_dispatch_event(env["conn"], now - timedelta(minutes=45))
    deadman.run(
        env["conn"], env["state"], env["notifier"],
        info_minutes=30, warn_minutes=90,
        alert_minutes=180, dead_minutes=360,
        now=now,
    )
    row = env["conn"].execute(
        "SELECT payload FROM event WHERE kind = 'deadman_info'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["thresholds"] == {
        "info": 30, "warn": 90, "alert": 180, "dead": 360,
    }
    assert payload["last_dispatch_minutes_ago"] == pytest.approx(45, rel=0.1)


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


def test_report_is_frozen():
    from dataclasses import FrozenInstanceError
    r = deadman.DeadmanReport(
        level=None, last_dispatch_minutes_ago=None,
        budget_hard_stop_active=False, notified=False,
    )
    with pytest.raises(FrozenInstanceError):
        r.notified = True  # type: ignore[misc]
