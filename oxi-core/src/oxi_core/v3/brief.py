"""Daily brief — a human-readable summary of engine activity.

The brief answers "what did the engine do today?" and writes to the
path configured by ``adapter.paths().brief_path`` (or a caller-supplied
override). Consumed by operators and by the dashboard.

Structure is intentionally simple: one markdown file per run, with
counts by status + a short event log. Not a full audit report; that's
what the event table is for.

The brief is pure read — no DB writes. Safe to run concurrently with
the engine.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class Brief:
    """Structured summary. ``render()`` turns it into markdown."""

    generated_at: datetime
    window_hours: int
    status_counts: dict[str, int]
    total_cost_usd: float
    recent_merges: list[tuple[str, str]]  # (identifier, title)
    recent_failures: list[tuple[str, str, str]]  # (identifier, title, reason)

    def render(self) -> str:
        total = sum(self.status_counts.values())
        lines = [
            f"# oxi brief — {self.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            f"Window: last {self.window_hours}h. Tasks observed: {total}.",
            "",
            "## Status counts",
            "",
        ]
        if not self.status_counts:
            lines.append("_(no tasks)_")
        else:
            for status in sorted(self.status_counts):
                lines.append(f"- **{status}**: {self.status_counts[status]}")
        lines += [
            "",
            f"## Spend (window): ${self.total_cost_usd:.2f}",
            "",
            "## Recent merges",
            "",
        ]
        if not self.recent_merges:
            lines.append("_(none)_")
        else:
            for ident, title in self.recent_merges:
                lines.append(f"- `{ident}` — {title}")
        lines += ["", "## Recent failures", ""]
        if not self.recent_failures:
            lines.append("_(none)_")
        else:
            for ident, title, reason in self.recent_failures:
                lines.append(f"- `{ident}` — {title} — _{reason}_")
        return "\n".join(lines) + "\n"


def _now_iso_minus(hours: int) -> str:
    cutoff = datetime.now(tz=UTC) - timedelta(hours=hours)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def generate(
    conn: sqlite3.Connection,
    *,
    window_hours: int = 24,
    merges_limit: int = 10,
    failures_limit: int = 10,
    now: datetime | None = None,
) -> Brief:
    """Collect engine state into a ``Brief``. Pure read."""
    now_dt = now if now is not None else datetime.now(tz=UTC)
    cutoff_str = (now_dt - timedelta(hours=window_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Status counts for tasks updated within the window.
    status_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM task "
        "WHERE updated_at >= ? GROUP BY status",
        (cutoff_str,),
    ):
        status_counts[row["status"]] = row["n"]

    cost_row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM task "
        "WHERE updated_at >= ?",
        (cutoff_str,),
    ).fetchone()
    total_cost = float(cost_row["total"] or 0.0)

    merges = [
        (row["identifier"], row["title"])
        for row in conn.execute(
            "SELECT identifier, title FROM task "
            "WHERE status = 'merged' AND merged_at >= ? "
            "ORDER BY merged_at DESC LIMIT ?",
            (cutoff_str, merges_limit),
        )
    ]
    failures = [
        (row["identifier"], row["title"], row["failure_reason"] or "unknown")
        for row in conn.execute(
            "SELECT identifier, title, failure_reason FROM task "
            "WHERE status IN ('failed', 'abandoned') AND updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (cutoff_str, failures_limit),
        )
    ]

    return Brief(
        generated_at=now_dt,
        window_hours=window_hours,
        status_counts=status_counts,
        total_cost_usd=total_cost,
        recent_merges=merges,
        recent_failures=failures,
    )


__all__ = ["Brief", "generate"]
