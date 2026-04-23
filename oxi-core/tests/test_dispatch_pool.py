"""Tests for oxi_core.v3.dispatch_pool."""

from __future__ import annotations

import threading

import pytest

from oxi_core.adapter import DispatchHost
from oxi_core.v3.dispatch_pool import DispatchPool, HostSlot, PoolError


def _host(name: str, max_concurrent: int = 1) -> DispatchHost:
    return DispatchHost(
        name=name,
        ssh_alias=None,
        max_concurrent=max_concurrent,
        worktree_root=f"/tmp/wt/{name}",
    )


class _FixedProvider:
    """Minimal HostProvider for tests — returns a fixed tuple."""

    def __init__(self, hosts: tuple[DispatchHost, ...]):
        self._hosts = hosts

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return self._hosts


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_empty_pool_snapshot_shows_zero_live():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 2), _host("b", 1))))
    slots = pool.snapshot()
    assert len(slots) == 2
    assert all(s.live == 0 for s in slots)
    assert all(s.has_capacity for s in slots)


def test_snapshot_reflects_registered_starts():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 3),)))
    pool.register_start("a")
    pool.register_start("a")
    slot = pool.snapshot()[0]
    assert slot.live == 2
    assert slot.available == 1
    assert slot.has_capacity is True


def test_snapshot_host_order_matches_provider():
    pool = DispatchPool(
        provider=_FixedProvider((_host("z"), _host("a"), _host("m")))
    )
    slots = pool.snapshot()
    assert [s.host.name for s in slots] == ["z", "a", "m"]


# ---------------------------------------------------------------------------
# pick()
# ---------------------------------------------------------------------------


def test_pick_returns_first_host_when_all_empty():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 2), _host("b", 2))))
    host = pool.pick()
    assert host is not None
    assert host.name == "a"


def test_pick_prefers_least_loaded_host():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 5), _host("b", 5))))
    pool.register_start("a")
    pool.register_start("a")
    # b has 0 live; a has 2. b wins.
    host = pool.pick()
    assert host.name == "b"


def test_pick_respects_capacity_limits():
    pool = DispatchPool(
        provider=_FixedProvider((_host("a", 1), _host("b", 2)))
    )
    pool.register_start("a")  # a is full
    pool.register_start("b")  # b has 1 of 2
    host = pool.pick()
    # Only b has capacity.
    assert host.name == "b"


def test_pick_returns_none_when_all_hosts_full():
    pool = DispatchPool(
        provider=_FixedProvider((_host("a", 1), _host("b", 1)))
    )
    pool.register_start("a")
    pool.register_start("b")
    assert pool.pick() is None


def test_pick_tie_broken_by_adapter_order():
    pool = DispatchPool(provider=_FixedProvider((_host("z", 3), _host("a", 3))))
    # Both empty → z wins (first in adapter tuple).
    assert pool.pick().name == "z"


def test_pick_does_not_increment_count():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 2),)))
    pool.pick()
    pool.pick()
    slot = pool.snapshot()[0]
    assert slot.live == 0


# ---------------------------------------------------------------------------
# register_start / register_end
# ---------------------------------------------------------------------------


def test_register_start_rejects_unknown_host():
    pool = DispatchPool(provider=_FixedProvider((_host("a"),)))
    with pytest.raises(PoolError, match="unknown host"):
        pool.register_start("ghost")


def test_register_end_clamps_at_zero():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 3),)))
    pool.register_end("a")  # never started; should not raise
    pool.register_end("a")
    slot = pool.snapshot()[0]
    assert slot.live == 0


def test_register_end_decrements():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 3),)))
    pool.register_start("a")
    pool.register_start("a")
    pool.register_end("a")
    slot = pool.snapshot()[0]
    assert slot.live == 1


def test_register_end_on_unknown_host_is_noop():
    """Recovery path: stale record for a host no longer in the adapter."""
    pool = DispatchPool(provider=_FixedProvider((_host("a"),)))
    # Should not raise even though "b" is not a host.
    pool.register_end("b")


# ---------------------------------------------------------------------------
# reset / reset_host
# ---------------------------------------------------------------------------


def test_reset_clears_all_counts():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 5), _host("b", 5))))
    pool.register_start("a")
    pool.register_start("a")
    pool.register_start("b")
    pool.reset()
    slots = pool.snapshot()
    assert all(s.live == 0 for s in slots)


def test_reset_host_clears_only_that_host():
    pool = DispatchPool(provider=_FixedProvider((_host("a", 5), _host("b", 5))))
    pool.register_start("a")
    pool.register_start("a")
    pool.register_start("b")
    pool.reset_host("a")
    by_name = {s.host.name: s.live for s in pool.snapshot()}
    assert by_name["a"] == 0
    assert by_name["b"] == 1


def test_reset_host_on_unknown_is_noop():
    pool = DispatchPool(provider=_FixedProvider((_host("a"),)))
    pool.reset_host("nonexistent")  # no raise


# ---------------------------------------------------------------------------
# capacity_summary
# ---------------------------------------------------------------------------


def test_capacity_summary_reports_available_slots():
    pool = DispatchPool(
        provider=_FixedProvider((_host("a", 3), _host("b", 2)))
    )
    pool.register_start("a")
    pool.register_start("b")
    pool.register_start("b")  # b is full
    summary = pool.capacity_summary()
    assert summary == {"a": 2, "b": 0}


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_register_start_is_threadsafe():
    """100 concurrent starts, count should be exactly 100."""
    pool = DispatchPool(provider=_FixedProvider((_host("a", 1000),)))
    threads: list[threading.Thread] = []

    def bump() -> None:
        for _ in range(10):
            pool.register_start("a")

    for _ in range(10):
        t = threading.Thread(target=bump)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    slot = pool.snapshot()[0]
    assert slot.live == 100


# ---------------------------------------------------------------------------
# HostSlot
# ---------------------------------------------------------------------------


def test_host_slot_has_capacity_and_available():
    h = _host("a", 3)
    slot = HostSlot(host=h, live=2)
    assert slot.available == 1
    assert slot.has_capacity is True

    full = HostSlot(host=h, live=3)
    assert full.available == 0
    assert full.has_capacity is False

    over = HostSlot(host=h, live=999)  # defensive
    assert over.available == 0
    assert over.has_capacity is False


# ---------------------------------------------------------------------------
# Dynamic host list
# ---------------------------------------------------------------------------


class _MutableProvider:
    def __init__(self) -> None:
        self._hosts = (_host("a"),)

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return self._hosts

    def set_hosts(self, hosts: tuple[DispatchHost, ...]) -> None:
        self._hosts = hosts


def test_pool_reflects_adapter_host_changes():
    prov = _MutableProvider()
    pool = DispatchPool(provider=prov)
    assert [s.host.name for s in pool.snapshot()] == ["a"]
    prov.set_hosts((_host("a"), _host("b")))
    assert [s.host.name for s in pool.snapshot()] == ["a", "b"]


def test_pool_drops_counts_for_removed_hosts_from_snapshot():
    prov = _MutableProvider()
    pool = DispatchPool(provider=prov)
    pool.register_start("a")
    # Remove "a" entirely.
    prov.set_hosts((_host("b"),))
    slots = pool.snapshot()
    assert [s.host.name for s in slots] == ["b"]
    # "a"'s count is not visible anymore.
    assert pool.capacity_summary() == {"b": 1}
