"""Tests for oxi_core.cli — argparse + command dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from oxi_core import adapter as adapter_mod
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
    err = capsys.readouterr().err.lower()
    # Either "no adapter" or "multiple adapters" — both are
    # registration-attention errors that exit 2. In CI both the
    # reference and self adapters are installed so `load_adapter`
    # hits MultipleAdaptersError; in dev with only one adapter
    # installed we'd see "no adapter".
    assert "no adapter" in err or "multiple adapters" in err


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
# ANTHROPIC_API_KEY plumbing
# ---------------------------------------------------------------------------


def test_real_claude_tick_passes_anthropic_api_key_from_env(
    _env, monkeypatch, capsys,
):
    """cmd_tick --real-claude must forward ANTHROPIC_API_KEY to dispatch.

    The dispatch_invoke env whitelist strips everything not explicitly
    passed. If the CLI doesn't plumb the key, the worker spawns with no
    credentials and fails in under a second. That was the first bug the
    dogfood loop caught.
    """
    import asyncio

    from oxi_core.v3 import dispatch

    captured: dict = {}

    async def _fake_dispatch_one(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-cli-plumbing-sentinel")
    monkeypatch.setattr(dispatch, "dispatch_one", _fake_dispatch_one)

    # pr_watcher + auto_merge call downstream; stub them to no-ops so we
    # only observe the dispatch path.
    from oxi_core.v3 import auto_merge, pr_watcher

    class _EmptyReport:
        pr_numbers_stamped = 0
        tasks_transitioned_merged = 0
        tasks_transitioned_failed = 0
        considered = 0
        merged = 0
        critic_rejected = 0

    monkeypatch.setattr(pr_watcher, "watch", lambda *a, **k: _EmptyReport())
    monkeypatch.setattr(auto_merge, "run", lambda *a, **k: _EmptyReport())
    # Prevent asyncio.run nesting issues when tests run under a loop.
    def _sync_run(coro):
        return asyncio.new_event_loop().run_until_complete(coro)
    monkeypatch.setattr(asyncio, "run", _sync_run)

    rc = main(["v3", "tick", "--real-claude", "--times", "1"])
    assert rc == 0
    assert captured.get("anthropic_api_key") == "sk-ant-test-cli-plumbing-sentinel"


def test_real_claude_tick_passes_none_when_env_missing(
    _env, monkeypatch, capsys,
):
    """If ANTHROPIC_API_KEY is absent, pass None (not an empty string).

    Preserves the dispatch_one contract: the dispatch layer decides what
    to do with a missing key (today it proceeds and lets claude fail
    fast with an auth error).
    """
    import asyncio

    from oxi_core.v3 import auto_merge, dispatch, pr_watcher

    captured: dict = {}

    async def _fake_dispatch_one(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(dispatch, "dispatch_one", _fake_dispatch_one)

    class _EmptyReport:
        pr_numbers_stamped = 0
        tasks_transitioned_merged = 0
        tasks_transitioned_failed = 0
        considered = 0
        merged = 0
        critic_rejected = 0

    monkeypatch.setattr(pr_watcher, "watch", lambda *a, **k: _EmptyReport())
    monkeypatch.setattr(auto_merge, "run", lambda *a, **k: _EmptyReport())
    def _sync_run(coro):
        return asyncio.new_event_loop().run_until_complete(coro)
    monkeypatch.setattr(asyncio, "run", _sync_run)

    rc = main(["v3", "tick", "--real-claude", "--times", "1"])
    assert rc == 0
    assert captured.get("anthropic_api_key") is None


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


# ---------------------------------------------------------------------------
# Auto-discovery via OXI_ADAPTER env var (_require_adapter integration)
# ---------------------------------------------------------------------------


def test_require_adapter_auto_loads_via_env_var(tmp_path, monkeypatch, capsys):
    """_require_adapter loads adapter from OXI_ADAPTER env var (no pre-registration)."""
    import sys
    import types

    clear_adapter()

    db_path = str(tmp_path / "oxi.db")
    ks_path = str(tmp_path / "KS")

    # Inject a synthetic module so importlib.import_module can find the factory.
    fake_mod = types.ModuleType("_oxi_cli_test_auto")

    def _make():
        return _Adapter(db_path_value=db_path, release_lock_value=ks_path)

    fake_mod.make = _make  # type: ignore[attr-defined]
    sys.modules["_oxi_cli_test_auto"] = fake_mod
    try:
        monkeypatch.setenv("OXI_ADAPTER", "_oxi_cli_test_auto:make")
        rc = main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "plan tier" in out
    finally:
        del sys.modules["_oxi_cli_test_auto"]


def test_require_adapter_multiple_entry_points_exits_2(monkeypatch, capsys):
    """Multiple installed entry-points with no env var → exit 2, clear message."""
    clear_adapter()
    monkeypatch.delenv("OXI_ADAPTER", raising=False)

    class _FakeEP:
        def __init__(self, name):
            self.name = name
            self.value = "..."
        def load(self):
            return _Adapter

    monkeypatch.setattr(
        adapter_mod, "entry_points",
        lambda group: [_FakeEP("alpha"), _FakeEP("beta")],
    )

    with pytest.raises(SystemExit) as exc:
        main(["status"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "multiple" in err.lower()
    assert "OXI_ADAPTER" in err


def test_require_adapter_bad_env_var_exits_2(monkeypatch, capsys):
    """Malformed OXI_ADAPTER value → exit 2 with 'adapter load failed' message."""
    clear_adapter()
    monkeypatch.setenv("OXI_ADAPTER", "no_colon_here")

    with pytest.raises(SystemExit) as exc:
        main(["status"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "adapter load failed" in err.lower()
