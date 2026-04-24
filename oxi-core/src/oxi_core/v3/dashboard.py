"""Localhost HTML dashboard — engine state at a glance.

A tiny ``http.server`` running on a user-chosen port. Renders the same
task + event data the CLI surfaces, as HTML. No auth — localhost only.
No JS framework, no build step; just server-rendered HTML.

The dashboard is a query layer, not a control plane. Operators halt
the engine via the killswitch file, not via a button here.
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..adapter import get_active_adapter
from .brief import generate as generate_brief


@dataclass(frozen=True)
class DashboardConfig:
    """How the dashboard binds to the network.

    Defaults are localhost-only. Callers that want to bind to a
    non-loopback interface must do so explicitly.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    window_hours: int = 24


def _render_merges(merges: list[tuple[str, str]]) -> str:
    if not merges:
        return "<li><em>none</em></li>"
    parts = [
        f"<li><code>{html.escape(i)}</code> — {html.escape(t)}</li>"
        for i, t in merges
    ]
    return "".join(parts)


def _render_failures(failures: list[tuple[str, str, str]]) -> str:
    if not failures:
        return "<li><em>none</em></li>"
    parts = []
    for ident, title, reason in failures:
        parts.append(
            f"<li><code>{html.escape(ident)}</code> — "
            f"{html.escape(title)} — <em>{html.escape(reason)}</em></li>"
        )
    return "".join(parts)


def _recovered_task_ids(conn: sqlite3.Connection) -> set[int]:
    """Return task IDs that have at least one ``auto_recover_attempted`` event.

    Used to annotate recovered tasks in the dashboard so operators can
    distinguish a retry from a first-run dispatch at a glance.
    """
    rows = conn.execute(
        "SELECT DISTINCT task_id FROM event WHERE kind = 'auto_recover_attempted'"
    ).fetchall()
    return {row[0] for row in rows}


def render_html(conn: sqlite3.Connection, *, window_hours: int = 24) -> str:
    """Render the dashboard HTML for the current DB state.

    Self-contained: no external CSS/JS, no images. One file, one
    response.

    Tasks that have been through ``auto_recover`` receive a
    ``[retry]`` badge in the id column so operators can immediately
    distinguish recovered dispatches from first-runs.
    """
    adapter = get_active_adapter()
    instance = adapter.naming().instance_name
    plan_tier = adapter.plan_tier()
    repo = adapter.github_repo()

    brief = generate_brief(conn, window_hours=window_hours)
    status_rows = "".join(
        f"<tr><td>{html.escape(s)}</td><td>{n}</td></tr>"
        for s, n in sorted(brief.status_counts.items())
    ) or "<tr><td colspan=2><em>no tasks</em></td></tr>"

    # Collect recovered task IDs for badge rendering.
    recovered_ids = _recovered_task_ids(conn)

    recent_tasks = [
        (row["id"], row["identifier"], row["title"], row["status"],
         row["pr_number"], row["last_progress_at"])
        for row in conn.execute(
            "SELECT id, identifier, title, status, pr_number, last_progress_at "
            "FROM task ORDER BY updated_at DESC LIMIT 50"
        )
    ]
    task_rows_parts = []
    for task_id, ident, title, status, pr, last_progress in recent_tasks:
        retry_badge = (
            ' <span class="retry-badge" title="recovered by auto_recover">[retry]</span>'
            if task_id in recovered_ids
            else ""
        )
        task_rows_parts.append(
            "<tr>"
            f"<td><code>{html.escape(ident)}</code>{retry_badge}</td>"
            f"<td>{html.escape(title)}</td>"
            f"<td>{html.escape(status)}</td>"
            # pr is INTEGER in schema but forks may relax it; escape defensively.
            f"<td>{html.escape(str(pr)) if pr is not None else ''}</td>"
            f"<td>{html.escape(last_progress or '')}</td>"
            "</tr>"
        )
    task_rows = "".join(task_rows_parts) or "<tr><td colspan=5><em>no tasks</em></td></tr>"

    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(instance)} dashboard</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2rem; color: #222; }}
h1 {{ margin-bottom: 0.2rem; }}
.meta {{ color: #666; margin-bottom: 2rem; }}
table {{ border-collapse: collapse; margin-bottom: 2rem; }}
th, td {{ border: 1px solid #ddd; padding: 0.3rem 0.6rem; text-align: left; }}
th {{ background: #f5f5f5; }}
code {{ background: #f0f0f0; padding: 0 0.2rem; border-radius: 2px; }}
.summary {{ display: flex; gap: 2rem; }}
.summary > div {{ flex: 1; }}
.retry-badge {{
  display: inline-block;
  margin-left: 0.3rem;
  padding: 0 0.3rem;
  background: #fff3cd;
  border: 1px solid #ffc107;
  border-radius: 3px;
  font-size: 0.75em;
  color: #856404;
  vertical-align: middle;
}}
</style>
</head>
<body>
<h1>{html.escape(instance)}</h1>
<p class="meta">
  repo: <code>{html.escape(repo)}</code> &middot;
  plan tier: <code>{html.escape(plan_tier)}</code> &middot;
  window: last {window_hours}h &middot;
  rendered: {now}
</p>

<div class="summary">
  <div>
    <h2>Status (last {window_hours}h)</h2>
    <table>
      <thead><tr><th>status</th><th>count</th></tr></thead>
      <tbody>{status_rows}</tbody>
    </table>
    <p>Spend: ${brief.total_cost_usd:.2f}</p>
  </div>

  <div>
    <h2>Recent merges</h2>
    <ul>
      {_render_merges(brief.recent_merges)}
    </ul>
    <h2>Recent failures</h2>
    <ul>
      {_render_failures(brief.recent_failures)}
    </ul>
  </div>
</div>

<h2>Recent tasks (50 most recently updated)</h2>
<table>
  <thead><tr><th>id</th><th>title</th><th>status</th><th>pr</th><th>last progress</th></tr></thead>
  <tbody>{task_rows}</tbody>
</table>

</body>
</html>
"""


def _handler_factory(db_path_fn, window_hours: int):
    class _Handler(BaseHTTPRequestHandler):
        # Quiet the default per-request stderr logging.
        def log_message(self, format, *args):  # noqa: A002, ARG002
            pass

        def do_GET(self) -> None:  # noqa: N802 (HTTPServer API)
            # Only the root path returns the dashboard. Other paths 404
            # so the server doesn't fingerprint as "anything oxi" for
            # arbitrary URL probes, and so forks can add /api or
            # /healthz later without refactoring.
            path = (self.path or "/").split("?", 1)[0].split("#", 1)[0]
            if path not in ("/", ""):
                self.send_error(404)
                return
            conn = sqlite3.connect(str(db_path_fn()))
            conn.row_factory = sqlite3.Row
            try:
                body = render_html(conn, window_hours=window_hours).encode("utf-8")
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


def serve(
    *,
    db_path: str,
    config: DashboardConfig | None = None,
) -> HTTPServer:
    """Start the dashboard on the configured host/port. Blocking.

    Returns the ``HTTPServer`` so callers that want to shut it down
    can call ``server.shutdown()``. For tests, use ``make_server``
    instead (sync single-request).
    """
    cfg = config or DashboardConfig()
    from pathlib import Path

    resolved_path = Path(db_path)

    server = HTTPServer(
        (cfg.host, cfg.port),
        _handler_factory(lambda: resolved_path, cfg.window_hours),
    )
    server.serve_forever()
    return server


def make_server(
    *,
    db_path: str,
    config: DashboardConfig | None = None,
) -> HTTPServer:
    """Construct the server without starting it. Used by tests + CLI."""
    cfg = config or DashboardConfig()
    from pathlib import Path

    resolved_path = Path(db_path)
    return HTTPServer(
        (cfg.host, cfg.port),
        _handler_factory(lambda: resolved_path, cfg.window_hours),
    )


__all__ = [
    "DashboardConfig",
    "make_server",
    "render_html",
    "serve",
]

# _recovered_task_ids is intentionally not in __all__ — it is an
# internal query helper.  Tests that need it import it directly.
