"""Model-routing table: map a *role* to a ``ModelChoice``.

The routing table answers the question "which adapter and model should
handle requests for role X?" without coupling call sites to hard-coded
model strings.  The table lives in
``oxi_core/defaults/routing.yaml`` and is documented inline there.

Public API
----------
``route_for(role, task=None) -> ModelChoice``
    Return the ``ModelChoice`` for *role*.  *task* is accepted for future
    use (e.g. per-task overrides in T2-40) but is currently ignored.
    Raises ``RoleNotConfiguredError`` if the role is absent from the table.

``ModelChoice``
    Frozen dataclass: ``(adapter_name, model_id, fallback_chain)``.

``RoleNotConfiguredError``
    Subclass of ``KeyError`` — callers can catch ``KeyError`` if they
    prefer not to import this exception directly.

``reload_routing_table()``
    Clear the module-level cache and re-read the YAML file.  Intended for
    use in tests that need a clean slate; production code should never call
    this.

Notes
-----
- The YAML file is loaded **once** per process, on first call to
  ``route_for``.  Subsequent calls use the cached table (an immutable dict
  of ``ModelChoice`` objects).

- The loader validates that every entry has ``adapter_name`` and
  ``model_id``.  Missing fields raise ``RoutingConfigError`` with a message
  that includes the file path so operators know where to look.

- No env-var overrides are implemented here (deferred to T2-40 if ever
  needed).  No call sites in dispatch.py or critic.py are wired up yet —
  that wiring is the next roadmap entry.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

#: Canonical path to the bundled routing table.
_ROUTING_YAML: Path = (
    Path(__file__).parent.parent / "defaults" / "routing.yaml"
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelChoice:
    """The fully-resolved routing decision for one role.

    Attributes:
        adapter_name: logical adapter identifier (e.g. ``"anthropic"``).
            Passed through to the dispatch layer; oxi-core does not
            validate it against installed adapters.
        model_id: primary model string (e.g. ``"claude-opus-4-7"``).
        fallback_chain: ordered sequence of model_ids to try when the
            primary model is unavailable.  Empty tuple means no fallback.
    """

    adapter_name: str
    model_id: str
    fallback_chain: tuple[str, ...]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RoleNotConfiguredError(KeyError):
    """Raised when *role* is absent from the routing table.

    Subclasses ``KeyError`` so callers that catch ``KeyError`` continue to
    work without importing this class.

    The ``args[0]`` is a human-readable message; ``role`` holds the raw
    role string for programmatic inspection.
    """

    def __init__(self, role: str, yaml_path: Path) -> None:
        self.role = role
        self.yaml_path = yaml_path
        msg = (
            f"role {role!r} is not configured in the routing table. "
            f"Add it to {os.fspath(yaml_path)} and restart the engine."
        )
        super().__init__(msg)

    def __str__(self) -> str:
        return str(self.args[0])


class RoutingConfigError(ValueError):
    """Raised when the YAML file is missing or structurally invalid.

    The message always includes the file path so operators know where
    to look.
    """


# ---------------------------------------------------------------------------
# YAML loading + validation
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, ModelChoice]:
    """Read *path*, parse YAML, validate schema, return immutable table.

    Raises:
        RoutingConfigError: file missing, unreadable, not a YAML mapping,
            or any entry is missing ``adapter_name`` / ``model_id``.
    """
    if not path.is_file():
        raise RoutingConfigError(
            f"routing.yaml not found at {os.fspath(path)}. "
            "This file is bundled with oxi-core; a missing file indicates "
            "a broken installation."
        )

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoutingConfigError(
            f"Could not read routing.yaml at {os.fspath(path)}: {exc}"
        ) from exc

    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RoutingConfigError(
            f"YAML parse error in {os.fspath(path)}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise RoutingConfigError(
            f"{os.fspath(path)} must be a YAML mapping (got {type(raw).__name__})"
        )

    table: dict[str, ModelChoice] = {}
    for role, entry in raw.items():
        role_str = str(role)
        if not isinstance(entry, dict):
            raise RoutingConfigError(
                f"{os.fspath(path)}: role {role_str!r} must be a mapping, "
                f"got {type(entry).__name__}"
            )

        adapter_name = entry.get("adapter_name")
        if not isinstance(adapter_name, str) or not adapter_name.strip():
            raise RoutingConfigError(
                f"{os.fspath(path)}: role {role_str!r} is missing a valid "
                "'adapter_name' string"
            )

        model_id = entry.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise RoutingConfigError(
                f"{os.fspath(path)}: role {role_str!r} is missing a valid "
                "'model_id' string"
            )

        raw_chain = entry.get("fallback_chain") or []
        if not isinstance(raw_chain, list):
            raise RoutingConfigError(
                f"{os.fspath(path)}: role {role_str!r} 'fallback_chain' must "
                f"be a list, got {type(raw_chain).__name__}"
            )
        fallback_chain: tuple[str, ...] = tuple(str(m) for m in raw_chain)

        table[role_str] = ModelChoice(
            adapter_name=adapter_name.strip(),
            model_id=model_id.strip(),
            fallback_chain=fallback_chain,
        )

    logger.debug("routing: loaded %d role(s) from %s", len(table), path)
    return table


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

# The routing table is intentionally module-level (not a class) to keep the
# API a single pure function.  lru_cache on an inner function provides the
# one-shot load + immutable-after-load semantics without a global variable
# that could be accidentally mutated.


@lru_cache(maxsize=1)
def _cached_table() -> dict[str, ModelChoice]:
    """Load and cache the routing table.  Called at most once per process."""
    return _load_yaml(_ROUTING_YAML)


def reload_routing_table() -> None:
    """Invalidate the in-process cache and force a re-read on next call.

    Intended for tests only.  Production code should never call this.
    """
    _cached_table.cache_clear()
    logger.debug("routing: cache cleared")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route_for(role: str, task: dict[str, Any] | None = None) -> ModelChoice:
    """Return the ``ModelChoice`` for *role*.

    Arguments:
        role: the routing role to look up, e.g. ``"worker"``,
              ``"critic"``, ``"heartbeat-triage"``.
        task: reserved for future per-task overrides (T2-40).
              Currently ignored.

    Returns:
        A ``ModelChoice`` with the adapter name, primary model ID, and
        ordered fallback chain for *role*.

    Raises:
        RoleNotConfiguredError: if *role* is absent from the table.
        RoutingConfigError: if the YAML file cannot be loaded or parsed
            (raised on first call only; cached on success).
    """
    # task parameter is intentionally unused until T2-40.
    _ = task
    table = _cached_table()
    if role not in table:
        raise RoleNotConfiguredError(role, _ROUTING_YAML)
    return table[role]


__all__ = [
    "ModelChoice",
    "RoleNotConfiguredError",
    "RoutingConfigError",
    "reload_routing_table",
    "route_for",
]
