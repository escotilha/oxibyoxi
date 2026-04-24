"""Roadmap → fronts projection.

``planner.plan`` reads the roadmap and creates tasks directly. That
works for the initial seed, but once the loop drains those tasks the
queue stays empty until an operator manually re-seeds.

``ingest_roadmap`` addresses this by projecting every roadmap item
into the ``front`` table — an intermediate structure that persists
across task lifecycles. ``seed_from_roadmap`` then auto-replenishes
the queue from ``front`` whenever the planned count drops below a
threshold.

Upsert semantics: re-ingesting an existing roadmap identifier updates
``title``, ``subtitle``, and ``tier`` (in case the roadmap was
reordered) but preserves ``last_seeded_at``. That way reseeding
cooldowns aren't bypassed by a trivial roadmap edit.

Emits a ``roadmap_ingested`` ledger event summarizing how many fronts
were inserted vs. updated.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..adapter import get_active_adapter
from ..planner import parse_roadmap


@dataclass(frozen=True)
class IngestReport:
    inserted: int
    updated: int
    total: int


def ingest(
    conn: sqlite3.Connection,
    *,
    repo_root: Path | None = None,
    default_cooldown_hours: float = 24.0,
) -> IngestReport:
    """Parse the adapter's roadmap and upsert each item into ``front``.

    ``repo_root`` defaults to ``adapter.paths().repo_root`` (or CWD
    if adapter doesn't set it).

    Idempotent: running twice on the same roadmap inserts nothing new.

    Each upsert preserves ``last_seeded_at`` so cooldowns aren't
    bypassed by trivial edits.
    """
    adapter = get_active_adapter()
    if repo_root is None:
        paths = adapter.paths()
        repo_root = Path(paths.repo_root) if paths.repo_root else Path.cwd()

    roadmap_path = repo_root / adapter.roadmap_location()
    if not roadmap_path.is_file():
        raise FileNotFoundError(f"roadmap not found at {roadmap_path}")

    items = parse_roadmap(roadmap_path.read_text(encoding="utf-8"))

    inserted = 0
    updated = 0
    with conn:
        for item in items:
            # Check existence; decide insert vs update based on actual
            # row presence rather than `INSERT OR REPLACE` (which would
            # wipe last_seeded_at on conflict).
            existing = conn.execute(
                "SELECT id, last_seeded_at FROM front WHERE identifier = ?",
                (item.identifier,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO front "
                    "(name, description, identifier, tier, title, subtitle, "
                    "cooldown_hours) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.identifier,  # name doubles as identifier
                        item.subtitle or "",
                        item.identifier,
                        item.tier,
                        item.title,
                        item.subtitle or "",
                        default_cooldown_hours,
                    ),
                )
                inserted += 1
            else:
                conn.execute(
                    "UPDATE front SET tier = ?, title = ?, subtitle = ?, "
                    "description = ? WHERE identifier = ?",
                    (
                        item.tier,
                        item.title,
                        item.subtitle or "",
                        item.subtitle or "",
                        item.identifier,
                    ),
                )
                updated += 1

        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (
                "roadmap_ingested",
                json.dumps({
                    "inserted": inserted,
                    "updated": updated,
                    "total": inserted + updated,
                }),
            ),
        )

    return IngestReport(
        inserted=inserted,
        updated=updated,
        total=inserted + updated,
    )


__all__ = ["IngestReport", "ingest"]
