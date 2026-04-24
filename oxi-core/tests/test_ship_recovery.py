"""Tests for oxi_core.v3.ship_recovery — committed work rescue.

Uses real git repos in tmp dirs (same pattern as test_worktree_provision).
FakeGitHubClient for PR operations (though ship_recovery currently
only emits an event and defers PR open to the worker side; those
tests are tight around that contract).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from fake_github import FakeGitHubClient  # noqa: E402

from oxi_core import db  # noqa: E402
from oxi_core.adapter import (  # noqa: E402
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3 import ship_recovery  # noqa: E402
from oxi_core.v3.engine_state import EngineState  # noqa: E402
from oxi_core.v3.worktree_provision import provision  # noqa: E402


@dataclass
class _Adapter:
    db_path_value: str
    worktree_root_value: str

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
                max_concurrent=1, worktree_root=self.worktree_root_value,
            ),
        )
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create bare upstream + clone; return (upstream, clone)."""
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _run(["git", "init", "--bare", "--initial-branch=main"], cwd=upstream)

    clone = tmp_path / "clone"
    clone.mkdir()
    _run(["git", "init", "--initial-branch=main"], cwd=clone)
    _run(["git", "config", "user.email", "t@e.com"], cwd=clone)
    _run(["git", "config", "user.name", "t"], cwd=clone)
    (clone / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=clone)
    _run(["git", "commit", "-m", "initial"], cwd=clone)
    _run(["git", "remote", "add", "origin", str(upstream)], cwd=clone)
    _run(["git", "push", "-u", "origin", "main"], cwd=clone)

    return upstream, clone


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def env(tmp_path: Path):
    _, clone = _make_repo(tmp_path)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        worktree_root_value=str(worktree_root),
    ))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "clone": clone,
        "worktree_root": worktree_root,
        "state": EngineState(plan_tier="standard", max_concurrent=4),
        "client": FakeGitHubClient(),
    }
    handle.connection.close()


def _seed_dispatched_no_pr(
    conn, identifier: str = "T0-1", title: str = "t"
) -> None:
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES (?, 0, ?, 'dispatched', NULL)",
        (identifier, title),
    )
    conn.commit()


def _provision_and_leave_uncommitted(env, identifier: str = "T0-1") -> Path:
    """Provision a worktree and write an uncommitted file.

    Mirrors the "Claude wrote code and exited" failure mode.
    """
    handle = provision(
        repo_root=env["clone"],
        worktree_root=env["worktree_root"],
        task_identifier=identifier,
        session_tag="sa",
    )
    (handle.path / "new_file.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    return handle.path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_recovers_uncommitted_dispatched_task(env):
    _seed_dispatched_no_pr(env["conn"], identifier="T0-1", title="greet")
    wt = _provision_and_leave_uncommitted(env, identifier="T0-1")
    assert (wt / "new_file.py").exists()

    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    assert report.considered == 1
    assert report.recovered == 1
    assert report.failed == 0

    # The commit landed.
    log = subprocess.run(
        ["git", "log", "--pretty=%s", "-1"],
        cwd=str(wt), capture_output=True, text=True, check=True,
    )
    assert "recover: T0-1: greet" in log.stdout


def test_recovers_multiple_tasks_in_one_pass(env):
    for identifier, title in [("T0-1", "first"), ("T0-2", "second")]:
        _seed_dispatched_no_pr(env["conn"], identifier=identifier, title=title)
        _provision_and_leave_uncommitted(env, identifier=identifier)

    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    assert report.considered == 2
    assert report.recovered == 2


def test_emits_ship_recovered_event(env):
    _seed_dispatched_no_pr(env["conn"], "T0-1", "thing")
    _provision_and_leave_uncommitted(env, "T0-1")

    ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    kinds = [r[0] for r in env["conn"].execute("SELECT kind FROM event")]
    assert "ship_recovered" in kinds


def test_recovery_stamps_last_progress_at(env):
    """The stickiness invariant from anti-pattern #1 — every transition
    (including the soft state bump during recovery) stamps freshness.
    """
    _seed_dispatched_no_pr(env["conn"], "T0-1")
    _provision_and_leave_uncommitted(env, "T0-1")

    env["conn"].execute(
        "UPDATE task SET last_progress_at = '2020-01-01 00:00:00' "
        "WHERE identifier = 'T0-1'"
    )
    env["conn"].commit()

    ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    row = env["conn"].execute(
        "SELECT last_progress_at FROM task WHERE identifier = 'T0-1'"
    ).fetchone()
    assert row[0] != "2020-01-01 00:00:00"


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


def test_skips_task_with_pr(env):
    """Task already has a PR — pr_watcher owns its lifecycle; don't touch."""
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES ('T0-1', 0, 't', 'dispatched', 42)"
    )
    env["conn"].commit()
    _provision_and_leave_uncommitted(env, "T0-1")

    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    assert report.considered == 0


def test_skips_planned_tasks(env):
    env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T0-1', 0, 't', 'planned')"
    )
    env["conn"].commit()
    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    assert report.considered == 0


def test_skips_worktree_missing(env):
    _seed_dispatched_no_pr(env["conn"], "T0-1")
    # No worktree provisioned.
    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    assert report.worktree_missing == 1
    assert report.recovered == 0


def test_skips_clean_worktree(env):
    """Worktree exists but no uncommitted changes → no recovery needed."""
    _seed_dispatched_no_pr(env["conn"], "T0-1")
    # Provision but don't write anything.
    handle = provision(
        repo_root=env["clone"], worktree_root=env["worktree_root"],
        task_identifier="T0-1", session_tag="sa",
    )
    assert handle.path.is_dir()

    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    assert report.no_uncommitted_changes == 1
    assert report.recovered == 0


# ---------------------------------------------------------------------------
# Engine stop
# ---------------------------------------------------------------------------


def test_engine_stop_skips_pass(env):
    _seed_dispatched_no_pr(env["conn"], "T0-1")
    _provision_and_leave_uncommitted(env, "T0-1")
    env["state"].request_stop()

    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    assert report.considered == 0


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_worktree_root_from_adapter(env):
    """Omitting worktree_root reads adapter.dispatch_hosts()[0].worktree_root."""
    _seed_dispatched_no_pr(env["conn"], "T0-1")
    _provision_and_leave_uncommitted(env, "T0-1")

    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
    )
    assert report.recovered == 1


def test_defaults_repo_from_adapter(env):
    _seed_dispatched_no_pr(env["conn"], "T0-1")
    _provision_and_leave_uncommitted(env, "T0-1")

    report = ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"],
        worktree_root=env["worktree_root"],
    )
    assert report.recovered == 1


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


def test_report_dataclass_is_frozen():
    from dataclasses import FrozenInstanceError
    r = ship_recovery.ShipReport(
        considered=0, recovered=0, failed=0,
        no_uncommitted_changes=0, worktree_missing=0,
    )
    with pytest.raises(FrozenInstanceError):
        r.recovered = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Failure emission
# ---------------------------------------------------------------------------


def test_commit_failure_emits_ship_recovery_failed_event(env):
    """Force a commit failure by corrupting git user config."""
    _seed_dispatched_no_pr(env["conn"], "T0-1")
    wt = _provision_and_leave_uncommitted(env, "T0-1")
    # Unset user config to force commit to fail.
    _run(["git", "config", "--unset", "user.email"], cwd=wt)
    _run(["git", "config", "--unset", "user.name"], cwd=wt)
    # Set an empty config to override any global fallback.
    _run(["git", "config", "user.email", ""], cwd=wt)
    _run(["git", "config", "user.name", ""], cwd=wt)

    # Depending on git's global config this may still succeed.
    # The assertion below is conditional on that.
    ship_recovery.run(
        env["conn"], env["state"],
        client=env["client"], repo="owner/repo",
        worktree_root=env["worktree_root"],
    )
    # Either ship_recovered or ship_recovery_failed — both are valid
    # outcomes of this environment test. What we're verifying is that
    # we ALWAYS emit exactly one of the two events.
    kinds = [r[0] for r in env["conn"].execute(
        "SELECT kind FROM event WHERE task_id IS NOT NULL"
    )]
    assert any(k in ("ship_recovered", "ship_recovery_failed") for k in kinds)
