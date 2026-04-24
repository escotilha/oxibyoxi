"""Tests for oxi_core.v3.engine_health — T1-18 auto-healing engine self-state.

Test strategy
-------------
- Pure in-memory EngineHealth tests: no DB, no git, no subprocesses.
- DB-backed tests: real SQLite, verify event emission and query helpers.
- Integration tests: dispatch_one with EngineHealth injected; fake claude.
- Dashboard tests: health banner shown/hidden based on DB events.
- CLI tests: oxi v3 heal command.

Coverage matrix
---------------
EngineHealth construction
  valid threshold
  threshold < 1 raises ValueError

HealthStatus
  starts HEALTHY
  is_healthy / is_unhealthy initial state

record_success
  resets consecutive_failures to 0
  does not heal an unhealthy engine (sticky)

record_failure
  increments consecutive_failures
  does NOT transition to UNHEALTHY below threshold
  transitions to UNHEALTHY exactly at threshold
  transitions to UNHEALTHY above threshold (idempotent event)
  emits engine_unhealthy event to DB on first transition
  does NOT emit duplicate engine_unhealthy events after first transition
  conn=None skips DB write (no crash)

heal
  returns True when engine was unhealthy
  returns False when engine was already healthy (no-op)
  resets consecutive_failures to 0
  transitions back to HEALTHY
  emits engine_healed event to DB

is_engine_unhealthy_from_db
  returns False with no events
  returns True after engine_unhealthy event
  returns False after engine_healed follows engine_unhealthy
  most-recent event wins

dispatch_one health gate
  returns None when engine is unhealthy (does not dispatch)
  dispatches normally when engine is healthy
  failure increments counter
  success resets counter
  RETRYABLE_TRANSIENT does NOT increment counter
  threshold crossed → unhealthy → next dispatch_one returns None

dispatch_loop health gate
  breaks on unhealthy mid-loop

dashboard health banner
  banner visible when DB says unhealthy
  banner absent when DB says healthy

CLI cmd_heal
  clears unhealthy state and prints message
  no-op when already healthy
"""

from __future__ import annotations

import json
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
from oxi_core.v3.engine_health import (
    DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD,
    EVENT_KIND_HEALED,
    EVENT_KIND_UNHEALTHY,
    HEALTH_BANNER,
    EngineHealth,
    HealthStatus,
    get_last_unhealthy_event,
    is_engine_unhealthy_from_db,
)
from oxi_core.v3.engine_state import EngineState

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"


# ---------------------------------------------------------------------------
# Minimal adapter fixture (mirrors pattern in test_auto_recover.py)
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    db_path_value: str
    worktree_root_value: str = "/tmp"

    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig(db_path=self.db_path_value)
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (DispatchHost(
            name="local", ssh_alias=None,
            max_concurrent=1, worktree_root=self.worktree_root_value,
        ),)
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def conn(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield handle.connection
    handle.connection.close()


def _events(conn, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM event WHERE kind = ? ORDER BY id", (kind,)
    ).fetchall()
    return [json.loads(r[0] or "{}") for r in rows]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_default_threshold():
    h = EngineHealth()
    assert h.consecutive_failure_threshold == DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD


def test_custom_threshold():
    h = EngineHealth(consecutive_failure_threshold=3)
    assert h.consecutive_failure_threshold == 3


def test_threshold_below_one_raises():
    with pytest.raises(ValueError, match="consecutive_failure_threshold must be >= 1"):
        EngineHealth(consecutive_failure_threshold=0)
    with pytest.raises(ValueError, match="consecutive_failure_threshold must be >= 1"):
        EngineHealth(consecutive_failure_threshold=-1)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_starts_healthy():
    h = EngineHealth()
    assert h.is_healthy() is True
    assert h.is_unhealthy() is False
    assert h.status() is HealthStatus.HEALTHY
    assert h.consecutive_failures() == 0


# ---------------------------------------------------------------------------
# record_success
# ---------------------------------------------------------------------------


def test_record_success_resets_counter():
    h = EngineHealth(consecutive_failure_threshold=5)
    h._consecutive_failures = 3  # noqa: SLF001 — inject state for test
    h.record_success()
    assert h.consecutive_failures() == 0


def test_record_success_does_not_heal_unhealthy_engine():
    """Success resets the counter but does NOT clear the UNHEALTHY status.

    The operator must call heal() explicitly.  This makes the unhealthy
    state sticky so operators see it even if a later task accidentally
    succeeds.
    """
    h = EngineHealth(consecutive_failure_threshold=2)
    h.record_failure(None)
    h.record_failure(None)
    assert h.is_unhealthy()

    h.record_success()
    # Still unhealthy — sticky by design.
    assert h.is_unhealthy()
    # But counter is reset.
    assert h.consecutive_failures() == 0


# ---------------------------------------------------------------------------
# record_failure — counting and threshold
# ---------------------------------------------------------------------------


def test_record_failure_increments_counter():
    h = EngineHealth(consecutive_failure_threshold=10)
    h.record_failure(None)
    assert h.consecutive_failures() == 1
    h.record_failure(None)
    assert h.consecutive_failures() == 2


def test_record_failure_below_threshold_stays_healthy():
    h = EngineHealth(consecutive_failure_threshold=3)
    h.record_failure(None)
    h.record_failure(None)
    assert h.is_healthy()


def test_record_failure_at_threshold_becomes_unhealthy():
    h = EngineHealth(consecutive_failure_threshold=3)
    h.record_failure(None)
    h.record_failure(None)
    h.record_failure(None)
    assert h.is_unhealthy()


def test_record_failure_above_threshold_stays_unhealthy():
    h = EngineHealth(consecutive_failure_threshold=2)
    h.record_failure(None)
    h.record_failure(None)
    h.record_failure(None)  # already unhealthy — stays that way
    assert h.is_unhealthy()


def test_record_failure_emits_engine_unhealthy_event(conn):
    h = EngineHealth(consecutive_failure_threshold=2)
    h.record_failure(conn, reason="first")
    # Not yet at threshold.
    evts = _events(conn, EVENT_KIND_UNHEALTHY)
    assert len(evts) == 0

    h.record_failure(conn, reason="second — threshold crossed")
    evts = _events(conn, EVENT_KIND_UNHEALTHY)
    assert len(evts) == 1
    assert evts[0]["consecutive_failures"] == 2


def test_record_failure_does_not_emit_duplicate_event_after_threshold(conn):
    """Only ONE engine_unhealthy event per transition (not one per failure)."""
    h = EngineHealth(consecutive_failure_threshold=2)
    h.record_failure(conn)
    h.record_failure(conn)  # → UNHEALTHY, event emitted
    h.record_failure(conn)  # already unhealthy — no new event
    h.record_failure(conn)

    evts = _events(conn, EVENT_KIND_UNHEALTHY)
    assert len(evts) == 1


def test_record_failure_conn_none_does_not_crash():
    """conn=None must not raise — DB write is skipped silently."""
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(None, reason="no db")
    assert h.is_unhealthy()


def test_record_failure_reason_stored_in_event(conn):
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(conn, reason="FAILED")
    evts = _events(conn, EVENT_KIND_UNHEALTHY)
    assert evts[0]["reason"] == "FAILED"


# ---------------------------------------------------------------------------
# heal
# ---------------------------------------------------------------------------


def test_heal_returns_true_when_unhealthy():
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(None)
    assert h.heal() is True


def test_heal_returns_false_when_already_healthy():
    h = EngineHealth()
    assert h.heal() is False


def test_heal_transitions_to_healthy():
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(None)
    assert h.is_unhealthy()
    h.heal()
    assert h.is_healthy()


def test_heal_resets_consecutive_failures():
    h = EngineHealth(consecutive_failure_threshold=5)
    for _ in range(7):
        h.record_failure(None)
    h.heal()
    assert h.consecutive_failures() == 0


def test_heal_emits_engine_healed_event(conn):
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(conn, reason="failed")
    h.heal(conn, operator="oxi v3 heal")

    evts = _events(conn, EVENT_KIND_HEALED)
    assert len(evts) == 1
    assert evts[0]["operator"] == "oxi v3 heal"
    assert evts[0]["prior_consecutive_failures"] >= 1


def test_heal_does_not_emit_healed_event_when_already_healthy(conn):
    h = EngineHealth()
    h.heal(conn, operator="test")  # no-op
    evts = _events(conn, EVENT_KIND_HEALED)
    assert len(evts) == 0


def test_heal_is_idempotent():
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(None)
    h.heal()
    h.heal()  # second call is a no-op
    assert h.is_healthy()


# ---------------------------------------------------------------------------
# is_engine_unhealthy_from_db
# ---------------------------------------------------------------------------


def test_is_engine_unhealthy_from_db_false_initially(conn):
    assert is_engine_unhealthy_from_db(conn) is False


def test_is_engine_unhealthy_from_db_true_after_unhealthy_event(conn):
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(conn, reason="test")
    assert is_engine_unhealthy_from_db(conn) is True


def test_is_engine_unhealthy_from_db_false_after_healed(conn):
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(conn, reason="test")
    h.heal(conn, operator="test")
    assert is_engine_unhealthy_from_db(conn) is False


def test_is_engine_unhealthy_from_db_most_recent_event_wins(conn):
    """If heal is followed by another failure, the unhealthy event should win."""
    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(conn, reason="first failure")   # → UNHEALTHY
    h.heal(conn, operator="first heal")              # → HEALTHY
    # Simulate engine restarting and failing again.
    h2 = EngineHealth(consecutive_failure_threshold=1)
    h2.record_failure(conn, reason="second failure")  # → UNHEALTHY again
    assert is_engine_unhealthy_from_db(conn) is True


# ---------------------------------------------------------------------------
# get_last_unhealthy_event
# ---------------------------------------------------------------------------


def test_get_last_unhealthy_event_none_initially(conn):
    assert get_last_unhealthy_event(conn) is None


def test_get_last_unhealthy_event_returns_payload(conn):
    h = EngineHealth(consecutive_failure_threshold=2)
    h.record_failure(conn)
    h.record_failure(conn, reason="the reason")
    event = get_last_unhealthy_event(conn)
    assert event is not None
    assert event["consecutive_failures"] == 2
    assert event["reason"] == "the reason"
    assert "created_at" in event


# ---------------------------------------------------------------------------
# dispatch_one health gate
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch_env(tmp_path: Path):
    """Full dispatch environment with git repo + DB."""
    import subprocess

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

    worktree_root = tmp_path / "worktrees"
    db_path = tmp_path / "oxi.db"
    adapter = _Adapter(
        db_path_value=str(db_path),
        worktree_root_value=str(worktree_root),
    )
    register_adapter(adapter)

    handle = db.connect()
    yield {
        "conn": handle.connection,
        "repo_root": clone,
        "adapter": adapter,
        "engine_state": EngineState(plan_tier="standard", max_concurrent=4),
    }
    handle.connection.close()


def _seed_task(conn, *, identifier: str = "T0-1", tier: int = 0,
               title: str = "do a thing") -> int:
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, subtitle) VALUES (?, ?, ?, '')",
        (identifier, tier, title),
    )
    conn.commit()
    return cur.lastrowid


def _extra(scenario: str) -> dict[str, str]:
    return {"FAKE_CLAUDE_SCENARIO": scenario}


@pytest.mark.asyncio
async def test_dispatch_one_skips_when_unhealthy(dispatch_env):
    """EngineHealth.is_unhealthy() → dispatch_one returns None without touching DB."""
    env = dispatch_env
    _seed_task(env["conn"])

    health = EngineHealth(consecutive_failure_threshold=1)
    health.record_failure(None)  # force unhealthy
    assert health.is_unhealthy()

    from oxi_core.v3.dispatch import dispatch_one
    result = await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        engine_health=health,
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_extra("happy"),
    )
    assert result is None

    # Task was NOT dispatched.
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row["status"] == "planned"


@pytest.mark.asyncio
async def test_dispatch_one_dispatches_normally_when_healthy(dispatch_env):
    env = dispatch_env
    _seed_task(env["conn"])

    health = EngineHealth(consecutive_failure_threshold=5)
    assert health.is_healthy()

    from oxi_core.v3.dispatch import dispatch_one
    result = await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        engine_health=health,
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_extra("happy"),
    )
    assert result is not None
    # After success, counter is reset.
    assert health.consecutive_failures() == 0


@pytest.mark.asyncio
async def test_dispatch_one_failure_increments_health_counter(dispatch_env):
    env = dispatch_env
    _seed_task(env["conn"])

    health = EngineHealth(consecutive_failure_threshold=5)

    from oxi_core.v3.dispatch import dispatch_one
    # "exit_1_after_pr" scenario: exits 1, both checks False → FAILED.
    result = await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        engine_health=health,
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_extra("exit_1_after_pr"),
        _check_commits=lambda _p, _b: False,
        _check_pr=lambda _b, _r: False,
    )
    assert result is not None
    from oxi_core.v3.dispatch_invoke import Classification
    assert result.classification is Classification.FAILED
    assert health.consecutive_failures() == 1


@pytest.mark.asyncio
async def test_dispatch_one_success_resets_health_counter(dispatch_env):
    env = dispatch_env
    _seed_task(env["conn"])

    health = EngineHealth(consecutive_failure_threshold=5)
    health._consecutive_failures = 3  # noqa: SLF001

    from oxi_core.v3.dispatch import dispatch_one
    result = await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        engine_health=health,
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_extra("happy"),
    )
    assert result is not None
    assert health.consecutive_failures() == 0


@pytest.mark.asyncio
async def test_dispatch_one_retryable_does_not_increment_counter(dispatch_env):
    """RETRYABLE_TRANSIENT must NOT count as a failure for health purposes."""
    env = dispatch_env
    _seed_task(env["conn"])

    health = EngineHealth(consecutive_failure_threshold=2)

    from oxi_core.v3.dispatch import dispatch_one
    # exit_143 → RETRYABLE_TRANSIENT
    await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        engine_health=health,
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_extra("exit_143"),
    )
    assert health.consecutive_failures() == 0
    assert health.is_healthy()


@pytest.mark.asyncio
async def test_threshold_crossed_blocks_subsequent_dispatches(dispatch_env):
    """After N failures cross the threshold, next dispatch_one returns None."""
    env = dispatch_env
    # Seed two tasks.
    _seed_task(env["conn"], identifier="T0-1")
    _seed_task(env["conn"], identifier="T0-2")

    health = EngineHealth(consecutive_failure_threshold=1)

    from oxi_core.v3.dispatch import dispatch_one

    # First dispatch fails → threshold crossed → UNHEALTHY.
    await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        engine_health=health,
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_extra("exit_1_after_pr"),
        _check_commits=lambda _p, _b: False,
        _check_pr=lambda _b, _r: False,
    )
    assert health.is_unhealthy()

    # Second dispatch_one must be blocked.
    result2 = await dispatch_one(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        engine_health=health,
        repo_root=env["repo_root"],
        binary=str(FAKE_CLAUDE),
        extra_env=_extra("happy"),
    )
    assert result2 is None

    # T0-2 was NOT dispatched.
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier = 'T0-2'"
    ).fetchone()
    assert row["status"] == "planned"


# ---------------------------------------------------------------------------
# dispatch_loop health gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_loop_breaks_on_unhealthy(dispatch_env):
    env = dispatch_env
    for i in range(4):
        _seed_task(env["conn"], identifier=f"T0-{i+1}", title=f"task {i+1}")

    health = EngineHealth(consecutive_failure_threshold=2)

    from oxi_core.v3.dispatch import dispatch_loop

    # Two failures should trigger UNHEALTHY; loop should stop.
    results = await dispatch_loop(
        conn=env["conn"],
        adapter=env["adapter"],
        engine_state=env["engine_state"],
        engine_health=health,
        repo_root=env["repo_root"],
        max_iterations=4,
        binary=str(FAKE_CLAUDE),
        extra_env=_extra("exit_1_after_pr"),
        _check_commits=lambda _p, _b: False,
        _check_pr=lambda _b, _r: False,
    )
    # At most 2 failures (threshold=2); then loop stops.
    assert len(results) <= 2
    assert health.is_unhealthy()


# ---------------------------------------------------------------------------
# Dashboard health banner
# ---------------------------------------------------------------------------


def test_dashboard_shows_health_banner_when_unhealthy(conn):
    from oxi_core.v3.dashboard import render_html

    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(conn, reason="test")

    html_out = render_html(conn)
    assert "health-banner" in html_out
    assert "ENGINE UNHEALTHY" in html_out
    assert "oxi v3 heal" in html_out


def test_dashboard_no_health_banner_when_healthy(conn):
    from oxi_core.v3.dashboard import render_html

    html_out = render_html(conn)
    assert "health-banner" not in html_out
    assert "ENGINE UNHEALTHY" not in html_out


def test_dashboard_banner_cleared_after_heal(conn):
    from oxi_core.v3.dashboard import render_html

    h = EngineHealth(consecutive_failure_threshold=1)
    h.record_failure(conn, reason="test")
    h.heal(conn, operator="test heal")

    html_out = render_html(conn)
    assert "health-banner" not in html_out
    assert "ENGINE UNHEALTHY" not in html_out


# ---------------------------------------------------------------------------
# HEALTH_BANNER constant
# ---------------------------------------------------------------------------


def test_health_banner_mentions_heal_command():
    assert "oxi v3 heal" in HEALTH_BANNER
    assert "ENGINE UNHEALTHY" in HEALTH_BANNER


# ---------------------------------------------------------------------------
# CLI: oxi v3 heal
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "tmp": tmp_path,
    }
    handle.connection.close()


def test_cli_heal_clears_unhealthy_state(cli_env, capsys):
    from oxi_core.cli import main

    # Manually write an engine_unhealthy event to simulate prior state.
    conn = cli_env["conn"]
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
        (
            EVENT_KIND_UNHEALTHY,
            '{"consecutive_failures": 5, "reason": "FAILED", '
            '"timestamp": "2026-01-01 00:00:00"}',
        ),
    )
    conn.commit()
    assert is_engine_unhealthy_from_db(conn) is True

    rc = main(["v3", "heal"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "cleared" in out.lower() or "resume" in out.lower()

    # After heal, DB event chain says healthy.
    assert is_engine_unhealthy_from_db(conn) is False


def test_cli_heal_noop_when_already_healthy(cli_env, capsys):
    from oxi_core.cli import main

    rc = main(["v3", "heal"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "already healthy" in out.lower() or "nothing" in out.lower()


def test_cli_status_shows_unhealthy_warning(cli_env, capsys):
    from oxi_core.cli import main

    conn = cli_env["conn"]
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
        (
            EVENT_KIND_UNHEALTHY,
            '{"consecutive_failures": 5, "reason": "FAILED", '
            '"timestamp": "2026-01-01 00:00:00"}',
        ),
    )
    conn.commit()

    rc = main(["status"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "ENGINE UNHEALTHY" in out or "unhealthy" in out.lower()
