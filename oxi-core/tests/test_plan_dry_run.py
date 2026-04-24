"""Tests for oxi_core.v3.plan_dry_run and `oxi v3 plan --dry-run` CLI command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

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
from oxi_core.v3.plan_dry_run import DryRunReport, dry_run, format_report

# ---------------------------------------------------------------------------
# Minimal adapter used by CLI tests
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    db_path_value: str
    repo_root_value: str
    roadmap_rel: str = "roadmap.md"

    def naming(self): return NamingConfig(instance_name="oxi-test")
    def paths(self):
        return PathsConfig(
            db_path=self.db_path_value,
            repo_root=self.repo_root_value,
        )
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return self.roadmap_rel
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


# ---------------------------------------------------------------------------
# dry_run() — unit tests (no adapter, no DB)
# ---------------------------------------------------------------------------


def test_dry_run_returns_all_items(tmp_path: Path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(
        "## Tier 0\n\n"
        "**T0-1 · first task**\n_subtitle one_\n\n"
        "**T0-2 · second task**\n\n"
        "## Tier 1\n\n"
        "**T1-1 · third task**\n"
    )
    report = dry_run(roadmap)
    assert report.ok
    assert report.parse_error is None
    assert len(report.items) == 3
    assert report.items[0].identifier == "T0-1"
    assert report.items[0].tier == 0
    assert report.items[0].title == "first task"
    assert report.items[0].subtitle == "subtitle one"
    assert report.items[1].identifier == "T0-2"
    assert report.items[1].subtitle == ""
    assert report.items[2].identifier == "T1-1"
    assert report.items[2].tier == 1


def test_dry_run_empty_roadmap_returns_zero_items(tmp_path: Path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("# Just a heading\n\nNo items here.\n")
    report = dry_run(roadmap)
    assert report.ok
    assert len(report.items) == 0


def test_dry_run_parse_error_captured_not_raised(tmp_path: Path):
    """Item before any Tier heading → parse error captured in report."""
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("**T0-1 · oops, no tier heading**\n")
    report = dry_run(roadmap)
    assert not report.ok
    assert report.parse_error is not None
    assert "T0-1" in report.parse_error
    assert len(report.items) == 0


def test_dry_run_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        dry_run(tmp_path / "does-not-exist.md")


def test_dry_run_does_not_write_to_db(tmp_path: Path):
    """Sanity: no DB file should be created by dry_run."""
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("## Tier 0\n\n**T0-1 · task**\n")
    dry_run(roadmap)
    db_files = list(tmp_path.glob("*.db"))
    assert db_files == []


def test_dry_run_report_is_frozen():
    from dataclasses import FrozenInstanceError
    report = DryRunReport(items=())
    with pytest.raises(FrozenInstanceError):
        report.items = ()  # type: ignore[misc]


def test_dry_run_duplicate_identifiers_captured_as_error(tmp_path: Path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(
        "## Tier 0\n\n**T0-1 · first**\n\n**T0-1 · duplicate**\n"
    )
    report = dry_run(roadmap)
    assert not report.ok
    assert "T0-1" in (report.parse_error or "")


# ---------------------------------------------------------------------------
# format_report() — output shape
# ---------------------------------------------------------------------------


def test_format_report_happy_path(tmp_path: Path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(
        "## Tier 0\n\n**T0-1 · first task**\n_a subtitle_\n\n**T0-2 · second**\n"
    )
    report = dry_run(roadmap)
    out = format_report(report, roadmap)
    assert "found 2 items" in out
    assert "T0-1" in out
    assert "first task" in out
    assert "a subtitle" in out
    assert "T0-2" in out
    assert "second" in out
    assert "DB not touched" in out


def test_format_report_zero_items(tmp_path: Path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("# No items\n")
    report = dry_run(roadmap)
    out = format_report(report, roadmap)
    assert "found 0 items" in out
    assert "## Tier" in out  # shows expected format hint


def test_format_report_parse_error(tmp_path: Path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("**T0-1 · no tier heading**\n")
    report = dry_run(roadmap)
    out = format_report(report, roadmap)
    assert "ERROR" in out
    assert "## Tier" in out  # shows expected format hint


def test_format_report_singular_item(tmp_path: Path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("## Tier 0\n\n**T0-1 · sole task**\n")
    report = dry_run(roadmap)
    out = format_report(report, roadmap)
    # "1 item" not "1 items"
    assert "found 1 item" in out
    assert "found 1 items" not in out


# ---------------------------------------------------------------------------
# `oxi v3 plan --dry-run` CLI integration
# ---------------------------------------------------------------------------


def test_cli_plan_dry_run_happy_path(tmp_path: Path, capsys):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(
        "## Tier 0\n\n"
        "**T0-1 · first task**\n_subtitle_\n\n"
        "**T0-2 · second task**\n"
    )
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    rc = main(["v3", "plan", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "T0-1" in out
    assert "first task" in out
    assert "T0-2" in out
    assert "DB not touched" in out


def test_cli_plan_dry_run_zero_items_exits_1(tmp_path: Path, capsys):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("# Nothing parseable\n")
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    rc = main(["v3", "plan", "--dry-run"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "found 0 items" in out


def test_cli_plan_dry_run_parse_error_exits_1(tmp_path: Path, capsys):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("**T0-1 · item before tier heading**\n")
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    rc = main(["v3", "plan", "--dry-run"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ERROR" in out


def test_cli_plan_dry_run_missing_roadmap_exits_2(tmp_path: Path, capsys):
    # No roadmap.md written — adapter points at tmp_path but it's absent.
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    with pytest.raises(SystemExit) as exc:
        main(["v3", "plan", "--dry-run"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "roadmap not found" in err


def test_cli_plan_dry_run_no_adapter_exits_2(capsys):
    clear_adapter()
    with pytest.raises(SystemExit) as exc:
        main(["v3", "plan", "--dry-run"])
    assert exc.value.code == 2
    err = capsys.readouterr().err.lower()
    # CI has multiple adapters installed → "multiple adapters";
    # dev with only one → "no adapter". Both are registration
    # errors that exit 2.
    assert "no adapter" in err or "multiple adapters" in err


def test_cli_plan_without_dry_run_flag_fails(tmp_path: Path, capsys):
    """Omitting --dry-run should fail (it is required)."""
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    with pytest.raises(SystemExit):
        main(["v3", "plan"])


def test_cli_plan_dry_run_shows_natural_markdown_hint(tmp_path: Path, capsys):
    """A roadmap written in natural markdown (## T0-1 — title) produces 0 items.

    The hint in the output must include the *correct* format so the operator
    knows what to fix.  This is the core UX goal of BLOCKER-3.
    """
    roadmap = tmp_path / "roadmap.md"
    # Natural (wrong) format an operator would write
    roadmap.write_text(
        "## T0-1 — install runbook\n"
        "Get oxi running in 5 minutes.\n\n"
        "## T0-2 — rollback runbook\n"
        "Document the PyPI yank flow.\n"
    )
    register_adapter(_Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        repo_root_value=str(tmp_path),
    ))
    rc = main(["v3", "plan", "--dry-run"])
    assert rc == 1
    out = capsys.readouterr().out
    # Must show the correct format so the operator can fix it
    assert "## Tier" in out
    assert "**T" in out  # points to **Tident · title** convention
