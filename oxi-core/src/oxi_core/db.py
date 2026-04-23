"""SQLite schema + connection management for oxi.

The engine keeps its state in a single SQLite file per install. Path is
resolved through the active adapter's `paths().db_path`, falling back
to `.oxi/oxi.db` under the repo root when the adapter omits it.

Anti-pattern #2: the default DB path this module hands to the CLI is
the same path any shipped systemd unit will reference. Unit tests
enforce that invariant (see tests/test_db_paths.py).

Schema versioning is linear, not branching. A single `schema_migrations`
table records applied version IDs; each migration is a SQL script in
`migrations/NNN_description.sql`. Migrations run in numeric order and
never run twice. There is no down-migration story — oxi rolls forward.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .adapter import get_active_adapter

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _adapter_paths() -> tuple[str | None, str]:
    """Return (adapter_db_path, oxi_dir) from the active adapter.

    Extracted as a helper so tests can stub it without touching
    registration internals.
    """
    adapter = get_active_adapter()
    paths = adapter.paths()
    return paths.db_path, paths.oxi_dir


def default_db_path(repo_root: Path | None = None) -> Path:
    """Compute the canonical DB path for the current install.

    Adapter's `paths().db_path` wins if set. Otherwise, falls back to
    `<repo_root>/<oxi_dir>/oxi.db` — with `repo_root` resolving to the
    current working directory if not supplied.
    """
    adapter_db_path, oxi_dir = _adapter_paths()
    if adapter_db_path is not None:
        return Path(adapter_db_path)
    root = repo_root if repo_root is not None else Path.cwd()
    return root / oxi_dir / "oxi.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT NOT NULL UNIQUE,
    tier INTEGER NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL DEFAULT '',
    target_repo TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    files_touched TEXT NOT NULL DEFAULT '[]',
    last_progress_at TEXT,
    dispatched_at TEXT,
    merged_at TEXT,
    failed_at TEXT,
    failure_reason TEXT,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_status ON task(status);
CREATE INDEX IF NOT EXISTS idx_task_tier ON task(tier);
CREATE INDEX IF NOT EXISTS idx_task_last_progress ON task(last_progress_at);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES task(id)
);

CREATE INDEX IF NOT EXISTS idx_event_task ON event(task_id);
CREATE INDEX IF NOT EXISTS idx_event_kind ON event(kind);
CREATE INDEX IF NOT EXISTS idx_event_created ON event(created_at);

CREATE TABLE IF NOT EXISTS front (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    weight REAL NOT NULL DEFAULT 1.0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# Version 2: pr_number column for pr_watcher (P1-6) and for heartbeat's
# orphan-with-PR guard. Nullable by design — not every task has a PR.
MIGRATION_V2_PR_NUMBER: str = """
ALTER TABLE task ADD COLUMN pr_number INTEGER;
CREATE INDEX IF NOT EXISTS idx_task_pr_number ON task(pr_number);
"""


# Migration list — append-only. Each (version, sql) pair runs in order if
# not already recorded in schema_migrations. Do NOT edit applied migrations;
# add a new one instead.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, SCHEMA_SQL),
    (2, MIGRATION_V2_PR_NUMBER),
)


# ---------------------------------------------------------------------------
# Connection + migration runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DbHandle:
    """Wraps a sqlite3.Connection with the resolved path.

    Frozen so it can be passed between modules without risk of mutation.
    The connection itself is mutable state held inside.
    """

    path: Path
    connection: sqlite3.Connection


def connect(path: Path | None = None) -> DbHandle:
    """Open a connection to the DB at `path`, creating the file + schema if absent.

    If `path` is None, resolves via `default_db_path()`. Parent directory
    is created if missing. Foreign keys are enabled on every connection.
    """
    resolved = path if path is not None else default_db_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_migrations(conn)
    return DbHandle(path=resolved, connection=conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Run every migration not yet recorded in schema_migrations, in order."""
    # Bootstrap: schema_migrations must exist before we can query it.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {
        row[0] for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 if none."""
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "DbHandle",
    "MIGRATIONS",
    "SCHEMA_SQL",
    "connect",
    "current_schema_version",
    "default_db_path",
]
