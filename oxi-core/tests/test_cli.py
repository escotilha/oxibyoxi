"""Tests for oxi_core.cli — argparse + command dispatch."""

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
from oxi_core.cli import main


@dataclass
class _Adapter:
    db_path_value: str
    release_lock_value: str

    def naming(self): return NamingConfig(instance_name="oxi-test")
    def paths(self):
        return PathsConfig(
            db_path=self.db_path_value,
            release_lock_path=self.release_lock_value,
        )
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
def _env(tmp_path: Path):
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        release_lock_value=str(tmp_path / "KS"),
    ))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "tmp": tmp_path,
        "ks_path": tmp_path / "KS",
    }
    handle.connection.close()


# ---------------------------------------------------------------------------
# No-args banner
# ---------------------------------------------------------------------------


def test_no_args_prints_banner(capsys):
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "oxi" in captured.out


# ---------------------------------------------------------------------------
# `oxi status`
# ---------------------------------------------------------------------------


def test_status_shows_plan_tier_and_repo(_env, capsys):
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "plan tier" in out
    assert "standard" in out
    assert "owner/repo" in out
    assert "oxi-test" in out


def test_status_shows_task_counts(_env, capsys):
    _env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T0-1', 0, 't', 'dispatched')"
    )
    _env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T0-2', 0, 't', 'merged')"
    )
    _env["conn"].commit()

    main(["status"])
    out = capsys.readouterr().out
    assert "dispatched" in out
    assert "merged" in out


def test_status_missing_adapter_exits_2(capsys):
    clear_adapter()
    with pytest.raises(SystemExit) as exc:
        main(["status"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no adapter" in err.lower()


# ---------------------------------------------------------------------------
# `oxi v3 status` (alias)
# ---------------------------------------------------------------------------


def test_v3_status_is_alias(_env, capsys):
    rc = main(["v3", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "plan tier" in out


# ---------------------------------------------------------------------------
# `oxi brief`
# ---------------------------------------------------------------------------


def test_brief_writes_markdown_to_stdout(_env, capsys):
    rc = main(["brief", "--hours", "48"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# oxi brief" in out
    assert "Window: last 48h" in out


def test_brief_write_flag_creates_file(_env, tmp_path: Path, capsys):
    # Adapter doesn't set brief_path, so falls back to .oxi/brief.md
    # relative to repo_root (or "." if not set).
    # Point repo_root at tmp so we can observe.

    @dataclass
    class _A2:
        db: str
        root: str
        def naming(self): return NamingConfig()
        def paths(self):
            return PathsConfig(db_path=self.db, repo_root=self.root)
        def budget(self): return BudgetCaps()
        def github_repo(self): return "o/r"
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

    clear_adapter()
    register_adapter(_A2(db=str(tmp_path / "oxi.db"), root=str(tmp_path)))

    rc = main(["brief", "--write"])
    assert rc == 0
    brief_path = tmp_path / ".oxi" / "brief.md"
    assert brief_path.exists()
    assert "# oxi brief" in brief_path.read_text()
    out = capsys.readouterr().out
    assert str(brief_path) in out


# ---------------------------------------------------------------------------
# `oxi v3 kill` / `unkill`
# ---------------------------------------------------------------------------


def test_kill_creates_file(_env, capsys):
    assert not _env["ks_path"].exists()
    rc = main(["v3", "kill", "--reason", "test halt"])
    assert rc == 0
    assert _env["ks_path"].exists()
    assert "test halt" in _env["ks_path"].read_text()
    out = capsys.readouterr().out
    assert "killswitch set" in out


def test_unkill_removes_file(_env, capsys):
    main(["v3", "kill"])
    assert _env["ks_path"].exists()
    rc = main(["v3", "unkill"])
    assert rc == 0
    assert not _env["ks_path"].exists()
    out = capsys.readouterr().out
    assert "killswitch cleared" in out


def test_unkill_when_absent_is_noop(_env, capsys):
    assert not _env["ks_path"].exists()
    rc = main(["v3", "unkill"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not present" in out


# ---------------------------------------------------------------------------
# `oxi v3 tick`
# ---------------------------------------------------------------------------


def test_tick_with_killswitch_is_noop(_env, capsys):
    # Set the killswitch before ticking.
    from oxi_core.v3 import kill as kill_mod
    kill_mod.create(reason="held")

    rc = main(["v3", "tick", "--times", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "killswitch is set" in out.lower()


def test_tick_runs_heartbeat_without_crashing(_env, capsys):
    # No tasks — heartbeat reaps nothing, but the command succeeds.
    rc = main(["v3", "tick", "--times", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tick done" in out.lower()


# ---------------------------------------------------------------------------
# Unknown command
# ---------------------------------------------------------------------------


def test_unknown_subcommand_prints_help_and_exits_2(_env):
    # argparse prints to stderr and raises SystemExit(2) on unknown args.
    with pytest.raises(SystemExit):
        main(["nope"])


# ---------------------------------------------------------------------------
# `oxi --version`
# ---------------------------------------------------------------------------


def test_version_flag_exits_cleanly(capsys):
    from oxi_core import __version__
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
