"""Host selection for dispatches.

A fork's adapter lists one or more ``DispatchHost`` entries. The pool
tracks how many dispatches are currently live on each, and picks the
least-loaded host that still has capacity.

Responsibilities:

- Maintain an in-memory count of live dispatches per host.
- Pick the next host for a new dispatch, or return None when full.
- Let callers register a dispatch start (increment) and completion
  (decrement). Mis-matched increments/decrements are detected and
  clamped at zero — no negative counts.

Non-responsibilities:

- This module does NOT spawn SSH. Remote dispatch is a future
  follow-up (the ``DispatchHost.ssh_alias`` field is already in the
  adapter protocol from P1-2, reserved for that use).
- This module does NOT know about task state. It only knows "N
  dispatches are live on host X."

The pool is intentionally small. In the current scope, every adapter
ships with one local host (via ``_reference``), so the pool reduces
to "accept up to max_concurrent". But doing the bookkeeping right
now means P1-5d heartbeat + future SSH dispatch slot in without a
rewrite.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from ..adapter import DispatchHost, get_active_adapter


class PoolError(RuntimeError):
    """Raised when the pool is asked to operate on an unknown host."""


class HostProvider(Protocol):
    """Minimal interface for looking up the current dispatch hosts.

    Exists so tests can inject a deterministic host list without
    registering a full adapter.
    """

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]: ...


@dataclass(frozen=True)
class HostSlot:
    """A host + its currently-live dispatch count.

    Snapshots of pool state; callers must not mutate.
    """

    host: DispatchHost
    live: int

    @property
    def has_capacity(self) -> bool:
        return self.live < self.host.max_concurrent

    @property
    def available(self) -> int:
        """Slots still free on this host. Never negative."""
        return max(0, self.host.max_concurrent - self.live)


class DispatchPool:
    """Process-wide pool tracking per-host live-dispatch counts.

    Thread-safe. A single process owns one pool; tests construct their
    own. The pool is keyed by ``host.name`` — adapters must return
    hosts with distinct names.

    Hosts are re-read on every call to ``pick()`` and ``snapshot()``
    so an adapter that changes its host list at runtime (rare but
    possible) is reflected immediately. Counts persist across host
    list changes; if a host is removed, its count is dropped when
    next observed absent.
    """

    def __init__(self, provider: HostProvider | None = None) -> None:
        self._provider = provider
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    # ---- Registration --------------------------------------------------

    def register_start(self, host_name: str) -> None:
        """Increment the live-dispatch count for ``host_name``.

        Raises ``PoolError`` if no host with that name appears in the
        current host list — dispatchers should only call this for a
        host they just got from ``pick()``.
        """
        hosts = self._current_hosts()
        if not any(h.name == host_name for h in hosts):
            raise PoolError(f"unknown host {host_name!r}")
        with self._lock:
            self._counts[host_name] = self._counts.get(host_name, 0) + 1

    def register_end(self, host_name: str) -> None:
        """Decrement the live-dispatch count for ``host_name``.

        Clamps at zero: calling ``register_end`` more times than
        ``register_start`` is a no-op rather than a raise. The reason
        is recovery — if a process crashes mid-dispatch, the heartbeat
        reaper will call ``register_end`` to clean up, and it should
        not blow up on a stale record.
        """
        with self._lock:
            current = self._counts.get(host_name, 0)
            if current > 0:
                self._counts[host_name] = current - 1

    def reset_host(self, host_name: str) -> None:
        """Drop the live-dispatch count for ``host_name`` back to zero.

        Used by recovery paths: a crash-restart of the engine should
        reset counts (no live dispatches across processes).
        """
        with self._lock:
            self._counts.pop(host_name, None)

    def reset(self) -> None:
        """Drop every host's count back to zero."""
        with self._lock:
            self._counts.clear()

    # ---- Queries -------------------------------------------------------

    def snapshot(self) -> tuple[HostSlot, ...]:
        """Return a per-host capacity snapshot.

        The snapshot is taken under the lock; counts are consistent
        with each other. Host order matches the adapter's order.
        """
        hosts = self._current_hosts()
        with self._lock:
            return tuple(
                HostSlot(host=h, live=self._counts.get(h.name, 0)) for h in hosts
            )

    def pick(self) -> DispatchHost | None:
        """Return the least-loaded host with capacity, or None if all full.

        Ties are broken by host order in the adapter's tuple (stable).
        The count is NOT incremented — call ``register_start`` after
        you've committed to using the host.
        """
        slots = self.snapshot()
        # Filter to hosts with capacity; pick the one with fewest live.
        # Stable min ensures adapter-order tiebreak.
        candidates = [s for s in slots if s.has_capacity]
        if not candidates:
            return None
        best = min(candidates, key=lambda s: s.live)
        return best.host

    def capacity_summary(self) -> dict[str, int]:
        """Map host_name → slots-still-free. Useful for dashboards."""
        return {s.host.name: s.available for s in self.snapshot()}

    # ---- Internal ------------------------------------------------------

    def _current_hosts(self) -> tuple[DispatchHost, ...]:
        if self._provider is not None:
            return self._provider.dispatch_hosts()
        return get_active_adapter().dispatch_hosts()


__all__ = [
    "DispatchPool",
    "HostSlot",
    "PoolError",
]
