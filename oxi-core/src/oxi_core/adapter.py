"""Adapter protocol — the single contract between oxi-core and a fork.

A fork implements `Adapter` to describe its project: naming, paths,
budget, repo, roadmap location, branch conventions, dispatch hosts,
promote recipe, plan tier, and policy. Core reads project-specific
values through the adapter; no engine module hardcodes anything a fork
would want to customize.

Per the plan:
- `plan_tier()` has no default. Missing → engine refuses to start.
- Every other method has a sensible default in `defaults.py`.
- Adapters that omit optional methods fall through to the default.

This module defines:
- Dataclasses for structured return values
- `Adapter` Protocol with 10 methods
- Module-level registration: register, get, clear

No core module imports from outside this file's public surface. The
`Adapter` protocol is the stable contract; internals may change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from . import defaults

# --- Dataclasses -------------------------------------------------------


@dataclass(frozen=True)
class NamingConfig:
    """How the instance identifies itself in user-facing surfaces.

    `instance_name` shows in dashboards, briefs, and prompts.
    `session_code_regex` validates worktree session tags.
    `branch_prefixes` is the whitelist the session-end hook recognizes.
    """

    instance_name: str = defaults.INSTANCE_NAME
    session_code_regex: str = defaults.SESSION_CODE_REGEX
    branch_prefixes: tuple[str, ...] = defaults.BRANCH_PREFIXES


@dataclass(frozen=True)
class BudgetCaps:
    """Spend limits. All USD."""

    daily_soft_warn: float = defaults.DAILY_SOFT_WARN_USD
    daily_hard_cap: float = defaults.DAILY_HARD_CAP_USD
    per_task_opus: float = defaults.PER_TASK_OPUS_USD
    per_task_sonnet: float = defaults.PER_TASK_SONNET_USD


@dataclass(frozen=True)
class PathsConfig:
    """On-disk paths the instance writes to.

    A field returned as None falls through to a sensible default
    computed relative to the repo root.
    """

    repo_root: str | None = None
    oxi_dir: str = defaults.OXI_DIR
    db_path: str | None = None
    brief_path: str | None = None
    dashboard_path: str | None = None
    release_lock_path: str | None = None
    daily_recap_path: str | None = None


@dataclass(frozen=True)
class DispatchHost:
    """A host where the engine can spawn Claude Code sessions.

    `name` is a label for logs and dashboard. `ssh_alias` is the
    SSH-config alias if remote, or None for local. `max_concurrent`
    bounds simultaneous dispatches on this host. `worktree_root` is
    where the host places per-task worktrees.
    """

    name: str
    ssh_alias: str | None
    max_concurrent: int
    worktree_root: str


@dataclass(frozen=True)
class PromoteRecipe:
    """Optional daily-promote configuration.

    If the adapter returns None from `promote_recipe()`, the engine
    skips the daily promote phase entirely.
    """

    cron_utc: str
    lock_duration_seconds: int
    staging_workflow: str
    production_workflow: str
    release_lock_path: str


@dataclass(frozen=True)
class RoadmapItem:
    """A single parsed roadmap entry.

    Identifier example: `T0-1`, `T2-17`. The tier (T0, T1, T2)
    determines dispatch priority; T0 items pause lower-tier work for
    one tick (the "tier-0 absorb" signal).
    """

    identifier: str
    tier: int
    title: str
    subtitle: str = ""
    target_repo: str | None = None
    files_touched: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DispatchPolicy:
    """Skill weights and plan tier overrides.

    `skill_weights` biases autonomous skill selection. `auto_merge`
    toggles whether the engine may merge PRs without a human approval.
    `tier_zero_absorb` toggles whether newly-added T0 items pause
    non-T0 dispatches for one tick.
    """

    skill_weights: dict[str, float] = field(default_factory=dict)
    auto_merge: bool = False
    tier_zero_absorb: bool = True


# --- Protocol ----------------------------------------------------------


@runtime_checkable
class Adapter(Protocol):
    """Contract every fork implements.

    Methods return the structured dataclasses above. Core reads through
    `get_active_adapter()` and uses the returned values. No method
    raises under normal operation; implementations must return a value
    even when the field is conceptually unset (use `None` inside
    dataclass fields to signal absence).
    """

    def naming(self) -> NamingConfig:
        """Instance-level identity."""

    def paths(self) -> PathsConfig:
        """On-disk paths."""

    def budget(self) -> BudgetCaps:
        """Spend limits."""

    def github_repo(self) -> str:
        """Owner/name slug the engine ships PRs against."""

    def roadmap_location(self) -> str:
        """Path to the roadmap markdown, relative to the target repo root."""

    def branch_prefixes(self) -> tuple[str, ...]:
        """Branch prefixes the session-end hook recognizes."""

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        """Hosts the engine may dispatch Claude sessions on."""

    def promote_recipe(self) -> PromoteRecipe | None:
        """Daily-promote configuration, or None to disable."""

    def plan_tier(self) -> str:
        """Claude plan tier the engine assumes (e.g. 'max_20x', 'standard').

        No default. Engine refuses to start if this returns an empty
        string. Required per anti-pattern #3.
        """

    def policy(self) -> DispatchPolicy:
        """Skill weights and dispatch behavior toggles."""


# --- Registration ------------------------------------------------------


_active_adapter: Adapter | None = None


class AdapterNotRegisteredError(RuntimeError):
    """Raised when engine code asks for the active adapter but none is set."""


class InvalidAdapterError(TypeError):
    """Raised when the object passed to register_adapter does not conform."""


def register_adapter(adapter: Adapter) -> None:
    """Set the process-wide active adapter.

    Raises `InvalidAdapterError` if the object does not satisfy the
    `Adapter` protocol, or if `plan_tier()` returns an empty string
    (anti-pattern #3).
    """
    global _active_adapter
    if not isinstance(adapter, Adapter):
        raise InvalidAdapterError(
            f"{type(adapter).__name__} does not implement the Adapter protocol"
        )
    tier = adapter.plan_tier()
    if not tier:
        raise InvalidAdapterError(
            f"{type(adapter).__name__}.plan_tier() returned empty; "
            "plan tier must be explicit (anti-pattern #3)"
        )
    _active_adapter = adapter


def get_active_adapter() -> Adapter:
    """Return the registered adapter.

    Raises `AdapterNotRegisteredError` if no adapter is active. Engine
    modules call this at startup; a missing adapter is a fatal
    configuration error, not a runtime condition to recover from.
    """
    if _active_adapter is None:
        raise AdapterNotRegisteredError(
            "no adapter registered; call register_adapter() before "
            "invoking the engine"
        )
    return _active_adapter


def clear_adapter() -> None:
    """Unset the active adapter. Intended for tests."""
    global _active_adapter
    _active_adapter = None
