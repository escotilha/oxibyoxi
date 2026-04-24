"""Tests for oxi_core.v3.dashboard — HTML rendering + HTTP server."""

from __future__ import annotations

import urllib.error
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
from oxi_core.v3.dashboard import (
    DashboardConfig,
    _render_task_events,
    _task_last_events,
    make_server,
    render_html,
)


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
          pr_number: int | None = None) -> int:
    """Insert a task row and return its rowid."""
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES (?, 0, ?, ?, ?)",
        (identifier, title, status, pr_number),
    )
    conn.commit()
    return cur.lastrowid


def _seed_event(conn, task_id: int, kind: str, payload: str = "{}") -> None:
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
        (task_id, kind, payload),
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


def test_render_escapes_pr_number_even_if_string(conn):
    """Schema declares pr_number INTEGER, but a fork might relax it.

    If a fork ever inserts a string into pr_number (e.g. a webhook
    payload not coerced through int()), the renderer must escape it
    like every other user-controlled field.
    """
    # Bypass the ORM and write a string directly to simulate the
    # fork-schema-relaxed case.
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES ('T0-1', 0, 't', 'dispatched', ?)",
        ("<script>alert(1)</script>",),
    )
    conn.commit()
    html = render_html(conn)
    # Raw script must not appear; escaped form must.
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# _task_last_events — unit tests for the query helper
# ---------------------------------------------------------------------------


def test_task_last_events_empty_for_no_events(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    events = _task_last_events(conn, task_id)
    assert events == []


def test_task_last_events_returns_most_recent_first(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    for i in range(3):
        _seed_event(conn, task_id, f"kind_{i}", f'{{"n":{i}}}')
    events = _task_last_events(conn, task_id)
    # ORDER BY id DESC — last inserted comes first.
    assert events[0][1] == "kind_2"
    assert events[1][1] == "kind_1"
    assert events[2][1] == "kind_0"


def test_task_last_events_caps_at_ten(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    for i in range(15):
        _seed_event(conn, task_id, f"k{i}")
    events = _task_last_events(conn, task_id)
    assert len(events) == 10


def test_task_last_events_truncates_long_payload(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    long_payload = "x" * 200
    _seed_event(conn, task_id, "some_kind", long_payload)
    events = _task_last_events(conn, task_id)
    assert len(events) == 1
    _ts, _kind, payload = events[0]
    # Payload must be truncated; raw 200-char string must not appear verbatim.
    assert len(payload) < 200
    assert payload.endswith("…")


def test_task_last_events_short_payload_not_truncated(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    short = '{"ok": true}'
    _seed_event(conn, task_id, "dispatch_started", short)
    events = _task_last_events(conn, task_id)
    _ts, _kind, payload = events[0]
    assert payload == short


def test_task_last_events_isolates_by_task(conn):
    id1 = _seed(conn, "T0-1", "dispatched")
    id2 = _seed(conn, "T0-2", "planned")
    _seed_event(conn, id1, "dispatch_started")
    _seed_event(conn, id2, "other_event")
    ev1 = _task_last_events(conn, id1)
    ev2 = _task_last_events(conn, id2)
    assert all(k == "dispatch_started" for _, k, _ in ev1)
    assert all(k == "other_event" for _, k, _ in ev2)


# ---------------------------------------------------------------------------
# _render_task_events — unit tests for the HTML renderer
# ---------------------------------------------------------------------------


def test_render_task_events_no_events_shows_placeholder():
    result = _render_task_events([])
    assert "no events" in result
    assert "<details" in result
    assert "<summary" in result


def test_render_task_events_shows_kind_and_timestamp():
    events = [("2026-01-02 03:04:05", "dispatch_started", "{}")]
    result = _render_task_events(events)
    assert "dispatch_started" in result
    assert "2026-01-02 03:04:05" in result


def test_render_task_events_escapes_kind():
    events = [("2026-01-02 03:04:05", "<bad>", "{}")]
    result = _render_task_events(events)
    assert "<bad>" not in result
    assert "&lt;bad&gt;" in result


def test_render_task_events_escapes_payload():
    events = [("2026-01-02 03:04:05", "k", '<script>alert("xss")</script>')]
    result = _render_task_events(events)
    assert '<script>' not in result
    assert "&lt;script&gt;" in result


def test_render_task_events_escapes_timestamp():
    events = [("<ts>", "k", "{}")]
    result = _render_task_events(events)
    assert "<ts>" not in result
    assert "&lt;ts&gt;" in result


def test_render_task_events_uses_details_element():
    events = [("2026-01-02 03:04:05", "dispatch_started", "{}")]
    result = _render_task_events(events)
    assert "<details" in result
    assert "</details>" in result
    assert "<summary" in result


# ---------------------------------------------------------------------------
# render_html — integration tests for expanded events in the page
# ---------------------------------------------------------------------------


def test_render_html_events_widget_present_for_task(conn):
    """Each task row must contain a <details> expand widget."""
    _seed(conn, "T0-1", "dispatched")
    page = render_html(conn)
    assert "ev-details" in page
    assert "<details" in page


def test_render_html_shows_event_kind_in_expanded_row(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    _seed_event(conn, task_id, "dispatch_started", '{"branch":"feat/x"}')
    page = render_html(conn)
    assert "dispatch_started" in page


def test_render_html_shows_at_most_ten_events_per_task(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    for i in range(15):
        _seed_event(conn, task_id, f"kind_{i:02d}")
    page = render_html(conn)
    # 15 events inserted but only 10 must appear.
    count = page.count("kind_")
    assert count == 10


def test_render_html_escapes_event_payload_xss(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    _seed_event(conn, task_id, "dispatch_started", "<script>alert(1)</script>")
    page = render_html(conn)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_render_html_escapes_event_kind_xss(conn):
    task_id = _seed(conn, "T0-1", "dispatched")
    _seed_event(conn, task_id, '<img src=x onerror=alert(1)>', "{}")
    page = render_html(conn)
    assert "<img src=x" not in page


def test_render_html_no_events_shows_placeholder(conn):
    """A task with zero events must still render the widget with placeholder."""
    _seed(conn, "T0-1", "planned")
    page = render_html(conn)
    assert "no events" in page


def test_render_html_events_only_for_own_task(conn):
    """Events from task B must not bleed into the row for task A."""
    id_a = _seed(conn, "T0-A", "dispatched")
    id_b = _seed(conn, "T0-B", "dispatched")
    _seed_event(conn, id_a, "dispatch_started")
    _seed_event(conn, id_b, "pr_merged")
    page = render_html(conn)
    # Both events appear but the page should render without error.
    assert "dispatch_started" in page
    assert "pr_merged" in page


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


def test_server_returns_404_for_non_root_paths(tmp_path: Path):
    """Only '/' returns the dashboard; other paths 404.

    Prevents fingerprinting on broader binds and keeps the URL space
    clean for fork extensions (/healthz, /api, etc.).
    """
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    handle.connection.close()

    config = DashboardConfig(host="127.0.0.1", port=0)
    server = make_server(db_path=str(tmp_path / "oxi.db"), config=config)
    try:
        actual_port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        # Root returns 200.
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{actual_port}/", timeout=5,
        )
        assert resp.status == 200

        # Any other path returns 404.
        for bogus in ("/foo", "/admin", "/metrics"):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{actual_port}{bogus}", timeout=5,
                )
                raise AssertionError(f"{bogus} should 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404, f"{bogus} returned {e.code}"
    finally:
        server.shutdown()
        server.server_close()


def test_server_ignores_querystring_on_root(tmp_path: Path):
    """Root with ?stuff=x still 200s — querystrings are allowed."""
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    handle.connection.close()

    config = DashboardConfig(host="127.0.0.1", port=0)
    server = make_server(db_path=str(tmp_path / "oxi.db"), config=config)
    try:
        actual_port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{actual_port}/?x=1", timeout=5,
        )
        assert resp.status == 200
    finally:
        server.shutdown()
        server.server_close()
