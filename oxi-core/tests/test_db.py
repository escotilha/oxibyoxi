"""Tests for oxi_core.db — connection, schema, migrations, path resolution."""

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

# ---------------------------------------------------------------------------
# Adapter fixtures — local, to avoid depending on the reference adapter.
# ---------------------------------------------------------------------------


@dataclass
class _TestAdapter:
    """Adapter whose paths.db_path is configurable per test."""

    db_path_value: str | None = None
    oxi_dir_value: str = ".oxi"

    def naming(self) -> NamingConfig:
        return NamingConfig()

    def paths(self) -> PathsConfig:
        return PathsConfig(db_path=self.db_path_value, oxi_dir=self.oxi_dir_value)

    def budget(self) -> BudgetCaps:
        return BudgetCaps()

    def github_repo(self) -> str:
        return "owner/repo"

    def roadmap_location(self) -> str:
        return "roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/", "fix/")

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (DispatchHost(name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"),)

    def promote_recipe(self) -> None:
        return None

    def plan_tier(self) -> str:
        return "standard"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_default_db_path_uses_adapter_override(tmp_path: Path) -> None:
    override = tmp_path / "custom.db"
    register_adapter(_TestAdapter(db_path_value=str(override)))
    assert db.default_db_path() == override


def test_default_db_path_falls_back_to_repo_root(tmp_path: Path) -> None:
    register_adapter(_TestAdapter(db_path_value=None, oxi_dir_value=".oxi"))
    resolved = db.default_db_path(repo_root=tmp_path)
    assert resolved == tmp_path / ".oxi" / "oxi.db"


def test_default_db_path_uses_cwd_when_no_repo_root(tmp_path: Path, monkeypatch) -> None:
    register_adapter(_TestAdapter(db_path_value=None))
    monkeypatch.chdir(tmp_path)
    resolved = db.default_db_path()
    assert resolved == tmp_path / ".oxi" / "oxi.db"


# ---------------------------------------------------------------------------
# Connection + migration
# ---------------------------------------------------------------------------


def test_connect_creates_file_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "oxi.db"
    register_adapter(_TestAdapter(db_path_value=str(path)))
    handle = db.connect()
    try:
        assert path.exists()
        # Required tables present.
        tables = {
            row[0]
            for row in handle.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"schema_migrations", "task", "event", "front"}.issubset(tables)
    finally:
        handle.connection.close()


def test_connect_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "oxi.db"
    register_adapter(_TestAdapter(db_path_value=str(path)))

    h1 = db.connect()
    h1.connection.close()
    h2 = db.connect()
    try:
        # Only one migration record after two connects.
        row = h2.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
        ).fetchone()
        assert row[0] == 1
    finally:
        h2.connection.close()


def test_current_schema_version_reflects_applied_migrations(tmp_path: Path) -> None:
    """Freshly created DB reflects the highest migration in ``MIGRATIONS``."""
    path = tmp_path / "oxi.db"
    register_adapter(_TestAdapter(db_path_value=str(path)))
    handle = db.connect()
    try:
        assert db.current_schema_version(handle.connection) == max(
            v for v, _ in db.MIGRATIONS
        )
    finally:
        handle.connection.close()


def test_connect_with_explicit_path_bypasses_adapter(tmp_path: Path) -> None:
    # Register adapter pointing somewhere else; explicit arg should win.
    register_adapter(_TestAdapter(db_path_value=str(tmp_path / "adapter.db")))
    explicit = tmp_path / "explicit.db"
    handle = db.connect(path=explicit)
    try:
        assert handle.path == explicit
        assert explicit.exists()
        assert not (tmp_path / "adapter.db").exists()
    finally:
        handle.connection.close()


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    path = tmp_path / "oxi.db"
    register_adapter(_TestAdapter(db_path_value=str(path)))
    handle = db.connect()
    try:
        row = handle.connection.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1
    finally:
        handle.connection.close()


# ---------------------------------------------------------------------------
# Schema-level invariants
# ---------------------------------------------------------------------------


def test_task_identifier_is_unique(tmp_path: Path) -> None:
    path = tmp_path / "oxi.db"
    register_adapter(_TestAdapter(db_path_value=str(path)))
    handle = db.connect()
    try:
        conn = handle.connection
        conn.execute(
            "INSERT INTO task (identifier, tier, title) VALUES (?, ?, ?)",
            ("T0-1", 0, "first"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task (identifier, tier, title) VALUES (?, ?, ?)",
                ("T0-1", 0, "second"),
            )
            conn.commit()
    finally:
        handle.connection.close()


def test_task_default_status_is_planned(tmp_path: Path) -> None:
    path = tmp_path / "oxi.db"
    register_adapter(_TestAdapter(db_path_value=str(path)))
    handle = db.connect()
    try:
        conn = handle.connection
        conn.execute(
            "INSERT INTO task (identifier, tier, title) VALUES (?, ?, ?)",
            ("T0-1", 0, "first"),
        )
        conn.commit()
        row = conn.execute("SELECT status, last_progress_at FROM task").fetchone()
        assert row["status"] == "planned"
        assert row["last_progress_at"] is None
    finally:
        handle.connection.close()
