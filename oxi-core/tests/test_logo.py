"""Tests for the ASCII logo glyph used by --version, dashboard, README."""

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
from oxi_core.v3._logo import LOGO, LOGO_LINE_1, LOGO_LINE_2
from oxi_core.v3.dashboard import render_html


@dataclass
class _Adapter:
    db_path_value: str
    instance: str = "oxi-test"

    def naming(self): return NamingConfig(instance_name=self.instance)
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
def conn(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield handle.connection
    handle.connection.close()


# ---------------------------------------------------------------------------
# Glyph constants
# ---------------------------------------------------------------------------


def test_logo_is_two_lines():
    assert LOGO.count("\n") == 1


def test_logo_lines_combine():
    assert LOGO == f"{LOGO_LINE_1}\n{LOGO_LINE_2}"


def test_logo_contains_oxi_wordmark():
    """The bottom line should include the literal 'oxi' wordmark."""
    assert "oxi" in LOGO_LINE_2


def test_logo_lines_under_20_chars():
    """Per the plan: stable at 80-col widths means each line is short."""
    assert len(LOGO_LINE_1) < 20
    assert len(LOGO_LINE_2) < 20


def test_logo_no_trailing_whitespace():
    """Trailing whitespace renders as background colour in some terminals."""
    assert LOGO_LINE_1 == LOGO_LINE_1.rstrip()
    assert LOGO_LINE_2 == LOGO_LINE_2.rstrip()


def test_logo_pure_ascii():
    """No escape codes baked in — colour is the renderer's call."""
    LOGO.encode("ascii")  # raises if any non-ASCII char


# ---------------------------------------------------------------------------
# Dashboard application
# ---------------------------------------------------------------------------


def test_dashboard_renders_logo_in_pre_block(conn):
    html_str = render_html(conn)
    assert 'class="brand-logo"' in html_str
    # Both lines appear (HTML-escaped where necessary; backslashes pass
    # through unchanged).
    assert LOGO_LINE_1 in html_str
    assert LOGO_LINE_2 in html_str


def test_dashboard_logo_uses_monospace_font(conn):
    """The brand-logo class must declare a monospace family so the
    glyph aligns visually with the wordmark beneath."""
    html_str = render_html(conn)
    assert ".brand-logo" in html_str
    # One of the standard monospace fallback names.
    assert "monospace" in html_str
    assert "white-space: pre" in html_str


# ---------------------------------------------------------------------------
# CLI --version application
# ---------------------------------------------------------------------------


def test_version_output_contains_logo_and_version(capsys):
    """``oxi --version`` prints the logo above the version line.

    We assert via SystemExit (argparse's --version action exits 0) and
    capsys to grab the printed banner.
    """
    from oxi_core import __version__
    from oxi_core.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err  # argparse may print to stdout
    assert LOGO_LINE_1 in out
    assert LOGO_LINE_2 in out
    assert __version__ in out
    # The 'oxi {version}' line must be the LAST line so scripts that
    # tail -1 keep working.  Strip trailing newline before splitting.
    last_line = out.rstrip("\n").splitlines()[-1]
    assert last_line == f"oxi {__version__}"
