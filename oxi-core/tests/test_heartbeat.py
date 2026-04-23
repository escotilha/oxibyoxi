"""Tests for oxi_core.v3.heartbeat — the reaper.

Every invariant from the prior-orchestrator post-mortem (orphan-reap
thrash, orphan-with-PR abandoned, killswitch bypass, created_at
fallback) has a dedicated test here.
"""

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
from oxi_core.v3.heartbeat import (
    DEFAULT_STALE_AFTER_SECONDS,
    ReapReport,
    _effective_freshness,
    _is_stale,
    _parse_sqlite_timestamp,
    reap,
)

# ---------------------------------------------------------------------------
# Adapter + fixtures
# ---------------------------------------------------------------------------


@dataclass
class _TestAdapter:
    db_path_value: str

    def naming(self) -> NamingConfig:
        return NamingConfig()

    def paths(self) -> PathsConfig:
        return PathsConfig(db_path=self.db_path_value)

    def budget(self) -> BudgetCaps:
        return BudgetCaps()

    def github_repo(self) -> str:
        return "owner/repo"

    def roadmap_location(self) -> str:
        return "roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/",)

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (
            DispatchHost(
                name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"
            ),
        )

    def promote_recipe(self) -> None:
        return None

    def plan_tier(self) -> str:
        return "standard"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def conn(tmp_path: Path):
    register_adapter(_TestAdapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield handle.connection
    handle.connection.close()


def _seed_dispatched(
    conn,
    *,
    identifier: str = "T0-1",
    last_progress_at: str | None = None,
    pr_number: int | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> int:
    """Insert a task directly into the 'dispatched' state with controllable timestamps.

    Uses raw UPDATE after INSERT so we can set arbitrary ISO strings.
    """
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES (?, 0, 'title', 'dispatched')",
        (identifier,),
    )
    task_id = cur.lastrowid

    if last_progress_at is not None:
        conn.execute(
            "UPDATE task SET last_progress_at = ? WHERE id = ?",
            (last_progress_at, task_id),
        )
    if updated_at is not None:
        conn.execute(
            "UPDATE task SET updated_at = ? WHERE id = ?",
            (updated_at, task_id),
        )
    if created_at is not None:
        conn.execute(
            "UPDATE task SET created_at = ? WHERE id = ?",
            (created_at, task_id),
        )
    if pr_number is not None:
        # The schema doesn't have pr_number yet; we'd need to add it
        # via future migration. For now, tests that want a PR simulate
        # it by patching the Task object post-load.
        pass
    conn.commit()
    return task_id


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# _parse_sqlite_timestamp
# ---------------------------------------------------------------------------


def test_parse_sqlite_timestamp_happy():
    dt = _parse_sqlite_timestamp("2026-04-23 12:00:00")
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 12
    assert dt.tzinfo is UTC


def test_parse_sqlite_timestamp_returns_none_on_none():
    assert _parse_sqlite_timestamp(None) is None


def test_parse_sqlite_timestamp_returns_none_on_garbage():
    assert _parse_sqlite_timestamp("not a date") is None


# ---------------------------------------------------------------------------
# _effective_freshness
# ---------------------------------------------------------------------------


def test_effective_freshness_prefers_last_progress_at():
    from oxi_core.v3.dispatch import Task
    lpa = "2026-04-23 12:00:00"
    upd = "2026-04-22 10:00:00"  # earlier
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=lpa, created_at="2026-04-20 00:00:00", updated_at=upd,
    )
    result = _effective_freshness(task)
    assert result == _parse_sqlite_timestamp(lpa)


def test_effective_freshness_falls_back_to_updated_at():
    from oxi_core.v3.dispatch import Task
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=None, created_at="2026-04-20 00:00:00",
        updated_at="2026-04-23 12:00:00",
    )
    result = _effective_freshness(task)
    assert result == _parse_sqlite_timestamp("2026-04-23 12:00:00")


def test_effective_freshness_never_uses_created_at():
    """Critical: heartbeat must never trust created_at for freshness.

    That's the bug class that killed prior orchestrators — a 2-day-old
    task dispatched 5 seconds ago gets reaped because created_at is
    used as the clock.
    """
    from oxi_core.v3.dispatch import Task
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=None, updated_at=None,
        created_at="2026-04-23 12:00:00",
    )
    # Both lpa and updated_at are None; freshness is None (stale by default).
    assert _effective_freshness(task) is None


# ---------------------------------------------------------------------------
# _is_stale
# ---------------------------------------------------------------------------


def test_is_stale_when_older_than_grace():
    from oxi_core.v3.dispatch import Task
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old_ts = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=old_ts, created_at=old_ts, updated_at=old_ts,
    )
    assert _is_stale(task, now, timedelta(minutes=10)) is True


def test_is_stale_false_when_fresh():
    from oxi_core.v3.dispatch import Task
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    recent = (now - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=recent, created_at=recent, updated_at=recent,
    )
    assert _is_stale(task, now, timedelta(minutes=10)) is False


def test_is_stale_true_when_no_freshness_data():
    """Defensive: no timestamps at all → stale."""
    from oxi_core.v3.dispatch import Task
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=None, created_at=None, updated_at=None,
    )
    assert _is_stale(task, now, timedelta(minutes=10)) is True


# ---------------------------------------------------------------------------
# reap — happy paths
# ---------------------------------------------------------------------------


def test_reap_abandons_stale_task(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.considered == 1
    assert report.abandoned == 1
    assert report.protected_by_pr == 0
    assert report.skipped_fresh == 0

    row = conn.execute("SELECT status, failure_reason FROM task WHERE id = 1").fetchone()
    assert row["status"] == "abandoned"
    assert row["failure_reason"] == "stale_no_progress"


def test_reap_skips_fresh_task(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    recent = _iso(now - timedelta(seconds=10))
    _seed_dispatched(conn, last_progress_at=recent, updated_at=recent)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.abandoned == 0
    assert report.skipped_fresh == 1
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "dispatched"


def test_reap_ignores_non_dispatched_tasks(conn):
    """Planned, merged, abandoned, failed rows are never considered."""
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(hours=2))
    # Planned, old — should not be touched.
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, last_progress_at, updated_at) "
        "VALUES ('T0-2', 0, 't', 'planned', ?, ?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, last_progress_at, updated_at) "
        "VALUES ('T0-3', 0, 't', 'merged', ?, ?)",
        (old, old),
    )
    conn.commit()

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.considered == 0
    assert report.abandoned == 0


def test_reap_emits_ledger_event(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    task_id = _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    reap(conn, state, stale_after_seconds=600, now=now)

    kinds = [
        row[0]
        for row in conn.execute(
            "SELECT kind FROM event WHERE task_id = ?", (task_id,)
        )
    ]
    assert "abandoned_by_heartbeat" in kinds


# ---------------------------------------------------------------------------
# Engine stop
# ---------------------------------------------------------------------------


def test_reap_is_noop_when_stopping(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    state.request_stop()

    report = reap(conn, state, stale_after_seconds=600, now=now)
    assert report == ReapReport(
        considered=0, abandoned=0, protected_by_pr=0, skipped_fresh=0
    )
    # Task stayed dispatched.
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Atomic transition (anti-pattern #1 on the reap side)
# ---------------------------------------------------------------------------


def test_reap_stamps_last_progress_at_on_abandon(conn):
    """Even on abandon, the freshness beacon updates.

    Without this, a subsequent reap would see the same stale timestamp
    and … well, the status is already abandoned so it wouldn't reap
    again. But stamping keeps the invariant that every state transition
    stamps freshness, which simplifies reasoning.
    """
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    reap(conn, state, stale_after_seconds=600, now=now)

    row = conn.execute(
        "SELECT last_progress_at, updated_at FROM task WHERE id = 1"
    ).fetchone()
    assert row["last_progress_at"] != old
    assert row["updated_at"] != old


# ---------------------------------------------------------------------------
# Orphan-with-PR protection (anti-pattern #4)
# ---------------------------------------------------------------------------


def test_reap_does_not_abandon_task_with_open_pr(conn, monkeypatch):
    """Tasks with pr_number != None are protected from abandonment.

    The current schema doesn't yet have pr_number; we simulate the
    invariant by patching ``is_orphan_reapable`` to return False for
    a specific task. When pr_watcher lands and adds the column, this
    test gets rewritten to seed pr_number directly.
    """
    from oxi_core.v3 import heartbeat as hb_mod

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    # Patch the shared helper: this specific task has a PR.
    monkeypatch.setattr(hb_mod, "is_orphan_reapable", lambda task: False)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.abandoned == 0
    assert report.protected_by_pr == 1
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Mixed case — multiple tasks, different outcomes
# ---------------------------------------------------------------------------


def test_reap_mixed_batch(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    stale_ts = _iso(now - timedelta(minutes=30))
    fresh_ts = _iso(now - timedelta(seconds=5))

    _seed_dispatched(conn, identifier="T0-1", last_progress_at=stale_ts, updated_at=stale_ts)
    _seed_dispatched(conn, identifier="T0-2", last_progress_at=fresh_ts, updated_at=fresh_ts)
    _seed_dispatched(conn, identifier="T0-3", last_progress_at=stale_ts, updated_at=stale_ts)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.considered == 3
    assert report.abandoned == 2
    assert report.skipped_fresh == 1
    assert report.protected_by_pr == 0


# ---------------------------------------------------------------------------
# Default constant sanity
# ---------------------------------------------------------------------------


def test_default_stale_after_is_conservative():
    # 10 minutes is the baseline documented grace period.
    assert DEFAULT_STALE_AFTER_SECONDS == 600


def test_report_dataclass_is_frozen():
    from dataclasses import FrozenInstanceError

    report = ReapReport(considered=1, abandoned=1, protected_by_pr=0, skipped_fresh=0)
    with pytest.raises(FrozenInstanceError):
        report.abandoned = 2  # type: ignore[misc]
