"""Context-compaction recovery via rolling resume snapshots.

Claude sessions can compact mid-task and lose their prior reasoning.
``handoff`` writes a small ``resume-<n>.md`` snapshot into the
worktree before each dispatch; if the session compacts and the next
dispatch picks up, it can read the most recent resume block to know
what was in flight.

Design:

- One file per dispatch, named ``resume-<task-identifier>-<timestamp>.md``
- Rolling retention: keep only the most recent ``keep`` snapshots
  per worktree (default 3). Older ones get pruned on write.
- Emits a ``handoff_written`` ledger event with the file path for
  post-mortem inspection.
- Reader (``read_latest``) returns the most recent snapshot body
  regardless of task identifier; callers decide whether to fold it
  into the next dispatch's prompt.

The snapshot content is the caller's call — handoff doesn't know what
the engine "should remember." Typical content: task identifier,
branch name, last 5 ledger events for this task, PR number if
observed. ``build_snapshot`` provides a sane default.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# Filename pattern for snapshots. Embeds task identifier + ISO timestamp
# so they're sortable and scrutable.
_SNAPSHOT_RE = re.compile(
    r"^resume-(?P<identifier>[A-Za-z0-9_\-]+)-"
    r"(?P<stamp>\d{8}T\d{6}Z)\.md$"
)


@dataclass(frozen=True)
class HandoffReport:
    """Outcome of one write."""

    path: Path
    pruned: tuple[Path, ...]


def _iso_compact(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def build_snapshot(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    identifier: str,
    title: str,
    branch: str,
    recent_events_limit: int = 10,
) -> str:
    """Compose a default markdown snapshot for this task.

    The snapshot is a plain reference: task metadata + the most recent
    N ledger events with kind + payload. Callers can swap this for
    their own formatter if they want richer content.
    """
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"# resume {identifier}",
        "",
        f"**Generated**: {now}",
        f"**Task ID**: {task_id}",
        f"**Title**: {title}",
        f"**Branch**: `{branch}`",
        "",
        f"## Recent events (last {recent_events_limit})",
        "",
    ]

    rows = list(
        conn.execute(
            "SELECT created_at, kind, payload FROM event "
            "WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, recent_events_limit),
        )
    )
    if not rows:
        lines.append("_(no events yet)_")
    else:
        for row in rows:
            kind = row["kind"] if hasattr(row, "keys") else row[1]
            created = row["created_at"] if hasattr(row, "keys") else row[0]
            payload_raw = row["payload"] if hasattr(row, "keys") else row[2]
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except (TypeError, ValueError):
                payload = {"_raw": str(payload_raw)[:200]}
            summary = json.dumps(payload, separators=(",", ":"))[:200]
            lines.append(f"- `{created}` **{kind}** {summary}")

    return "\n".join(lines) + "\n"


def write_snapshot(
    conn: sqlite3.Connection,
    *,
    worktree: Path,
    task_id: int,
    identifier: str,
    content: str,
    keep: int = 3,
    now: datetime | None = None,
) -> HandoffReport:
    """Write a snapshot + prune to ``keep`` most-recent per worktree.

    Arguments:
        conn: SQLite connection (to emit the ledger event).
        worktree: where to write. Must exist.
        task_id: for the ledger event.
        identifier: task identifier, embedded in the filename.
        content: the markdown body to write.
        keep: how many snapshots to retain post-prune. Must be >= 1.
        now: override for the timestamp component (tests).

    Returns a ``HandoffReport`` with the written path + pruned paths.
    """
    if keep < 1:
        raise ValueError(f"keep must be >= 1, got {keep}")
    if not worktree.is_dir():
        raise FileNotFoundError(f"worktree {worktree} does not exist")

    now_dt = now if now is not None else datetime.now(tz=UTC)
    filename = f"resume-{identifier}-{_iso_compact(now_dt)}.md"
    out_path = worktree / filename
    out_path.write_text(content, encoding="utf-8")

    # Prune: list every resume-*.md, sort by embedded timestamp, keep
    # the N most recent.
    pruned = _prune(worktree, keep=keep)

    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (
                task_id,
                "handoff_written",
                json.dumps({
                    "path": str(out_path),
                    "pruned": [str(p) for p in pruned],
                    "identifier": identifier,
                }),
            ),
        )

    return HandoffReport(path=out_path, pruned=tuple(pruned))


def _prune(worktree: Path, *, keep: int) -> list[Path]:
    """Delete all but the ``keep`` most-recent ``resume-*.md`` files.

    Returns the paths that were deleted.
    """
    snapshots: list[tuple[str, Path]] = []
    for entry in worktree.iterdir():
        if not entry.is_file():
            continue
        m = _SNAPSHOT_RE.match(entry.name)
        if m is None:
            continue
        snapshots.append((m.group("stamp"), entry))

    # Sort by timestamp descending (newest first).
    snapshots.sort(key=lambda t: t[0], reverse=True)

    pruned: list[Path] = []
    for _stamp, path in snapshots[keep:]:
        try:
            path.unlink()
            pruned.append(path)
        except OSError:
            logger.warning("handoff: failed to prune %s", path)
    return pruned


def read_latest(worktree: Path) -> str | None:
    """Return the body of the most recent ``resume-*.md``, or None.

    Used by the next dispatch on this worktree to recover prior
    context.
    """
    if not worktree.is_dir():
        return None
    snapshots: list[tuple[str, Path]] = []
    for entry in worktree.iterdir():
        if not entry.is_file():
            continue
        m = _SNAPSHOT_RE.match(entry.name)
        if m is None:
            continue
        snapshots.append((m.group("stamp"), entry))
    if not snapshots:
        return None
    snapshots.sort(key=lambda t: t[0], reverse=True)
    return snapshots[0][1].read_text(encoding="utf-8")


__all__ = [
    "HandoffReport",
    "build_snapshot",
    "read_latest",
    "write_snapshot",
]
