"""Tests for oxi_core.v3.dashboard — HTML rendering + HTTP server."""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

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
from oxi_core.v3.dashboard import DashboardConfig, make_server, render_html


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


def _seed(conn, identifier: str, status: str, title: str = "t",
          pr_number: int | None = None) -> None:
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES (?, 0, ?, ?, ?)",
        (identifier, title, status, pr_number),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------


def test_render_contains_instance_name(conn):
    html = render_html(conn)
    assert "oxi-test" in html


def test_render_contains_repo_slug(conn):
    html = render_html(conn)
    assert "owner/repo" in html


def test_render_contains_plan_tier(conn):
    html = render_html(conn)
    assert "standard" in html


def test_render_shows_recent_tasks(conn):
    _seed(conn, "T0-1", "merged", title="example")
    html = render_html(conn)
    assert "T0-1" in html
    assert "example" in html
    assert "merged" in html


def test_render_empty_db_has_no_crashes(conn):
    html = render_html(conn)
    assert "<table>" in html
    assert "no tasks" in html.lower()


def test_render_escapes_title_content(conn):
    _seed(conn, "T0-1", "dispatched", title="<script>alert(1)</script>")
    html = render_html(conn)
    # The raw script tag must not appear unescaped.
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_includes_pr_number_when_set(conn):
    _seed(conn, "T0-1", "dispatched", pr_number=42)
    html = render_html(conn)
    # Table row contains the PR number.
    assert "42" in html


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def test_server_serves_html(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        handle.connection.execute(
            "INSERT INTO task (identifier, tier, title, status) "
            "VALUES ('T0-1', 0, 'demo', 'dispatched')"
        )
        handle.connection.commit()
    finally:
        handle.connection.close()

    # Use port 0 to let OS pick a free port.
    config = DashboardConfig(host="127.0.0.1", port=0)
    server = make_server(db_path=str(tmp_path / "oxi.db"), config=config)
    try:
        actual_port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        response = urllib.request.urlopen(
            f"http://127.0.0.1:{actual_port}/", timeout=5
        )
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/html")
        assert "T0-1" in body
        assert "demo" in body
    finally:
        server.shutdown()
        server.server_close()


def test_server_reflects_db_updates_between_requests(tmp_path: Path):
    """Each request reopens the DB — mid-serve updates are visible."""
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        handle.connection.execute(
            "INSERT INTO task (identifier, tier, title, status) "
            "VALUES ('T0-1', 0, 'first', 'dispatched')"
        )
        handle.connection.commit()
    finally:
        handle.connection.close()

    config = DashboardConfig(host="127.0.0.1", port=0)
    server = make_server(db_path=str(tmp_path / "oxi.db"), config=config)
    try:
        actual_port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        body1 = urllib.request.urlopen(
            f"http://127.0.0.1:{actual_port}/", timeout=5
        ).read().decode("utf-8")
        assert "first" in body1

        # Mutate DB between requests.
        handle2 = db.connect()
        try:
            handle2.connection.execute(
                "INSERT INTO task (identifier, tier, title, status) "
                "VALUES ('T0-2', 0, 'second', 'dispatched')"
            )
            handle2.connection.commit()
        finally:
            handle2.connection.close()

        body2 = urllib.request.urlopen(
            f"http://127.0.0.1:{actual_port}/", timeout=5
        ).read().decode("utf-8")
        assert "second" in body2
    finally:
        server.shutdown()
        server.server_close()
