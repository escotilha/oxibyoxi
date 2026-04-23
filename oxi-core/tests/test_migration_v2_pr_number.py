"""Tests for schema migration v2 — add pr_number column to task."""

from __future__ import annotations

import sqlite3
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
from oxi_core.v3.dispatch import Task


@dataclass
class _Adapter:
    db_path_value: str

    def naming(self): return NamingConfig()
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
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


def test_pr_number_column_exists_after_migration(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        cols = {
            row[1]
            for row in handle.connection.execute("PRAGMA table_info(task)")
        }
        assert "pr_number" in cols
    finally:
        handle.connection.close()


def test_migration_v2_is_recorded(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        version = db.current_schema_version(handle.connection)
        assert version == 2
    finally:
        handle.connection.close()


def test_pr_number_defaults_to_null(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        handle.connection.execute(
            "INSERT INTO task (identifier, tier, title) VALUES ('T0-1', 0, 't')"
        )
        handle.connection.commit()
        row = handle.connection.execute(
            "SELECT pr_number FROM task WHERE identifier = 'T0-1'"
        ).fetchone()
        assert row["pr_number"] is None
    finally:
        handle.connection.close()


def test_pr_number_accepts_integer_values(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        handle.connection.execute(
            "INSERT INTO task (identifier, tier, title, pr_number) VALUES ('T0-1', 0, 't', 42)"
        )
        handle.connection.commit()
        row = handle.connection.execute(
            "SELECT pr_number FROM task WHERE identifier = 'T0-1'"
        ).fetchone()
        assert row["pr_number"] == 42
    finally:
        handle.connection.close()


def test_task_from_row_reads_pr_number(tmp_path: Path):
    """The _safe_pr_number helper now reads a real column."""
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        handle.connection.execute(
            "INSERT INTO task (identifier, tier, title, pr_number) VALUES ('T0-1', 0, 't', 99)"
        )
        handle.connection.commit()
        row = handle.connection.execute(
            "SELECT * FROM task WHERE identifier = 'T0-1'"
        ).fetchone()
        task = Task.from_row(row)
        assert task.pr_number == 99
    finally:
        handle.connection.close()


def test_migration_is_idempotent_on_reconnect(tmp_path: Path):
    """Reconnecting does not re-apply v2."""
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle1 = db.connect()
    handle1.connection.close()
    handle2 = db.connect()
    try:
        count = handle2.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 2"
        ).fetchone()[0]
        assert count == 1
    finally:
        handle2.connection.close()


def test_pr_number_index_exists(tmp_path: Path):
    """Index on pr_number accelerates pr_watcher lookups."""
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        indexes = {
            row[1]
            for row in handle.connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_task_pr_number" in indexes
    finally:
        handle.connection.close()


def test_v1_only_db_can_be_upgraded_to_v2(tmp_path: Path):
    """Create a v1 DB manually, reconnect, verify v2 applies cleanly.

    Simulates an existing install that predates this migration.
    """
    db_path = tmp_path / "legacy.db"
    # Create v1 schema by hand, record it as version 1.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(db.SCHEMA_SQL)
    conn.execute("INSERT INTO schema_migrations (version) VALUES (1)")
    conn.commit()
    # v1 state: no pr_number column.
    cols_v1 = {row[1] for row in conn.execute("PRAGMA table_info(task)")}
    assert "pr_number" not in cols_v1
    conn.close()

    # Reconnect via db.connect — should apply v2.
    register_adapter(_Adapter(db_path_value=str(db_path)))
    handle = db.connect()
    try:
        cols_v2 = {
            row[1]
            for row in handle.connection.execute("PRAGMA table_info(task)")
        }
        assert "pr_number" in cols_v2
        assert db.current_schema_version(handle.connection) == 2
    finally:
        handle.connection.close()
