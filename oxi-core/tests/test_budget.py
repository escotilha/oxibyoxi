"""Tests for oxi_core.v3.budget — daily-cap enforcement."""

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
from oxi_core.v3 import budget


@dataclass
class _Adapter:
    db_path_value: str
    soft: float = 1.0
    hard: float = 5.0

    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig(db_path=self.db_path_value)
    def budget(self):
        return BudgetCaps(
            daily_soft_warn=self.soft,
            daily_hard_cap=self.hard,
            per_task_opus=0.5,
            per_task_sonnet=0.2,
        )
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
def conn(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield handle.connection
    handle.connection.close()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _seed(conn, identifier: str, cost: float, updated_at: datetime) -> None:
    conn.execute(
        "INSERT INTO task (identifier, tier, title, cost_usd, updated_at) "
        "VALUES (?, 0, 't', ?, ?)",
        (identifier, cost, _iso(updated_at)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Verdict thresholds
# ---------------------------------------------------------------------------


def test_empty_db_is_ok(conn):
    result = budget.check(conn)
    assert result.verdict is budget.Verdict.OK
    assert result.today_spend_usd == 0.0


def test_below_soft_warn_is_ok(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 0.50, now)
    result = budget.check(conn, now=now)
    assert result.verdict is budget.Verdict.OK
    assert result.today_spend_usd == pytest.approx(0.50)


def test_at_soft_warn_is_warn(conn):
    """Crossing into [soft, hard) yields WARN."""
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 1.0, now)  # exactly at soft=1.0
    result = budget.check(conn, now=now)
    assert result.verdict is budget.Verdict.WARN


def test_at_hard_cap_is_hard_stop(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 5.0, now)  # at hard=5.0
    result = budget.check(conn, now=now)
    assert result.verdict is budget.Verdict.HARD_STOP


def test_over_hard_cap_is_hard_stop(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 10.0, now)
    assert budget.check(conn, now=now).verdict is budget.Verdict.HARD_STOP


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def test_hard_stop_emits_ledger_event(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 10.0, now)
    budget.check(conn, now=now)
    kinds = [r[0] for r in conn.execute("SELECT kind FROM event")]
    assert "budget_hard_stop" in kinds


def test_hard_stop_emits_event_once_per_day(conn):
    """Subsequent checks the same day do not spam the ledger."""
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 10.0, now)
    for _ in range(5):
        budget.check(conn, now=now)
    count = conn.execute(
        "SELECT COUNT(*) FROM event WHERE kind = 'budget_hard_stop'"
    ).fetchone()[0]
    assert count == 1


def test_soft_warn_emits_event_once_per_day(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 2.0, now)  # between soft=1 and hard=5
    for _ in range(5):
        budget.check(conn, now=now)
    count = conn.execute(
        "SELECT COUNT(*) FROM event WHERE kind = 'budget_soft_warn'"
    ).fetchone()[0]
    assert count == 1


def test_soft_warn_event_includes_spend_and_caps(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 2.0, now)
    budget.check(conn, now=now)
    import json
    row = conn.execute(
        "SELECT payload FROM event WHERE kind = 'budget_soft_warn'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["today_spend_usd"] == pytest.approx(2.0)
    assert payload["daily_soft_warn_usd"] == 1.0
    assert payload["daily_hard_cap_usd"] == 5.0


# ---------------------------------------------------------------------------
# Stickiness of HARD_STOP
# ---------------------------------------------------------------------------


def test_hard_stop_sticks_for_the_day(conn):
    """Once HARD_STOP event exists, check() keeps returning HARD_STOP
    even if spend later appears lower (defensive)."""
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 10.0, now)
    assert budget.check(conn, now=now).verdict is budget.Verdict.HARD_STOP

    # Nuke the task row to simulate an impossible rollback.
    conn.execute("DELETE FROM task")
    conn.commit()
    # Event still exists → verdict is still HARD_STOP.
    assert budget.check(conn, now=now).verdict is budget.Verdict.HARD_STOP


def test_yesterdays_spend_does_not_count_toward_today(conn):
    """Tasks updated yesterday are excluded from today's spend total.

    The stickiness property (HARD_STOP event sticks within a UTC day)
    is tested separately in ``test_hard_stop_sticks_for_the_day``.
    Insertion-time ``created_at`` on the event row means we cannot
    unit-test event-created-yesterday behavior without a time-travel
    shim; that aspect is instead covered by the window math test
    ``test_window_is_utc_midnight_to_midnight``.
    """
    now = datetime.now(tz=UTC)
    yesterday = now - timedelta(days=1)
    _seed(conn, "T0-1", 10.0, yesterday)

    # Today has no spend within its window.
    result = budget.check(conn, now=now)
    assert result.today_spend_usd == 0.0
    assert result.verdict is budget.Verdict.OK


# ---------------------------------------------------------------------------
# clear_today
# ---------------------------------------------------------------------------


def test_clear_today_removes_hard_stop(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 10.0, now)
    budget.check(conn, now=now)
    assert budget.is_hard_stopped(conn, now=now) is True

    removed = budget.clear_today(conn)
    assert removed == 1

    # Spend is still $10 → verdict immediately returns to HARD_STOP
    # (it re-emits). Confirms clear_today is an escape hatch, not a
    # budget reset.
    assert budget.check(conn, now=now).verdict is budget.Verdict.HARD_STOP


def test_clear_today_removes_soft_warn_too(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 2.0, now)
    budget.check(conn, now=now)

    removed = budget.clear_today(conn)
    assert removed == 1


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def test_window_is_utc_midnight_to_midnight():
    mid_afternoon = datetime(2026, 4, 24, 14, 30, 0, tzinfo=UTC)
    start, end = budget.window_for(now=mid_afternoon)
    assert start == datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 4, 25, 0, 0, 0, tzinfo=UTC)


def test_window_start_excludes_yesterdays_spend(conn):
    now = datetime.now(tz=UTC)
    yesterday_evening = now - timedelta(hours=25)
    _seed(conn, "T0-1", 100.0, yesterday_evening)
    result = budget.check(conn, now=now)
    assert result.today_spend_usd == 0.0
    assert result.verdict is budget.Verdict.OK


# ---------------------------------------------------------------------------
# is_hard_stopped helper
# ---------------------------------------------------------------------------


def test_is_hard_stopped_false_when_ok(conn):
    assert budget.is_hard_stopped(conn) is False


def test_is_hard_stopped_true_when_over_cap(conn):
    now = datetime.now(tz=UTC)
    _seed(conn, "T0-1", 10.0, now)
    assert budget.is_hard_stopped(conn, now=now) is True


# ---------------------------------------------------------------------------
# BudgetStatus dataclass
# ---------------------------------------------------------------------------


def test_status_dataclass_is_frozen(conn):
    from dataclasses import FrozenInstanceError
    result = budget.check(conn)
    with pytest.raises(FrozenInstanceError):
        result.verdict = budget.Verdict.HARD_STOP  # type: ignore[misc]
