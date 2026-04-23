"""Fallback constants for adapter-optional fields.

Every value here is used when an adapter does not override it. Values
must be project-neutral — no names, paths, or identifiers that tie the
engine to any one project.

Per the plan, `plan_tier` has no default. The engine refuses to start
if the active adapter omits it.
"""

from __future__ import annotations

# --- Identity ----------------------------------------------------------

INSTANCE_NAME: str = "oxi"
"""Default user-facing label. Appears in dashboard titles, brief headers,
and prompts when no adapter overrides it."""


# --- Branches ----------------------------------------------------------

BRANCH_PREFIXES: tuple[str, ...] = ("feat/", "fix/", "chore/")
"""Default branch prefixes the session-end hook recognizes. Adapters
expand this tuple for project-specific conventions."""

SESSION_CODE_REGEX: str = r"^[a-z]{2}[0-9a-z_]*$"
"""Two-letter session tag followed by alphanumerics. Matches worktree
naming conventions like `sa`, `sb`, `sc-focus`."""


# --- Paths -------------------------------------------------------------

OXI_DIR: str = ".oxi"
"""Per-project config directory. Holds local DB path, adapter pointer,
and ephemeral state. Lives at the repo root."""


# --- Budget (per-task safety ceilings, not the live cap) ---------------

PER_TASK_OPUS_USD: float = 5.0
"""Conservative ceiling for a single Opus-tier dispatch. Adapters raise
this for higher-budget projects."""

PER_TASK_SONNET_USD: float = 1.5
"""Conservative ceiling for a single Sonnet-tier dispatch."""

DAILY_SOFT_WARN_USD: float = 50.0
"""Alert threshold. Engine warns but continues dispatching."""

DAILY_HARD_CAP_USD: float = 100.0
"""Stop threshold. Engine halts dispatch until the next UTC day or until
an operator resets the cap."""


# --- Reaping + cadence -------------------------------------------------

LAST_PROGRESS_STALE_SECONDS: int = 600
"""How long a dispatched task may go without progress before heartbeat
reaps it. Anti-pattern #3 enforcement: dispatched transitions must stamp
`last_progress_at` immediately, or this reaper fires prematurely."""

TICK_INTERVAL_SECONDS: int = 30
"""Default interval between engine ticks. Adapters override for slower
or faster cadence."""


# --- Roadmap -----------------------------------------------------------

ROADMAP_FILENAME: str = "roadmap.md"
"""Default roadmap filename under the target repo. Adapters override for
project-specific layouts."""
