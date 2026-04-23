"""Roadmap parser + task seeder.

The planner is stateless: it reads a markdown roadmap, turns its
entries into `RoadmapItem` records, and writes each new item as a
`task` row. Items that already exist (by `identifier`) are skipped —
re-running the planner is a no-op.

Roadmap format (markdown):

    ## Tier 0

    **T0-1 · add a greet function**
    _a pure function that returns "hello, {name}"_

    **T0-2 · add a changelog stub**
    _optional subtitle lines like this_

    ## Tier 1

    **T1-1 · another thing**
    _its subtitle_

Rules:

- A `## Tier N` heading sets the tier for every item that follows,
  until the next `## Tier M` heading.
- An item is a bold line shaped `**{IDENTIFIER} · {TITLE}**`. The
  identifier is the opaque key (`T0-1`, `T2-17`, etc.). The title is
  whatever text follows the ` · ` separator.
- The italic line immediately after an item is its subtitle. Optional.
- Everything else (intro paragraphs, blank lines, other headings) is
  ignored.

No project-specific assumptions. The `Tier N` heading pattern is
conventional but the actual tier integer lives in the markdown; the
parser doesn't validate the tier value beyond "it must be a
non-negative integer."
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .adapter import RoadmapItem, get_active_adapter

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_TIER_HEADING = re.compile(r"^##\s+Tier\s+(\d+)\b", re.IGNORECASE)
_ITEM_LINE = re.compile(r"^\*\*([A-Za-z0-9_\-]+)\s*·\s*(.+?)\*\*\s*$")
_SUBTITLE_LINE = re.compile(r"^_(.+)_\s*$")


class RoadmapParseError(ValueError):
    """Raised when the roadmap has structural problems the planner can't resolve."""


def parse_roadmap(text: str) -> list[RoadmapItem]:
    """Turn a roadmap markdown string into a list of `RoadmapItem` records.

    Items appear in the order they appear in the markdown. Subtitle is
    picked up from the line immediately following the item line if and
    only if that line is italic (`_…_`); otherwise the subtitle is
    empty.
    """
    items: list[RoadmapItem] = []
    current_tier: int | None = None
    pending_item: RoadmapItem | None = None

    def _flush_pending() -> None:
        nonlocal pending_item
        if pending_item is not None:
            items.append(pending_item)
            pending_item = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        tier_match = _TIER_HEADING.match(line)
        if tier_match:
            _flush_pending()
            current_tier = int(tier_match.group(1))
            continue

        item_match = _ITEM_LINE.match(line)
        if item_match:
            _flush_pending()
            if current_tier is None:
                raise RoadmapParseError(
                    f"item {item_match.group(1)!r} appears before any '## Tier N' heading"
                )
            identifier = item_match.group(1).strip()
            title = item_match.group(2).strip()
            pending_item = RoadmapItem(
                identifier=identifier, tier=current_tier, title=title
            )
            continue

        subtitle_match = _SUBTITLE_LINE.match(line)
        if subtitle_match and pending_item is not None:
            pending_item = RoadmapItem(
                identifier=pending_item.identifier,
                tier=pending_item.tier,
                title=pending_item.title,
                subtitle=subtitle_match.group(1).strip(),
                target_repo=pending_item.target_repo,
                files_touched=pending_item.files_touched,
            )
            _flush_pending()
            continue

        # Blank line or unrelated content — if we have a pending item
        # without a subtitle, commit it as-is.
        if not line:
            _flush_pending()

    _flush_pending()

    _check_no_duplicate_identifiers(items)
    return items


def _check_no_duplicate_identifiers(items: list[RoadmapItem]) -> None:
    seen: set[str] = set()
    for item in items:
        if item.identifier in seen:
            raise RoadmapParseError(
                f"duplicate identifier {item.identifier!r} in roadmap"
            )
        seen.add(item.identifier)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def read_roadmap(repo_root: Path) -> list[RoadmapItem]:
    """Read the roadmap from the location the active adapter advertises.

    `repo_root` is the directory the adapter's `roadmap_location()` is
    relative to — typically the target repo root.
    """
    adapter = get_active_adapter()
    relative = adapter.roadmap_location()
    path = repo_root / relative
    if not path.is_file():
        raise FileNotFoundError(f"roadmap not found at {path}")
    return parse_roadmap(path.read_text(encoding="utf-8"))


def seed_tasks(conn: sqlite3.Connection, items: list[RoadmapItem]) -> int:
    """Insert each item as a `task` row, skipping identifiers already present.

    Returns the number of rows actually inserted (items that collided
    with existing identifiers do not count).

    This runs in one transaction: either all new items land or none do.
    """
    existing = {
        row[0]
        for row in conn.execute("SELECT identifier FROM task")
    }
    to_insert = [item for item in items if item.identifier not in existing]

    with conn:
        for item in to_insert:
            conn.execute(
                "INSERT INTO task (identifier, tier, title, subtitle, "
                "target_repo, files_touched) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.identifier,
                    item.tier,
                    item.title,
                    item.subtitle,
                    item.target_repo,
                    json.dumps(list(item.files_touched)),
                ),
            )
    return len(to_insert)


def plan(repo_root: Path, conn: sqlite3.Connection) -> int:
    """Read roadmap + seed tasks. Returns number of new tasks created.

    Convenience wrapper for the common tick-of-the-engine flow.
    """
    items = read_roadmap(repo_root)
    return seed_tasks(conn, items)


__all__ = [
    "RoadmapParseError",
    "parse_roadmap",
    "plan",
    "read_roadmap",
    "seed_tasks",
]
