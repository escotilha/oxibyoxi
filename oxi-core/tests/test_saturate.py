"""Tests for oxi_core.v3.saturate — continuous-dispatch supervisor.

These tests exercise:
- PID file lifecycle (write / check / remove).
- ``_day_spend_exceeds_cap`` (the CLI-level budget ceiling).
- ``run()`` stop-paths: killswitch, daily_budget_reached, sigterm, budget hard-stop.
- ``saturate_started`` and ``saturate_stopped`` ledger events.
- CLI integration via ``cmd_saturate`` (``oxi v3 saturate``).

All dispatch calls are stubbed — no real Claude invocations.  The
async ``run()`` helper takes injectable sleep/dispatch so tests complete
in milliseconds.
"""

from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path

import pytest

from oxi_core import db as db_mod
from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3 import saturate as sat_mod

# ---------------------------------------------------------------------------
# Minimal fake adapter
# ---------------------------------------------------------------------------


@dataclass
class _FakeAdapter:
    db_path_value: str
    repo_root_value: str
    release_lock_value: str
    daily_hard_cap: float = 100.0

    def naming(self):
        return NamingConfig(instance_name="test-sat")

    def paths(self):
        return PathsConfig(
            db_path=self.db_path_value,
            repo_root=self.repo_root_value,
            release_lock_path=self.release_lock_value,
        )

    def budget(self):
        return BudgetCaps(
            daily_soft_warn=50.0,
            daily_hard_cap=self.daily_hard_cap,
            per_task_opus=2.0,
            per_task_sonnet=1.0,
        )

    def github_repo(self):
        return "owner/repo"

    def roadmap_location(self):
        return "roadmap.md"

    def branch_prefixes(self):
        return ("feat/",)

    def dispatch_hosts(self):
        return (
            DispatchHost(
                name="local",
                ssh_alias=None,
                max_concurrent=2,
                worktree_root=str(Path(self.repo_root_value) / ".oxi" / "worktrees"),
            ),
        )

    def promote_recipe(self):
        return None

    def plan_tier(self):
        return "max_20x"

    def policy(self):
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
def env(tmp_path: Path):
    """Set up a temporary adapter + DB + roadmap."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    oxi_dir = repo_root / ".oxi"
    oxi_dir.mkdir()
    ks_path = oxi_dir / "KILLSWITCH"
    roadmap = repo_root / "roadmap.md"
    roadmap.write_text("# Roadmap\n")  # empty but parseable

    adapter = _FakeAdapter(
        db_path_value=str(oxi_dir / "oxi.db"),
        repo_root_value=str(repo_root),
        release_lock_value=str(ks_path),
    )
    register_adapter(adapter)

    handle = db_mod.connect()
    yield {
        "conn": handle.connection,
        "repo_root": repo_root,
        "ks_path": ks_path,
        "oxi_dir": ".oxi",
        "adapter": adapter,
    }
    handle.connection.close()


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


class TestPidHelpers:
    def test_pid_path_returns_expected_location(self, env):
        p = sat_mod.pid_path(env["repo_root"], ".oxi")
        assert p == env["repo_root"] / ".oxi" / "saturate.pid"

    def test_write_and_read_pid_file(self, env):
        p = sat_mod.pid_path(env["repo_root"], ".oxi")
        sat_mod.write_pid_file(p)
        assert p.exists()
        content = p.read_text().strip()
        assert content == str(os.getpid())

    def test_remove_pid_file(self, env):
        p = sat_mod.pid_path(env["repo_root"], ".oxi")
        sat_mod.write_pid_file(p)
        sat_mod.remove_pid_file(p)
        assert not p.exists()

    def test_remove_pid_file_nonexistent_is_noop(self, env):
        p = sat_mod.pid_path(env["repo_root"], ".oxi")
        sat_mod.remove_pid_file(p)  # should not raise

    def test_check_pid_file_no_file(self, env):
        p = sat_mod.pid_path(env["repo_root"], ".oxi")
        live, pid = sat_mod.check_pid_file(p)
        assert not live
        assert pid is None

    def test_check_pid_file_current_process(self, env):
        p = sat_mod.pid_path(env["repo_root"], ".oxi")
        sat_mod.write_pid_file(p)
        live, pid = sat_mod.check_pid_file(p)
        assert live
        assert pid == os.getpid()

    def test_check_pid_file_dead_pid(self, env, tmp_path):
        p = sat_mod.pid_path(env["repo_root"], ".oxi")
        # Use a PID that is almost certainly not running.
        dead_pid = 2_000_000
        p.write_text(str(dead_pid) + "\n")
        live, pid = sat_mod.check_pid_file(p)
        assert not live
        assert pid == dead_pid

    def test_check_pid_file_unparseable(self, env):
        p = sat_mod.pid_path(env["repo_root"], ".oxi")
        p.write_text("not-a-number\n")
        live, pid = sat_mod.check_pid_file(p)
        assert not live
        assert pid is None


# ---------------------------------------------------------------------------
# _day_spend_exceeds_cap
# ---------------------------------------------------------------------------


class TestDaySpendExceedsCap:
    def test_none_cap_never_triggers(self, env):
        assert not sat_mod._day_spend_exceeds_cap(env["conn"], None)

    def test_zero_spend_does_not_exceed_positive_cap(self, env):
        assert not sat_mod._day_spend_exceeds_cap(env["conn"], 10.0)

    def test_spend_at_cap_triggers(self, env):
        # Insert a task with cost_usd = cap.
        env["conn"].execute(
            "INSERT INTO task (identifier, tier, title, status, cost_usd, updated_at) "
            "VALUES ('T0-1', 0, 'test', 'dispatched', 10.0, datetime('now'))"
        )
        env["conn"].commit()
        assert sat_mod._day_spend_exceeds_cap(env["conn"], 10.0)

    def test_spend_below_cap_does_not_trigger(self, env):
        env["conn"].execute(
            "INSERT INTO task (identifier, tier, title, status, cost_usd, updated_at) "
            "VALUES ('T0-1', 0, 'test', 'dispatched', 5.0, datetime('now'))"
        )
        env["conn"].commit()
        assert not sat_mod._day_spend_exceeds_cap(env["conn"], 10.0)


# ---------------------------------------------------------------------------
# run() — stop via killswitch
# ---------------------------------------------------------------------------


class TestRunKillswitch:
    @pytest.mark.asyncio
    async def test_stops_when_killswitch_set(self, env, monkeypatch):
        """If the killswitch is already set, run() should exit immediately."""
        # Set the killswitch.
        env["ks_path"].write_text("test stop\n")

        # Stub dispatch_loop to assert it is never called.
        dispatch_called = []

        async def _fake_dispatch_loop(**kwargs):
            dispatch_called.append(1)
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        report = await sat_mod.run(
            conn=env["conn"],
            concurrency=1,
            repo_root=env["repo_root"],
            oxi_dir=env["oxi_dir"],
            idle_sleep_s=0,
            active_sleep_s=0,
        )

        assert report.stop_reason == "killswitch"
        assert report.exit_code == 0
        assert not dispatch_called


# ---------------------------------------------------------------------------
# run() — stop via daily_budget_reached (CLI cap)
# ---------------------------------------------------------------------------


class TestRunDailyBudgetReached:
    @pytest.mark.asyncio
    async def test_stops_when_cli_cap_reached(self, env, monkeypatch):
        """run() exits with daily_budget_reached when CLI cap is exhausted."""
        # Insert spend at the cap.
        env["conn"].execute(
            "INSERT INTO task (identifier, tier, title, status, cost_usd, updated_at) "
            "VALUES ('T0-1', 0, 'spend', 'dispatched', 5.0, datetime('now'))"
        )
        env["conn"].commit()

        async def _fake_dispatch_loop(**kwargs):
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        report = await sat_mod.run(
            conn=env["conn"],
            concurrency=1,
            max_cost_per_day_usd=5.0,  # exactly at cap
            repo_root=env["repo_root"],
            oxi_dir=env["oxi_dir"],
            idle_sleep_s=0,
            active_sleep_s=0,
        )

        assert report.stop_reason == "daily_budget_reached"
        assert report.exit_code == 0

        # Verify the daily_budget_reached event was written.
        row = env["conn"].execute(
            "SELECT payload FROM event WHERE kind = 'daily_budget_reached'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        assert payload["max_cost_per_day_usd"] == 5.0


# ---------------------------------------------------------------------------
# run() — adapter budget hard-stop
# ---------------------------------------------------------------------------


class TestRunAdapterHardStop:
    @pytest.mark.asyncio
    async def test_stops_on_adapter_budget_hard_stop(self, env, monkeypatch):
        """run() exits when budget.is_hard_stopped() returns True."""
        import oxi_core.v3.budget as budget_mod
        monkeypatch.setattr(budget_mod, "is_hard_stopped", lambda conn: True)

        async def _fake_dispatch_loop(**kwargs):
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        report = await sat_mod.run(
            conn=env["conn"],
            concurrency=1,
            repo_root=env["repo_root"],
            oxi_dir=env["oxi_dir"],
            idle_sleep_s=0,
            active_sleep_s=0,
        )

        assert report.stop_reason == "budget_hard_stop"
        assert report.exit_code == 0


# ---------------------------------------------------------------------------
# run() — engine_unhealthy stop
# ---------------------------------------------------------------------------


class TestRunEngineUnhealthy:
    @pytest.mark.asyncio
    async def test_stops_when_engine_unhealthy(self, env, monkeypatch):
        """run() exits with exit_code=1 when EngineHealth is unhealthy."""
        import oxi_core.v3.engine_health as eh_mod

        call_count = [0]

        def _fake_is_unhealthy(self):
            call_count[0] += 1
            # Unhealthy from the very first iteration check.
            return True

        monkeypatch.setattr(eh_mod.EngineHealth, "is_unhealthy", _fake_is_unhealthy)

        async def _fake_dispatch_loop(**kwargs):
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        report = await sat_mod.run(
            conn=env["conn"],
            concurrency=1,
            repo_root=env["repo_root"],
            oxi_dir=env["oxi_dir"],
            idle_sleep_s=0,
            active_sleep_s=0,
        )

        assert report.stop_reason == "engine_unhealthy"
        assert report.exit_code == 1


# ---------------------------------------------------------------------------
# run() — ledger events emitted
# ---------------------------------------------------------------------------


class TestRunLedgerEvents:
    @pytest.mark.asyncio
    async def test_saturate_started_and_stopped_events(self, env, monkeypatch):
        """run() always emits saturate_started and saturate_stopped events."""
        # Force immediate stop via killswitch.
        env["ks_path"].write_text("test\n")

        async def _fake_dispatch_loop(**kwargs):
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        await sat_mod.run(
            conn=env["conn"],
            concurrency=1,
            repo_root=env["repo_root"],
            oxi_dir=env["oxi_dir"],
            idle_sleep_s=0,
            active_sleep_s=0,
        )

        # Both events must exist.
        started_row = env["conn"].execute(
            "SELECT payload FROM event WHERE kind = 'saturate_started'"
        ).fetchone()
        assert started_row is not None
        started = json.loads(started_row[0])
        assert started["pid"] == os.getpid()
        assert started["concurrency"] == 1

        stopped_row = env["conn"].execute(
            "SELECT payload FROM event WHERE kind = 'saturate_stopped'"
        ).fetchone()
        assert stopped_row is not None
        stopped = json.loads(stopped_row[0])
        assert stopped["stop_reason"] == "killswitch"
        assert stopped["exit_code"] == 0


# ---------------------------------------------------------------------------
# run() — PID file lifecycle
# ---------------------------------------------------------------------------


class TestRunPidLifecycle:
    @pytest.mark.asyncio
    async def test_pid_file_written_and_cleaned_up(self, env, monkeypatch):
        """PID file exists during run() and is removed on exit."""
        env["ks_path"].write_text("test\n")
        pid_file = sat_mod.pid_path(env["repo_root"], ".oxi")

        async def _fake_dispatch_loop(**kwargs):
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        # We can't easily observe the PID file mid-run in a single-threaded
        # test, so we verify pre/post invariants.
        assert not pid_file.exists()

        await sat_mod.run(
            conn=env["conn"],
            concurrency=1,
            repo_root=env["repo_root"],
            oxi_dir=env["oxi_dir"],
            idle_sleep_s=0,
            active_sleep_s=0,
        )

        assert not pid_file.exists()


# ---------------------------------------------------------------------------
# run() — one iteration dispatches work, then killswitch stops it
# ---------------------------------------------------------------------------


class TestRunOneIteration:
    @pytest.mark.asyncio
    async def test_dispatch_loop_called_and_results_counted(self, env, monkeypatch):
        """run() calls dispatch_loop and counts results."""
        from oxi_core.v3.dispatch_invoke import Classification, DispatchResult

        results_to_return = [
            DispatchResult(
                classification=Classification.SUCCESS,
                exit_code=0,
                session_id="abc123",
                events=[],
                trailing_line="",
                stderr_text="",
                cost_usd=0.10,
                wall_clock_seconds=1.0,
            )
        ]
        call_count = [0]

        async def _fake_dispatch_loop(**kwargs):
            call_count[0] += 1
            # Only return results on the first call; second call sets killswitch.
            if call_count[0] == 1:
                return results_to_return
            # On second call set killswitch so loop exits.
            env["ks_path"].write_text("done\n")
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        # Stub ingest so FileNotFoundError doesn't spam (roadmap exists but is empty).
        report = await sat_mod.run(
            conn=env["conn"],
            concurrency=1,
            repo_root=env["repo_root"],
            oxi_dir=env["oxi_dir"],
            idle_sleep_s=0,
            active_sleep_s=0,
        )

        assert report.total_dispatched == 1
        assert report.stop_reason == "killswitch"


# ---------------------------------------------------------------------------
# CLI integration — oxi v3 saturate
# ---------------------------------------------------------------------------


class TestCliSaturate:
    def test_saturate_subcommand_registered(self):
        """``oxi v3 saturate --help`` should exit 0 without error."""
        from oxi_core.cli import _build_parser

        parser = _build_parser()
        # argparse raises SystemExit(0) on --help; we just check no KeyError.
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["v3", "saturate", "--help"])
        assert exc.value.code == 0

    def test_saturate_refuses_if_live_pid(self, env, monkeypatch, capsys):
        """``oxi v3 saturate`` exits 2 if a live saturate is running."""
        from oxi_core.cli import main

        pid_file = sat_mod.pid_path(env["repo_root"], ".oxi")
        # Write current PID → will be detected as live.
        sat_mod.write_pid_file(pid_file)

        with pytest.raises(SystemExit) as exc:
            main(["v3", "saturate"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "already running" in err

    def test_saturate_killswitch_stop(self, env, monkeypatch, capsys):
        """``oxi v3 saturate`` exits 0 when killswitch is pre-set."""
        from oxi_core.cli import main

        # Pre-set killswitch so the loop exits immediately.
        env["ks_path"].write_text("pre-test stop\n")

        async def _fake_dispatch_loop(**kwargs):
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        rc = main(["v3", "saturate", "--concurrency", "2"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "saturate" in out
        assert "stopped" in out

    def test_saturate_max_cost_flag_parsed(self, env, monkeypatch, capsys):
        """``--max-cost-per-day`` is forwarded to run()."""
        from oxi_core.cli import main

        env["ks_path"].write_text("stop\n")
        run_kwargs = {}

        async def _fake_run(**kwargs):
            run_kwargs.update(kwargs)
            return sat_mod.SaturateReport(
                iterations=0,
                total_dispatched=0,
                stop_reason="killswitch",
                exit_code=0,
            )

        monkeypatch.setattr(sat_mod, "run", _fake_run)

        rc = main(["v3", "saturate", "--max-cost-per-day", "7.50"])
        assert rc == 0
        assert run_kwargs.get("max_cost_per_day_usd") == pytest.approx(7.50)

    def test_saturate_concurrency_default_is_1(self, env, monkeypatch, capsys):
        """Default concurrency is 1 when no --concurrency flag is given."""
        from oxi_core.cli import main

        env["ks_path"].write_text("stop\n")
        run_kwargs = {}

        async def _fake_run(**kwargs):
            run_kwargs.update(kwargs)
            return sat_mod.SaturateReport(
                iterations=0,
                total_dispatched=0,
                stop_reason="killswitch",
                exit_code=0,
            )

        monkeypatch.setattr(sat_mod, "run", _fake_run)

        rc = main(["v3", "saturate"])
        assert rc == 0
        assert run_kwargs.get("concurrency") == 1

    def test_saturate_sigterm_stop(self, env, monkeypatch, capsys):
        """SIGTERM sets the stop flag and the loop exits cleanly."""
        from oxi_core.cli import main

        # We'll inject a SIGTERM after the first dispatch iteration starts.
        call_count = [0]

        async def _fake_dispatch_loop(**kwargs):
            call_count[0] += 1
            if call_count[0] >= 1:
                # Send SIGTERM to ourselves.
                os.kill(os.getpid(), signal.SIGTERM)
            return []

        import oxi_core.v3.dispatch as dispatch_mod
        monkeypatch.setattr(dispatch_mod, "dispatch_loop", _fake_dispatch_loop)

        rc = main(["v3", "saturate"])
        assert rc == 0
        out = capsys.readouterr().out
        # The loop exits — "saturate stopped" should appear.
        assert "stopped" in out
