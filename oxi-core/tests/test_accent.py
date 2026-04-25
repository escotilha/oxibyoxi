"""Tests for the brand accent constant and its dashboard application.

The accent ships as a single hex string in ``oxi_core.v3._color``.
Surfaces that use it (dashboard, future CLI banners) reference the
constant rather than hardcoding the hex; these tests pin the value
and confirm the dashboard renders it.
"""

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
from oxi_core.v3._color import ACCENT_HEX, ACCENT_HEX_DARK
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
# Constants
# ---------------------------------------------------------------------------


def test_accent_hex_is_a_well_formed_hex_color():
    assert ACCENT_HEX.startswith("#")
    assert len(ACCENT_HEX) == 7
    int(ACCENT_HEX[1:], 16)  # raises if any non-hex chars


def test_accent_hex_dark_is_a_well_formed_hex_color():
    assert ACCENT_HEX_DARK.startswith("#")
    assert len(ACCENT_HEX_DARK) == 7
    int(ACCENT_HEX_DARK[1:], 16)


def test_accent_pair_is_distinct():
    """Light and dark variants must differ."""
    assert ACCENT_HEX != ACCENT_HEX_DARK


# ---------------------------------------------------------------------------
# Dashboard application
# ---------------------------------------------------------------------------


def test_dashboard_renders_accent_on_h1(conn):
    html_str = render_html(conn)
    assert ACCENT_HEX in html_str
    # The accent should appear in the h1 underline rule.
    assert "border-bottom: 3px solid" in html_str


def test_dashboard_renders_accent_on_event_summary_link(conn):
    """ev-details summary text uses the accent color (replaces #0057b8)."""
    html_str = render_html(conn)
    assert ACCENT_HEX in html_str
    assert "#0057b8" not in html_str  # old blue should be gone
    assert "#003d82" not in html_str


def test_dashboard_does_not_change_semantic_colors(conn):
    """Health banner red, retry-badge amber, table greys are unchanged.

    The accent applies to brand surfaces only — semantic colours that
    encode meaning (warn/error/state) must not move.
    """
    html_str = render_html(conn)
    # Retry-badge amber is unaffected.
    assert "#fff3cd" in html_str
    assert "#ffc107" in html_str
    # Body text colour, meta grey, table greys.
    assert "#222" in html_str
    assert "#666" in html_str
    assert "#ddd" in html_str
