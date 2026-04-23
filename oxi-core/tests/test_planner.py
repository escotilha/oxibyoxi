"""Tests for oxi_core.planner — roadmap parsing and task seeding."""

from __future__ import annotations

import json
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
    RoadmapItem,
    clear_adapter,
    register_adapter,
)
from oxi_core.planner import (
    RoadmapParseError,
    parse_roadmap,
    plan,
    read_roadmap,
    seed_tasks,
)

# ---------------------------------------------------------------------------
# Adapter fixture
# ---------------------------------------------------------------------------


@dataclass
class _TestAdapter:
    db_path_value: str | None = None
    roadmap_location_value: str = "roadmap.md"

    def naming(self) -> NamingConfig:
        return NamingConfig()

    def paths(self) -> PathsConfig:
        return PathsConfig(db_path=self.db_path_value)

    def budget(self) -> BudgetCaps:
        return BudgetCaps()

    def github_repo(self) -> str:
        return "owner/repo"

    def roadmap_location(self) -> str:
        return self.roadmap_location_value

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/",)

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (
            DispatchHost(
                name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"
            ),
        )

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
# Parsing — happy paths
# ---------------------------------------------------------------------------


def test_parse_single_item_with_subtitle():
    text = (
        "# roadmap\n\n"
        "## Tier 0\n\n"
        "**T0-1 · add a greet function**\n"
        "_returns hello-name_\n"
    )
    items = parse_roadmap(text)
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, RoadmapItem)
    assert item.identifier == "T0-1"
    assert item.tier == 0
    assert item.title == "add a greet function"
    assert item.subtitle == "returns hello-name"


def test_parse_item_without_subtitle():
    text = "## Tier 0\n\n**T0-1 · solo title**\n\n## Tier 1\n"
    items = parse_roadmap(text)
    assert len(items) == 1
    assert items[0].subtitle == ""


def test_parse_multiple_items_across_tiers():
    text = (
        "## Tier 0\n\n"
        "**T0-1 · first**\n_one_\n\n"
        "**T0-2 · second**\n_two_\n\n"
        "## Tier 1\n\n"
        "**T1-1 · third**\n_three_\n"
    )
    items = parse_roadmap(text)
    ids = [i.identifier for i in items]
    tiers = [i.tier for i in items]
    titles = [i.title for i in items]
    assert ids == ["T0-1", "T0-2", "T1-1"]
    assert tiers == [0, 0, 1]
    assert titles == ["first", "second", "third"]


def test_parse_preserves_item_order():
    text = (
        "## Tier 2\n\n"
        "**T2-5 · fifth**\n\n"
        "**T2-1 · first-in-tier**\n\n"
        "**T2-3 · middle**\n"
    )
    items = parse_roadmap(text)
    assert [i.identifier for i in items] == ["T2-5", "T2-1", "T2-3"]


def test_parse_ignores_intro_paragraphs_and_other_headings():
    text = (
        "# roadmap\n\n"
        "some introductory text\n\n"
        "### Notes\n"
        "more text\n\n"
        "## Tier 0\n\n"
        "**T0-1 · only real item**\n"
    )
    items = parse_roadmap(text)
    assert len(items) == 1
    assert items[0].identifier == "T0-1"


def test_parse_handles_whitespace_around_separator():
    # The literal contains non-breaking space around ` · `; must still match.
    text = "## Tier 0\n\n**T0-1   ·   spaced title**\n"
    items = parse_roadmap(text)
    assert items[0].title == "spaced title"


def test_parse_empty_roadmap_returns_empty_list():
    assert parse_roadmap("") == []
    assert parse_roadmap("# just a heading\n\nand some text\n") == []


# ---------------------------------------------------------------------------
# Parsing — error paths
# ---------------------------------------------------------------------------


def test_parse_rejects_item_before_tier_heading():
    text = "**T0-1 · before tier heading**\n"
    with pytest.raises(RoadmapParseError, match="before any .* heading"):
        parse_roadmap(text)


def test_parse_rejects_duplicate_identifiers():
    text = (
        "## Tier 0\n\n"
        "**T0-1 · first**\n\n"
        "## Tier 1\n\n"
        "**T0-1 · duplicate in different tier**\n"
    )
    with pytest.raises(RoadmapParseError, match="duplicate identifier"):
        parse_roadmap(text)


def test_parse_italic_line_without_pending_item_is_ignored():
    # A stray italic line between sections must not crash.
    text = "## Tier 0\n\n_just italic, no item above_\n\n**T0-1 · real**\n"
    items = parse_roadmap(text)
    assert len(items) == 1
    assert items[0].subtitle == ""


# ---------------------------------------------------------------------------
# Reading from disk via adapter
# ---------------------------------------------------------------------------


def test_read_roadmap_uses_adapter_location(tmp_path: Path):
    roadmap = tmp_path / "custom-roadmap.md"
    roadmap.write_text("## Tier 0\n\n**T0-1 · from custom**\n")
    register_adapter(_TestAdapter(roadmap_location_value="custom-roadmap.md"))

    items = read_roadmap(tmp_path)
    assert len(items) == 1
    assert items[0].identifier == "T0-1"


def test_read_roadmap_missing_file_raises(tmp_path: Path):
    register_adapter(_TestAdapter(roadmap_location_value="does-not-exist.md"))
    with pytest.raises(FileNotFoundError):
        read_roadmap(tmp_path)


# ---------------------------------------------------------------------------
# Seeding into the DB
# ---------------------------------------------------------------------------


def _db_handle(tmp_path: Path):
    register_adapter(_TestAdapter(db_path_value=str(tmp_path / "oxi.db")))
    return db.connect()


def test_seed_inserts_new_items(tmp_path: Path):
    handle = _db_handle(tmp_path)
    items = [
        RoadmapItem(identifier="T0-1", tier=0, title="first", subtitle="a"),
        RoadmapItem(identifier="T1-1", tier=1, title="second"),
    ]
    try:
        inserted = seed_tasks(handle.connection, items)
        assert inserted == 2
        rows = list(
            handle.connection.execute(
                "SELECT identifier, tier, title, subtitle, files_touched FROM task "
                "ORDER BY identifier"
            )
        )
        assert [r["identifier"] for r in rows] == ["T0-1", "T1-1"]
        assert rows[0]["title"] == "first"
        assert rows[0]["subtitle"] == "a"
        assert json.loads(rows[0]["files_touched"]) == []
    finally:
        handle.connection.close()


def test_seed_is_idempotent_on_identifier_collisions(tmp_path: Path):
    handle = _db_handle(tmp_path)
    items = [RoadmapItem(identifier="T0-1", tier=0, title="v1")]
    try:
        first = seed_tasks(handle.connection, items)
        # Re-running with the same (and extra) items inserts only the new ones.
        more = [
            RoadmapItem(identifier="T0-1", tier=0, title="v1-updated"),
            RoadmapItem(identifier="T0-2", tier=0, title="new"),
        ]
        second = seed_tasks(handle.connection, more)
        assert first == 1
        assert second == 1  # T0-1 skipped, T0-2 inserted
        count = handle.connection.execute(
            "SELECT COUNT(*) FROM task"
        ).fetchone()[0]
        assert count == 2
        # Original title preserved — seeding does not overwrite.
        row = handle.connection.execute(
            "SELECT title FROM task WHERE identifier = 'T0-1'"
        ).fetchone()
        assert row["title"] == "v1"
    finally:
        handle.connection.close()


def test_seed_persists_files_touched_as_json(tmp_path: Path):
    handle = _db_handle(tmp_path)
    items = [
        RoadmapItem(
            identifier="T0-1",
            tier=0,
            title="touches files",
            files_touched=("src/a.py", "src/b.py"),
        )
    ]
    try:
        seed_tasks(handle.connection, items)
        row = handle.connection.execute(
            "SELECT files_touched FROM task WHERE identifier = 'T0-1'"
        ).fetchone()
        assert json.loads(row["files_touched"]) == ["src/a.py", "src/b.py"]
    finally:
        handle.connection.close()


# ---------------------------------------------------------------------------
# Top-level plan() wrapper
# ---------------------------------------------------------------------------


def test_plan_reads_and_seeds_end_to_end(tmp_path: Path):
    (tmp_path / "roadmap.md").write_text(
        "## Tier 0\n\n**T0-1 · first**\n_one_\n\n**T0-2 · second**\n"
    )
    register_adapter(_TestAdapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        n = plan(tmp_path, handle.connection)
        assert n == 2
        count = handle.connection.execute(
            "SELECT COUNT(*) FROM task"
        ).fetchone()[0]
        assert count == 2
    finally:
        handle.connection.close()


def test_plan_against_sample_project_roadmap(tmp_path: Path):
    """Drive the planner against the real sample-project roadmap format."""
    sample_root = Path(__file__).resolve().parents[2] / "sample-project"
    if not (sample_root / "roadmap.md").is_file():
        pytest.skip("sample-project roadmap not checked out")

    register_adapter(_TestAdapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        n = plan(sample_root, handle.connection)
        assert n >= 1
        ids = [
            r[0]
            for r in handle.connection.execute(
                "SELECT identifier FROM task ORDER BY identifier"
            )
        ]
        # Sample-project roadmap lists T0-1, T0-2, T1-1 per P1-1.
        assert "T0-1" in ids
    finally:
        handle.connection.close()
