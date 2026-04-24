"""Tests for oxi_core.v3.auto_recover — retry rejected / failed PR dispatches.

Test strategy:
- Pure DB + in-memory state: no git, no GitHub, no subprocess.
  auto_recover has no GitHubClient dependency.
- FakeGitHubClient is imported where referenced in docstrings for
  consistency with adjacent modules, but is not used here.
- Every observable effect is checked: DB row mutations, event emission,
  counter values in RecoverReport.

Coverage matrix:
  happy path — critic_rejected task past cooldown → reset to planned
  happy path — stale_no_progress task past cooldown → reset to planned
  happy path — pr_closed_unmerged task past cooldown → reset to planned
  retry_n badge in subtitle
  max_retries exhaustion → auto_recover_exhausted event
  within cooldown → skipped_cooldown
  non-recoverable reason → skipped_non_recoverable
  engine stopping → noop
  multiple tasks in one pass
  dashboard [retry] badge appears for recovered task
  dashboard [retry] badge absent for non-recovered task
"""

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
from oxi_core.v3.auto_recover import (
    RECOVERABLE_REASONS,
    RecoverReport,
    run,
)
from oxi_core.v3.dashboard import _recovered_task_ids, render_html
from oxi_core.v3.engine_state import EngineState

# ---------------------------------------------------------------------------
# Minimal adapter
# ---------------------------------------------------------------------------


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
def env(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "state": EngineState(plan_tier="standard", max_concurrent=4),
    }
    handle.connection.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _past(minutes: int = 60) -> str:
    """Return an ISO timestamp ``minutes`` ago (in the past)."""
    t = datetime.now(tz=UTC) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _future(minutes: int = 10) -> str:
    """Return an ISO timestamp ``minutes`` in the future."""
    t = datetime.now(tz=UTC) + timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _seed_failed(
    conn,
    identifier: str = "T1-1",
    title: str = "demo",
    subtitle: str = "",
    failure_reason: str = "critic_rejected",
    failed_at: str | None = None,
    status: str = "failed",
    pr_number: int | None = None,
) -> int:
    """Insert a failed/abandoned task into the DB. Returns task id."""
    if failed_at is None:
        failed_at = _past(60)
    cur = conn.execute(
        "INSERT INTO task "
        "(identifier, tier, title, subtitle, status, failure_reason, "
        "failed_at, pr_number, last_progress_at, updated_at) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            identifier,
            title,
            subtitle,
            status,
            failure_reason,
            failed_at,
            pr_number,
            _past(60),
            _past(60),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _events(conn, task_id: int, kind: str | None = None) -> list[dict]:
    """Return all events for task_id, optionally filtered by kind."""
    query = "SELECT kind, payload FROM event WHERE task_id = ?"
    params: list = [task_id]
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    rows = conn.execute(query, params).fetchall()
    return [{"kind": r[0], "payload": json.loads(r[1])} for r in rows]


def _task_row(conn, identifier: str) -> dict:
    row = conn.execute(
        "SELECT * FROM task WHERE identifier = ?", (identifier,)
    ).fetchone()
    assert row is not None, f"task {identifier!r} not found"
    return dict(row)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_recoverable_reasons_contains_expected():
    assert "critic_rejected" in RECOVERABLE_REASONS
    assert "pr_closed_unmerged" in RECOVERABLE_REASONS
    assert "stale_no_progress" in RECOVERABLE_REASONS


def test_report_is_frozen():
    from dataclasses import FrozenInstanceError
    r = RecoverReport(
        considered=0, recovered=0, exhausted=0,
        skipped_cooldown=0, skipped_non_recoverable=0,
    )
    with pytest.raises(FrozenInstanceError):
        r.recovered = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Engine-stop guard
# ---------------------------------------------------------------------------


def test_engine_stopping_returns_empty_report(env):
    _seed_failed(env["conn"])
    env["state"].request_stop()

    report = run(env["conn"], env["state"], cooldown_minutes=0)
    assert report == RecoverReport(
        considered=0, recovered=0, exhausted=0,
        skipped_cooldown=0, skipped_non_recoverable=0,
    )


# ---------------------------------------------------------------------------
# Happy path: reset to planned
# ---------------------------------------------------------------------------


def test_critic_rejected_past_cooldown_resets_to_planned(env):
    _seed_failed(
        env["conn"], "T1-1", failure_reason="critic_rejected",
        failed_at=_past(60),
    )
    report = run(env["conn"], env["state"], cooldown_minutes=30)

    assert report.recovered == 1
    assert report.considered == 1
    row = _task_row(env["conn"], "T1-1")
    assert row["status"] == "planned"
    assert row["failure_reason"] is None
    assert row["failed_at"] is None
    assert row["pr_number"] is None


def test_stale_no_progress_resets_to_planned(env):
    _seed_failed(
        env["conn"], "T1-2",
        failure_reason="stale_no_progress",
        status="abandoned",
        failed_at=_past(60),
    )
    report = run(env["conn"], env["state"], cooldown_minutes=0)

    assert report.recovered == 1
    row = _task_row(env["conn"], "T1-2")
    assert row["status"] == "planned"


def test_pr_closed_unmerged_resets_to_planned(env):
    _seed_failed(
        env["conn"], "T1-3",
        failure_reason="pr_closed_unmerged",
        failed_at=_past(90),
    )
    report = run(env["conn"], env["state"], cooldown_minutes=60)

    assert report.recovered == 1
    row = _task_row(env["conn"], "T1-3")
    assert row["status"] == "planned"


def test_recovery_clears_pr_number(env):
    _seed_failed(
        env["conn"], "T1-4",
        failure_reason="critic_rejected",
        pr_number=99,
        failed_at=_past(60),
    )
    run(env["conn"], env["state"], cooldown_minutes=0)

    row = _task_row(env["conn"], "T1-4")
    assert row["pr_number"] is None


def test_recovery_stamps_last_progress_at(env):
    """anti-pattern #1 invariant: every transition stamps freshness."""
    _seed_failed(
        env["conn"], "T1-5",
        failure_reason="critic_rejected",
        failed_at=_past(60),
    )
    # Manually set an ancient last_progress_at to verify it gets updated.
    env["conn"].execute(
        "UPDATE task SET last_progress_at = '2020-01-01 00:00:00' "
        "WHERE identifier = 'T1-5'"
    )
    env["conn"].commit()

    run(env["conn"], env["state"], cooldown_minutes=0)

    row = _task_row(env["conn"], "T1-5")
    assert row["last_progress_at"] != "2020-01-01 00:00:00"


# ---------------------------------------------------------------------------
# Recovery note in subtitle
# ---------------------------------------------------------------------------


def test_subtitle_includes_retry_n_badge(env):
    _seed_failed(
        env["conn"], "T1-6", subtitle="original subtitle",
        failure_reason="critic_rejected", failed_at=_past(60),
    )
    run(env["conn"], env["state"], cooldown_minutes=0)

    row = _task_row(env["conn"], "T1-6")
    subtitle = row["subtitle"]
    assert "[retry 1]" in subtitle
    assert "critic_rejected" in subtitle


def test_subtitle_does_not_stack_retry_prefixes(env):
    """Running auto_recover twice should not result in '[retry 2]...[retry 1]...'."""
    _seed_failed(
        env["conn"], "T1-7", subtitle="",
        failure_reason="critic_rejected", failed_at=_past(60),
    )
    # First recovery.
    run(env["conn"], env["state"], cooldown_minutes=0)
    row = _task_row(env["conn"], "T1-7")
    subtitle_after_first = row["subtitle"]
    assert subtitle_after_first.count("[retry") == 1

    # Simulate a second failure (same task, re-failed).
    env["conn"].execute(
        "UPDATE task SET status='failed', failure_reason='critic_rejected', "
        "failed_at=? WHERE identifier='T1-7'",
        (_past(60),),
    )
    env["conn"].commit()

    # Second recovery.
    run(env["conn"], env["state"], cooldown_minutes=0)
    row2 = _task_row(env["conn"], "T1-7")
    subtitle_after_second = row2["subtitle"]
    # Only one [retry N] prefix, not two stacked.
    assert subtitle_after_second.count("[retry") == 1
    assert "[retry 2]" in subtitle_after_second


def test_subtitle_preserves_original_content(env):
    _seed_failed(
        env["conn"], "T1-8", subtitle="do the thing",
        failure_reason="critic_rejected", failed_at=_past(60),
    )
    run(env["conn"], env["state"], cooldown_minutes=0)

    row = _task_row(env["conn"], "T1-8")
    assert "do the thing" in row["subtitle"]


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def test_emits_auto_recover_attempted_event(env):
    task_id = _seed_failed(
        env["conn"], "T1-9",
        failure_reason="critic_rejected", failed_at=_past(60),
    )
    run(env["conn"], env["state"], cooldown_minutes=0)

    evts = _events(env["conn"], task_id, kind="auto_recover_attempted")
    assert len(evts) == 1
    payload = evts[0]["payload"]
    assert payload["retry_n"] == 1
    assert payload["prior_failure_reason"] == "critic_rejected"


# ---------------------------------------------------------------------------
# Cooldown filter
# ---------------------------------------------------------------------------


def test_within_cooldown_is_skipped(env):
    _seed_failed(
        env["conn"], "T1-10",
        failure_reason="critic_rejected",
        failed_at=_past(5),   # only 5 minutes ago
    )
    report = run(env["conn"], env["state"], cooldown_minutes=30)

    assert report.skipped_cooldown == 1
    assert report.recovered == 0
    row = _task_row(env["conn"], "T1-10")
    assert row["status"] == "failed"


def test_exactly_at_cooldown_boundary_is_recovered(env):
    """A task exactly at the cooldown edge (>= cooldown) must be recovered."""
    # Set failed_at to exactly cooldown_minutes ago.
    exactly_ago = (datetime.now(tz=UTC) - timedelta(minutes=30)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    _seed_failed(
        env["conn"], "T1-11",
        failure_reason="critic_rejected",
        failed_at=exactly_ago,
    )
    now = datetime.now(tz=UTC)
    report = run(
        env["conn"], env["state"],
        cooldown_minutes=30,
        now=now,
    )
    # At exactly the boundary the timedelta equals cooldown → NOT < cooldown → recovered.
    assert report.recovered == 1


# ---------------------------------------------------------------------------
# Non-recoverable reasons
# ---------------------------------------------------------------------------


def test_non_recoverable_reason_is_skipped(env):
    _seed_failed(
        env["conn"], "T1-12",
        failure_reason="budget_hard_stop",
        failed_at=_past(60),
    )
    report = run(env["conn"], env["state"], cooldown_minutes=0)

    assert report.skipped_non_recoverable == 1
    assert report.recovered == 0
    row = _task_row(env["conn"], "T1-12")
    assert row["status"] == "failed"


def test_none_failure_reason_is_skipped(env):
    """Tasks without failure_reason are not candidates (query filters them)."""
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status, failed_at) "
        "VALUES ('T1-13', 1, 't', 'failed', ?)",
        (_past(60),),
    )
    env["conn"].commit()
    report = run(env["conn"], env["state"], cooldown_minutes=0)

    assert report.considered == 0


# ---------------------------------------------------------------------------
# Max retries / exhaustion
# ---------------------------------------------------------------------------


def test_exhausted_task_emits_auto_recover_exhausted_event(env):
    task_id = _seed_failed(
        env["conn"], "T1-14",
        failure_reason="critic_rejected",
        failed_at=_past(60),
    )
    # Pre-seed retry events to simulate prior attempts (max_retries=2).
    for i in range(2):
        env["conn"].execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, "auto_recover_attempted", json.dumps({"retry_n": i + 1})),
        )
    env["conn"].commit()

    report = run(env["conn"], env["state"], cooldown_minutes=0, max_retries=2)

    assert report.exhausted == 1
    assert report.recovered == 0
    # Task stays in failed.
    row = _task_row(env["conn"], "T1-14")
    assert row["status"] == "failed"
    evts = _events(env["conn"], task_id, kind="auto_recover_exhausted")
    assert len(evts) == 1


def test_exhaustion_does_not_change_task_status(env):
    task_id = _seed_failed(
        env["conn"], "T1-15",
        failure_reason="critic_rejected",
        status="abandoned",
        failed_at=_past(60),
    )
    for i in range(3):
        env["conn"].execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, "auto_recover_attempted", json.dumps({"retry_n": i + 1})),
        )
    env["conn"].commit()

    run(env["conn"], env["state"], cooldown_minutes=0, max_retries=3)

    row = _task_row(env["conn"], "T1-15")
    assert row["status"] == "abandoned"


def test_retry_count_increments_correctly(env):
    """After two successful recoveries the third run sees retry_n=3."""
    task_id = _seed_failed(
        env["conn"], "T1-16",
        failure_reason="critic_rejected",
        failed_at=_past(60),
    )
    # Simulate two prior attempts already recorded.
    for i in range(2):
        env["conn"].execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, "auto_recover_attempted", json.dumps({"retry_n": i + 1})),
        )
    env["conn"].commit()

    run(env["conn"], env["state"], cooldown_minutes=0, max_retries=5)

    evts = _events(env["conn"], task_id, kind="auto_recover_attempted")
    last_payload = evts[-1]["payload"]
    assert last_payload["retry_n"] == 3


# ---------------------------------------------------------------------------
# Multiple tasks in one pass
# ---------------------------------------------------------------------------


def test_multiple_tasks_recovered_in_one_pass(env):
    for ident in ("T1-17", "T1-18", "T1-19"):
        _seed_failed(
            env["conn"], ident,
            failure_reason="critic_rejected",
            failed_at=_past(60),
        )
    report = run(env["conn"], env["state"], cooldown_minutes=0)

    assert report.recovered == 3
    for ident in ("T1-17", "T1-18", "T1-19"):
        row = _task_row(env["conn"], ident)
        assert row["status"] == "planned"


def test_mixed_batch(env):
    """One recovered, one in cooldown, one non-recoverable, one exhausted."""
    # T1-20: recoverable, past cooldown.
    _seed_failed(env["conn"], "T1-20",
                         failure_reason="critic_rejected", failed_at=_past(60))
    # T1-21: recoverable but within cooldown.
    _seed_failed(env["conn"], "T1-21",
                 failure_reason="critic_rejected", failed_at=_past(5))
    # T1-22: non-recoverable reason.
    _seed_failed(env["conn"], "T1-22",
                 failure_reason="custom_reason", failed_at=_past(60))
    # T1-23: past cooldown but exhausted.
    id_23 = _seed_failed(env["conn"], "T1-23",
                         failure_reason="pr_closed_unmerged", failed_at=_past(60))
    for i in range(3):
        env["conn"].execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (id_23, "auto_recover_attempted", json.dumps({"retry_n": i + 1})),
        )
    env["conn"].commit()

    report = run(env["conn"], env["state"], cooldown_minutes=30, max_retries=3)

    assert report.recovered == 1
    assert report.skipped_cooldown == 1
    assert report.skipped_non_recoverable == 1
    assert report.exhausted == 1
    assert report.considered == 2  # T1-20 and T1-23 (past cooldown + recoverable)


# ---------------------------------------------------------------------------
# Tasks without failed_at are excluded
# ---------------------------------------------------------------------------


def test_task_without_failed_at_is_not_candidate(env):
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status, failure_reason) "
        "VALUES ('T1-24', 1, 't', 'failed', 'critic_rejected')",
    )
    env["conn"].commit()
    report = run(env["conn"], env["state"], cooldown_minutes=0)

    assert report.considered == 0
    assert report.recovered == 0


# ---------------------------------------------------------------------------
# Planned / dispatched tasks are ignored
# ---------------------------------------------------------------------------


def test_planned_task_not_candidate(env):
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T1-25', 1, 't', 'planned')"
    )
    env["conn"].commit()
    report = run(env["conn"], env["state"], cooldown_minutes=0)

    assert report.considered == 0


def test_dispatched_task_not_candidate(env):
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T1-26', 1, 't', 'dispatched')"
    )
    env["conn"].commit()
    report = run(env["conn"], env["state"], cooldown_minutes=0)

    assert report.considered == 0


# ---------------------------------------------------------------------------
# Dashboard: [retry] badge
# ---------------------------------------------------------------------------


def test_dashboard_shows_retry_badge_for_recovered_task(env):
    """Tasks that have been through auto_recover get a [retry] badge in the HTML."""
    _seed_failed(
        env["conn"], "T1-27", title="retry-me",
        failure_reason="critic_rejected", failed_at=_past(60),
    )
    run(env["conn"], env["state"], cooldown_minutes=0)

    html_out = render_html(env["conn"])
    assert "[retry]" in html_out
    assert "retry-badge" in html_out


def test_dashboard_no_retry_badge_for_fresh_task(env):
    """Tasks with no auto_recover history must not show the [retry] badge."""
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T1-28', 1, 'fresh task', 'planned')"
    )
    env["conn"].commit()

    html_out = render_html(env["conn"])
    assert "T1-28" in html_out
    assert "[retry]" not in html_out


def test_recovered_task_ids_helper_returns_correct_ids(env):
    task_id = _seed_failed(
        env["conn"], "T1-29",
        failure_reason="critic_rejected", failed_at=_past(60),
    )
    env["conn"].execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
        (task_id, "auto_recover_attempted", json.dumps({"retry_n": 1})),
    )
    env["conn"].commit()

    ids = _recovered_task_ids(env["conn"])
    assert task_id in ids


def test_recovered_task_ids_empty_when_no_recoveries(env):
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T1-30', 1, 't', 'planned')"
    )
    env["conn"].commit()
    ids = _recovered_task_ids(env["conn"])
    assert len(ids) == 0


# ---------------------------------------------------------------------------
# now= override for deterministic cooldown tests
# ---------------------------------------------------------------------------


def test_now_override_controls_cooldown_evaluation(env):
    """Passing now= fixes the reference clock so tests are time-independent."""
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # failed_at is 31 minutes before fixed_now → past 30-min cooldown.
    failed_at = (fixed_now - timedelta(minutes=31)).strftime("%Y-%m-%d %H:%M:%S")
    _seed_failed(
        env["conn"], "T1-31",
        failure_reason="critic_rejected",
        failed_at=failed_at,
    )
    report = run(
        env["conn"], env["state"],
        cooldown_minutes=30,
        now=fixed_now,
    )
    assert report.recovered == 1


def test_now_override_keeps_within_cooldown(env):
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # failed_at is 10 minutes before fixed_now → still within 30-min cooldown.
    failed_at = (fixed_now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    _seed_failed(
        env["conn"], "T1-32",
        failure_reason="critic_rejected",
        failed_at=failed_at,
    )
    report = run(
        env["conn"], env["state"],
        cooldown_minutes=30,
        now=fixed_now,
    )
    assert report.skipped_cooldown == 1
    assert report.recovered == 0
