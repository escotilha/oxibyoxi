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
- Auto-discovery: load_adapter() via oxi.toml, entry-points, or
  OXI_ADAPTER env var

No core module imports from outside this file's public surface. The
`Adapter` protocol is the stable contract; internals may change.

Auto-discovery
--------------
Resolution order (first wins):

1. **Already registered** — explicit ``register_adapter()`` calls always
   take priority.

2. **OXI_ADAPTER env var** — ``OXI_ADAPTER=module:ClassName``.  The
   class is imported and instantiated with no arguments, then
   registered.

3. **oxi.toml** — if an ``oxi.toml`` file exists in the current working
   directory, its ``[adapter]`` section is read::

       [adapter]
       class = "oxi_adapter_myproject:MyAdapter"

       [adapter.kwargs]
       repo_root = "."

   ``class`` is a ``module:ClassName`` spec.  ``kwargs`` is an optional
   table of keyword arguments forwarded to the constructor.  String
   values whose key ends with ``_root``, ``_path``, or ``_dir`` are
   resolved as paths relative to the directory that contains
   ``oxi.toml`` before being passed to the constructor.

4. **Entry-point discovery** — packages that declare
   ``[project.entry-points."oxi.adapters"]`` in their pyproject.toml.
   If exactly one is installed it is loaded and registered with no
   constructor arguments.  If more than one is installed a
   ``MultipleAdaptersError`` is raised (use ``OXI_ADAPTER`` to
   disambiguate).

``load_adapter()`` is idempotent: if an adapter is already registered it
returns immediately.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
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


# EscalationConfig is defined in v3.deep_fix to avoid a circular
# import (adapter ← deep_fix ← dispatch ← adapter).  Adapters that
# want to customise deep_fix behaviour import it directly:
#
#   from oxi_core.v3.deep_fix import EscalationConfig, EscalationRecipe
#
# and implement an ``escalation_config()`` method on their adapter class.
# The engine reads this method if it exists; adapters that omit it fall
# back to the ``EscalationConfig`` defaults.  The method is intentionally
# NOT part of the ``Adapter`` Protocol below so that existing adapters
# written before T1-13 continue to satisfy the protocol without changes.


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


# --- Auto-discovery --------------------------------------------------------

_ENTRY_POINT_GROUP = "oxi.adapters"
_ENV_VAR = "OXI_ADAPTER"
_TOML_CONFIG_NAME = "oxi.toml"

# Keys whose string values are resolved as paths relative to the TOML file.
# Any kwarg whose name ends with one of these suffixes is treated as a path.
_PATH_KWARG_SUFFIXES = ("_root", "_path", "_dir")


class MultipleAdaptersError(RuntimeError):
    """Raised when more than one adapter is installed and no env var pins one.

    Pin the desired adapter with::

        OXI_ADAPTER=oxi_adapter_myproject:MyAdapter oxi status
    """


class AdapterLoadError(RuntimeError):
    """Raised when the OXI_ADAPTER env var, oxi.toml, or an entry-point cannot
    be loaded."""


def _import_class(spec: str, source_label: str) -> type:
    """Import a class from a ``module:ClassName`` spec.

    ``source_label`` is included in error messages (e.g. ``"OXI_ADAPTER"``
    or ``"oxi.toml [adapter] class"``).

    Raises `AdapterLoadError` on any import/attribute failure.
    """
    if ":" not in spec:
        raise AdapterLoadError(
            f"{source_label}={spec!r} is not a valid 'module:ClassName' spec; "
            "expected something like 'oxi_adapter_myproject:MyAdapter'"
        )
    module_path, class_name = spec.rsplit(":", 1)
    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise AdapterLoadError(
            f"{source_label}: cannot import module {module_path!r}: {exc}"
        ) from exc
    try:
        cls = getattr(mod, class_name)
    except AttributeError as exc:
        raise AdapterLoadError(
            f"{source_label}: module {module_path!r} has no attribute {class_name!r}"
        ) from exc
    return cls  # type: ignore[return-value]


def _load_from_env(spec: str) -> Adapter:
    """Import and instantiate an adapter class from a ``module:ClassName`` spec.

    Raises `AdapterLoadError` on any import/attribute failure.
    """
    cls = _import_class(spec, _ENV_VAR)
    try:
        instance: Adapter = cls()
    except TypeError as exc:
        raise AdapterLoadError(
            f"{_ENV_VAR}: {cls.__name__}() raised TypeError — does it require "
            f"constructor arguments? ({exc})"
        ) from exc
    return instance


def _resolve_path_kwargs(kwargs: dict[str, object], toml_dir: Path) -> dict[str, object]:
    """Resolve string kwargs whose keys end with a path suffix.

    Paths are resolved relative to ``toml_dir`` (the directory that
    contains ``oxi.toml``). Non-string values and keys without a path
    suffix are passed through unchanged.
    """
    resolved: dict[str, object] = {}
    for key, value in kwargs.items():
        if isinstance(value, str) and any(key.endswith(s) for s in _PATH_KWARG_SUFFIXES):
            resolved[key] = str((toml_dir / value).resolve())
        else:
            resolved[key] = value
    return resolved


def _load_from_toml(toml_path: Path) -> Adapter | None:
    """Read ``oxi.toml`` and instantiate the configured adapter.

    Returns ``None`` if the file has no ``[adapter]`` section (so the
    caller can fall through to entry-point discovery).

    Raises `AdapterLoadError` if the section is present but malformed or
    the class cannot be imported / instantiated.
    """
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover — Python < 3.11 fallback
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise AdapterLoadError(
                "oxi.toml: cannot read TOML — install 'tomli' on Python < 3.11"
            ) from exc

    try:
        with toml_path.open("rb") as fh:
            config = tomllib.load(fh)
    except Exception as exc:
        raise AdapterLoadError(
            f"oxi.toml: failed to parse {toml_path}: {exc}"
        ) from exc

    adapter_section = config.get("adapter")
    if adapter_section is None:
        return None  # file present but no [adapter] — fall through

    spec = adapter_section.get("class")
    if not spec:
        raise AdapterLoadError(
            f"oxi.toml: [adapter] section is missing the 'class' key in {toml_path}"
        )

    raw_kwargs: dict[str, object] = adapter_section.get("kwargs", {})
    toml_dir = toml_path.parent
    kwargs = _resolve_path_kwargs(raw_kwargs, toml_dir)

    cls = _import_class(spec, f"oxi.toml [adapter] class")
    try:
        instance: Adapter = cls(**kwargs)
    except TypeError as exc:
        raise AdapterLoadError(
            f"oxi.toml: {cls.__name__}(**kwargs) raised TypeError — "
            f"check the [adapter.kwargs] table in {toml_path}: {exc}"
        ) from exc
    return instance


def find_oxi_toml(start: Path | None = None) -> Path | None:
    """Return the path to ``oxi.toml`` if one exists in ``start`` (default: CWD).

    Only looks in the given directory, not parent directories, to keep
    the resolution predictable for operators using launchd/systemd units
    that ``cd`` into the repo root.
    """
    directory = start if start is not None else Path.cwd()
    candidate = directory / _TOML_CONFIG_NAME
    return candidate if candidate.is_file() else None


def load_adapter() -> None:
    """Auto-discover and register an adapter if none is already active.

    Resolution order (first wins):

    1. **Already registered** — if `get_active_adapter()` succeeds, return
       immediately. Explicit `register_adapter()` calls always take priority.

    2. **OXI_ADAPTER env var** — ``OXI_ADAPTER=module:ClassName``. The class
       is imported and instantiated with no arguments, then registered.

    3. **oxi.toml** — if ``oxi.toml`` exists in the current working directory
       and contains an ``[adapter]`` section, the class named by ``class`` is
       imported and instantiated with the keyword arguments from
       ``[adapter.kwargs]`` (paths resolved relative to the toml file).

    4. **Entry-point discovery** — all packages that declare
       ``[project.entry-points."oxi.adapters"]`` in their pyproject.toml are
       collected. Each entry-point value must be a ``module:ClassName`` spec.
       If exactly one is found it is loaded and registered. If more than one
       is found a `MultipleAdaptersError` is raised (use ``OXI_ADAPTER`` to
       disambiguate).

    If no adapter is found by any mechanism this function returns without
    registering anything; the subsequent `get_active_adapter()` call in
    ``_require_adapter`` will raise `AdapterNotRegisteredError` with the
    normal user-facing message.

    Raises:
        AdapterLoadError: env var, oxi.toml, or entry-point spec cannot be
            imported.
        MultipleAdaptersError: more than one entry-point is installed and
            ``OXI_ADAPTER`` is not set.
        InvalidAdapterError: loaded class does not satisfy the Adapter
            protocol or returns an empty plan_tier.
    """
    # 1. Already registered — nothing to do.
    if _active_adapter is not None:
        return

    # 2. OXI_ADAPTER env var — explicit override.
    env_spec = os.environ.get(_ENV_VAR)
    if env_spec:
        adapter = _load_from_env(env_spec)
        register_adapter(adapter)
        return

    # 3. oxi.toml in CWD.
    toml_path = find_oxi_toml()
    if toml_path is not None:
        adapter = _load_from_toml(toml_path)
        if adapter is not None:
            register_adapter(adapter)
            return

    # 4. Entry-point discovery.
    eps = list(entry_points(group=_ENTRY_POINT_GROUP))
    if not eps:
        return  # nothing installed; let _require_adapter emit the friendly error

    if len(eps) > 1:
        names = ", ".join(ep.name for ep in eps)
        raise MultipleAdaptersError(
            f"Multiple oxi adapters are installed ({names}). "
            f"Pin one with: {_ENV_VAR}=module:ClassName"
        )

    ep = eps[0]
    try:
        cls = ep.load()
    except Exception as exc:
        raise AdapterLoadError(
            f"Failed to load adapter entry-point {ep.name!r} "
            f"({ep.value!r}): {exc}"
        ) from exc

    try:
        adapter = cls()
    except TypeError as exc:
        raise AdapterLoadError(
            f"Adapter class {ep.value!r} raised TypeError on instantiation — "
            f"does it require constructor arguments? Use oxi.toml to supply "
            f"kwargs. ({exc})"
        ) from exc

    register_adapter(adapter)
