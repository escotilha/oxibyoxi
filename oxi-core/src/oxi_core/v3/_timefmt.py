"""Shared time-formatting helpers for the v3 engine.

Consolidates the `_now_iso` function that was duplicated across 7 v3
modules (pr_watcher, deep_fix, ship_recovery, dispatch, auto_merge,
auto_recover, seed_from_roadmap). Single source of truth — changing the
format in one place cascades to every event-emitting module.

The SQLite schema stores timestamps as TEXT. `datetime.now(tz=UTC)` with
the space-separated format matches sqlite's `datetime('now')` default so
SQL-side comparisons against column values work uniformly.
"""

from __future__ import annotations

from datetime import UTC, datetime

# ISO-8601-ish format: "YYYY-MM-DD HH:MM:SS". Matches sqlite's
# datetime('now') output, which means `column > datetime('now')` and
# `column > ?` (with a now_iso()-formatted bind) compare identically.
_FMT = "%Y-%m-%d %H:%M:%S"


def now_iso() -> str:
    """Return the current UTC time in SQLite-compatible ISO format."""
    return datetime.now(tz=UTC).strftime(_FMT)


__all__ = ["now_iso"]
