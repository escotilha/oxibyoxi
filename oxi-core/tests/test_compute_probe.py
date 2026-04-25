"""Tests for oxi_core.compute_probe."""

from __future__ import annotations

import subprocess

import pytest

from oxi_core.compute_probe import (
    DEFAULT_DAILY_HARD_CAP_USD,
    PLAN_TIER_CONCURRENCY,
    PLAN_TIER_HARD_CAPS,
    HostCapacity,
    _read_cpu_cores_linux,
    _read_cpu_cores_macos,
    _read_load_avg_posix,
    _read_plan_tier,
    _read_ram_gb_linux,
    _read_ram_gb_macos,
    probe_and_recommend,
    probe_cpu_count,
    probe_host,
    probe_load_avg,
    recommend,
    recommend_concurrency,
    recommend_ram_concurrency,
)

# A no-op load_avg_reader for tests that aren't testing the load-avg path.
# Using load_avg=0.0 means "host is idle" so the CPU envelope never constrains.
_IDLE_LOAD_AVG: float = 0.0

# ---------------------------------------------------------------------------
# Probe primitives — macOS path
# ---------------------------------------------------------------------------


def test_read_ram_gb_macos_parses_sysctl():
    """48 GB Mac mini: sysctl returns hw.memsize as bytes."""
    def fake_run(cmd: list[str]) -> str:
        assert cmd == ["sysctl", "-n", "hw.memsize"]
        return "51539607552\n"  # 48 GB in bytes

    result = _read_ram_gb_macos(fake_run)
    assert result is not None
    assert 47.5 < result < 48.5


def test_read_ram_gb_macos_handles_missing_sysctl():
    def fake_run(cmd: list[str]) -> str:
        raise FileNotFoundError("sysctl: not found")

    assert _read_ram_gb_macos(fake_run) is None


def test_read_ram_gb_macos_handles_nonzero_exit():
    def fake_run(cmd: list[str]) -> str:
        raise subprocess.CalledProcessError(1, cmd)

    assert _read_ram_gb_macos(fake_run) is None


def test_read_ram_gb_macos_handles_garbage_output():
    def fake_run(cmd: list[str]) -> str:
        return "not a number\n"

    assert _read_ram_gb_macos(fake_run) is None


def test_read_cpu_cores_macos():
    def fake_run(cmd: list[str]) -> str:
        return "10\n"

    assert _read_cpu_cores_macos(fake_run) == 10


def test_read_cpu_cores_macos_handles_failure():
    def fake_run(cmd: list[str]) -> str:
        raise FileNotFoundError("sysctl missing")

    assert _read_cpu_cores_macos(fake_run) is None


# ---------------------------------------------------------------------------
# Probe primitives — Linux path
# ---------------------------------------------------------------------------


def test_read_ram_gb_linux_parses_meminfo():
    meminfo = """\
MemTotal:        49283456 kB
MemFree:          2843288 kB
MemAvailable:    13428752 kB
"""
    result = _read_ram_gb_linux(meminfo)
    assert result is not None
    assert 46.5 < result < 47.5  # ~47 GB


def test_read_ram_gb_linux_handles_no_memtotal():
    meminfo = "Cached: 1024 kB\n"
    assert _read_ram_gb_linux(meminfo) is None


def test_read_ram_gb_linux_handles_none_input():
    assert _read_ram_gb_linux(None) is None


def test_read_ram_gb_linux_handles_garbage():
    assert _read_ram_gb_linux("MemTotal:        not_a_number kB") is None


def test_read_cpu_cores_linux():
    cpuinfo = """\
processor   : 0
vendor_id   : AuthenticAMD
processor   : 1
vendor_id   : AuthenticAMD
processor   : 2
processor   : 3
"""
    assert _read_cpu_cores_linux(cpuinfo) == 4


def test_read_cpu_cores_linux_handles_none():
    assert _read_cpu_cores_linux(None) is None


def test_read_cpu_cores_linux_handles_empty():
    assert _read_cpu_cores_linux("") is None


# ---------------------------------------------------------------------------
# Plan tier from env
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("standard", "standard"),
        ("max_5x", "max_5x"),
        ("max_20x", "max_20x"),
        ("MAX_20X", "max_20x"),  # case-insensitive
        ("  max_5x  ", "max_5x"),  # whitespace tolerated
        ("", None),
        ("max_50x", None),  # unknown tier
        ("free", None),
    ],
)
def test_read_plan_tier(value: str, expected: str | None):
    assert _read_plan_tier({"ANTHROPIC_PLAN": value}) == expected


def test_read_plan_tier_missing_env():
    assert _read_plan_tier({}) is None


# ---------------------------------------------------------------------------
# Top-level probe_host orchestration
# ---------------------------------------------------------------------------


def test_probe_host_macos_path():
    def fake_run(cmd: list[str]) -> str:
        if "memsize" in cmd[-1]:
            return "51539607552\n"  # 48 GB
        if "ncpu" in cmd[-1]:
            return "10\n"
        raise AssertionError(f"unexpected cmd {cmd}")

    result = probe_host(
        run=fake_run,
        read_file=lambda _: None,
        env={},
        platform_name="Darwin",
        load_avg_reader=lambda: _IDLE_LOAD_AVG,
    )
    assert result.platform_name == "Darwin"
    assert result.ram_gb is not None
    assert 47.5 < result.ram_gb < 48.5
    assert result.cpu_cores == 10
    assert result.plan_tier is None  # not in env
    assert result.load_avg_5min == _IDLE_LOAD_AVG


def test_probe_host_linux_path():
    def fake_read(path: str) -> str | None:
        if path == "/proc/meminfo":
            return "MemTotal:        16777216 kB\n"  # 16 GB
        if path == "/proc/cpuinfo":
            return "processor : 0\nprocessor : 1\nprocessor : 2\nprocessor : 3\n"
        return None

    def fake_run(cmd: list[str]) -> str:
        raise AssertionError(f"linux probe should not call subprocess: {cmd}")

    result = probe_host(
        run=fake_run,
        read_file=fake_read,
        env={"ANTHROPIC_PLAN": "max_5x"},
        platform_name="Linux",
        load_avg_reader=lambda: 1.5,
    )
    assert result.platform_name == "Linux"
    assert result.ram_gb is not None
    assert 15 < result.ram_gb < 17
    assert result.cpu_cores == 4
    assert result.plan_tier == "max_5x"
    assert result.load_avg_5min == 1.5


def test_probe_host_windows_falls_back_to_defaults():
    """Unsupported platform → Nones rather than errors."""
    result = probe_host(
        run=lambda cmd: "",
        read_file=lambda _: None,
        env={},
        platform_name="Windows",
        load_avg_reader=lambda: None,
    )
    assert result.platform_name == "Windows"
    assert result.ram_gb is None
    assert result.cpu_cores is None
    assert result.plan_tier is None
    assert result.load_avg_5min is None


def test_probe_host_handles_total_failure():
    """Every probe primitive fails — caller still gets a HostCapacity."""
    def boom(*args, **kwargs):
        raise FileNotFoundError("nope")

    result = probe_host(
        run=boom,
        read_file=lambda _: None,
        env={},
        platform_name="Darwin",
        load_avg_reader=lambda: None,
    )
    assert result.ram_gb is None
    assert result.cpu_cores is None
    assert result.plan_tier is None
    assert result.load_avg_5min is None


# ---------------------------------------------------------------------------
# recommend() — translation rules
# ---------------------------------------------------------------------------


def test_recommend_max20x_with_48gb_recommends_full_envelope():
    capacity = HostCapacity(
        ram_gb=48.0, cpu_cores=10, plan_tier="max_20x", platform_name="Darwin"
    )
    rec = recommend(capacity)
    # 48 GB / (0.5 * 2) = 48 ram envelope; plan tier max_20x = 10;
    # ceiling = 20 (raised from 10 once parallel dispatch landed).
    # min(10, 48, 20) = 10 — plan-tier-bound on a roomy 48 GB box.
    assert rec.max_concurrent == PLAN_TIER_CONCURRENCY["max_20x"]
    assert rec.daily_hard_cap_usd == PLAN_TIER_HARD_CAPS["max_20x"]


def test_recommend_standard_plan_caps_concurrency_at_3():
    capacity = HostCapacity(
        ram_gb=64.0, cpu_cores=16, plan_tier="standard", platform_name="Darwin"
    )
    rec = recommend(capacity)
    assert rec.max_concurrent == 3  # plan-tier dominates over RAM
    assert rec.daily_hard_cap_usd == PLAN_TIER_HARD_CAPS["standard"]


def test_recommend_low_ram_caps_concurrency():
    """4 GB host → can't run many workers no matter what plan."""
    capacity = HostCapacity(
        ram_gb=4.0, cpu_cores=4, plan_tier="max_20x", platform_name="Linux"
    )
    rec = recommend(capacity)
    # 4 / (0.5 * 2) = 4 ram envelope; min(plan_tier=10, 4) = 4
    # The test name still holds: the box is bounded by RAM, not plan.
    assert rec.max_concurrent == 4


def test_recommend_unknown_plan_falls_back_to_standard():
    capacity = HostCapacity(
        ram_gb=32.0, cpu_cores=8, plan_tier=None, platform_name="Linux"
    )
    rec = recommend(capacity)
    assert rec.max_concurrent == PLAN_TIER_CONCURRENCY["standard"]
    assert rec.daily_hard_cap_usd == DEFAULT_DAILY_HARD_CAP_USD


def test_recommend_unknown_capacity_returns_safe_defaults():
    """All Nones → recommendation still bounded above zero."""
    capacity = HostCapacity(
        ram_gb=None, cpu_cores=None, plan_tier=None, platform_name="Windows"
    )
    rec = recommend(capacity)
    assert rec.max_concurrent >= 1
    assert rec.max_concurrent <= 5  # safe ceiling without RAM info
    assert rec.daily_hard_cap_usd == DEFAULT_DAILY_HARD_CAP_USD


def test_recommend_soft_warn_never_exceeds_hard_cap():
    """With a tiny hard cap the soft-warn must shrink proportionally."""
    capacity = HostCapacity(
        ram_gb=8.0, cpu_cores=2, plan_tier=None, platform_name="Linux"
    )
    # Force a tiny hard cap for the recommend path by monkeypatching
    # — this is a tail-edge case but matters if a fork narrows the
    # default cap.
    rec = recommend(capacity)
    assert rec.daily_soft_warn_usd <= rec.daily_hard_cap_usd


def test_recommend_rationale_includes_detected_values():
    capacity = HostCapacity(
        ram_gb=48.0, cpu_cores=10, plan_tier="max_20x", platform_name="Darwin"
    )
    rec = recommend(capacity)
    assert "48.0 GB" in rec.rationale
    assert "10" in rec.rationale  # cpu cores
    assert "max_20x" in rec.rationale
    assert "max_concurrent" in rec.rationale


def test_recommend_rationale_explains_unknown_plan():
    capacity = HostCapacity(
        ram_gb=16.0, cpu_cores=8, plan_tier=None, platform_name="Linux"
    )
    rec = recommend(capacity)
    assert "ANTHROPIC_PLAN" in rec.rationale  # tells operator how to refine


# ---------------------------------------------------------------------------
# probe_and_recommend
# ---------------------------------------------------------------------------


def test_probe_and_recommend_returns_pair():
    """End-to-end: probe + recommend in one call."""
    def fake_run(cmd: list[str]) -> str:
        if "memsize" in cmd[-1]:
            return "8589934592\n"  # 8 GB
        if "ncpu" in cmd[-1]:
            return "4\n"
        raise AssertionError(cmd)

    capacity, rec = probe_and_recommend(
        run=fake_run,
        read_file=lambda _: None,
        env={"ANTHROPIC_PLAN": "max_5x"},
        platform_name="Darwin",
        load_avg_reader=lambda: _IDLE_LOAD_AVG,  # inject idle load so CPU envelope is 4
    )
    assert capacity.ram_gb is not None
    assert 7.5 < capacity.ram_gb < 8.5
    assert capacity.cpu_cores == 4
    assert capacity.plan_tier == "max_5x"
    assert capacity.load_avg_5min == _IDLE_LOAD_AVG
    # 8 GB → ram_envelope = 8 // (0.5 * 2) = 8; cpu_envelope = 4 - 0 = 4;
    # max_5x plan = 5; min(5, 8, 4, 20) = 4
    assert rec.max_concurrent == 4


# ---------------------------------------------------------------------------
# _read_load_avg_posix — unit tests for the new load-avg primitive
# ---------------------------------------------------------------------------


def test_read_load_avg_posix_returns_float(monkeypatch):
    """Monkeypatch os.getloadavg to return a known triple."""
    import os
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 2.5, 3.0))
    result = _read_load_avg_posix()
    assert result == pytest.approx(2.5)


def test_read_load_avg_posix_returns_none_on_attribute_error(monkeypatch):
    """Windows doesn't have os.getloadavg — probe must degrade gracefully."""
    import os
    monkeypatch.delattr(os, "getloadavg", raising=False)
    result = _read_load_avg_posix()
    assert result is None


def test_read_load_avg_posix_returns_none_on_os_error(monkeypatch):
    """Container environments may raise OSError from getloadavg."""
    import os

    def boom():
        raise OSError("no /proc/loadavg")

    monkeypatch.setattr(os, "getloadavg", boom)
    result = _read_load_avg_posix()
    assert result is None


# ---------------------------------------------------------------------------
# HostCapacity — load_avg_5min field
# ---------------------------------------------------------------------------


def test_host_capacity_load_avg_defaults_to_none():
    """The new field is optional and defaults to None (backward compat)."""
    cap = HostCapacity(
        ram_gb=16.0, cpu_cores=8, plan_tier=None, platform_name="Linux"
    )
    assert cap.load_avg_5min is None


def test_host_capacity_accepts_load_avg():
    cap = HostCapacity(
        ram_gb=48.0, cpu_cores=10, plan_tier="max_20x",
        platform_name="Darwin", load_avg_5min=3.5,
    )
    assert cap.load_avg_5min == 3.5


# ---------------------------------------------------------------------------
# recommend() — CPU envelope shapes the result
# ---------------------------------------------------------------------------


def test_recommend_cpu_envelope_constrains_on_busy_host():
    """Busy host: load avg 13 on a 10-core machine → cpu_envelope = 1.

    This is the exact scenario from T2-43: Mac mini at load 13
    with 7 workers recommended by RAM alone → load spiked to 36+.
    CPU envelope must clamp to 1 so no extra workers are spawned.
    """
    capacity = HostCapacity(
        ram_gb=48.0,
        cpu_cores=10,
        plan_tier="max_20x",
        platform_name="Darwin",
        load_avg_5min=13.0,
    )
    rec = recommend(capacity)
    # cpu_envelope = max(1, 10 - 13) = max(1, -3) = 1
    assert rec.max_concurrent == 1


def test_recommend_cpu_envelope_idle_host_not_constrained():
    """Idle host: load avg 0.5 on a 10-core machine → cpu_envelope = 9.

    RAM and plan tier still dominate on a host with headroom.
    """
    capacity = HostCapacity(
        ram_gb=48.0,
        cpu_cores=10,
        plan_tier="max_20x",
        platform_name="Darwin",
        load_avg_5min=0.5,
    )
    rec = recommend(capacity)
    # cpu_envelope = max(1, 10 - 0) = 9; plan_tier = 10; ram_envelope = 48
    # min(9, 10, 48, 20) = 9
    assert rec.max_concurrent == 9


def test_recommend_cpu_envelope_partially_busy():
    """Partial load: load avg 5 on a 10-core machine → cpu_envelope = 5."""
    capacity = HostCapacity(
        ram_gb=48.0,
        cpu_cores=10,
        plan_tier="max_20x",
        platform_name="Darwin",
        load_avg_5min=5.0,
    )
    rec = recommend(capacity)
    # cpu_envelope = max(1, 10 - 5) = 5; plan_tier = 10; ram_envelope = 48
    # min(5, 10, 48, 20) = 5
    assert rec.max_concurrent == 5


def test_recommend_missing_load_avg_falls_back_to_ram_plan_ceiling():
    """No load avg → CPU envelope doesn't restrict; other signals dominate."""
    capacity = HostCapacity(
        ram_gb=48.0,
        cpu_cores=10,
        plan_tier="max_20x",
        platform_name="Darwin",
        load_avg_5min=None,
    )
    rec = recommend(capacity)
    # Without load_avg, cpu_envelope = ceiling = 20; plan_tier = 10; ram = 48
    # min(10, 48, 20, 20) = 10
    assert rec.max_concurrent == PLAN_TIER_CONCURRENCY["max_20x"]


def test_recommend_rationale_includes_load_avg():
    """Rationale surface shows load average and cpu envelope for the operator."""
    capacity = HostCapacity(
        ram_gb=48.0,
        cpu_cores=10,
        plan_tier="max_20x",
        platform_name="Darwin",
        load_avg_5min=13.0,
    )
    rec = recommend(capacity)
    assert "load avg" in rec.rationale
    assert "13.00" in rec.rationale
    assert "CPU envelope" in rec.rationale


def test_recommend_rationale_no_load_avg_no_cpu_envelope_line():
    """When load avg is unknown, the rationale must not mention CPU envelope."""
    capacity = HostCapacity(
        ram_gb=48.0,
        cpu_cores=10,
        plan_tier=None,
        platform_name="Darwin",
        load_avg_5min=None,
    )
    rec = recommend(capacity)
    assert "load avg" not in rec.rationale
    assert "CPU envelope" not in rec.rationale


# ---------------------------------------------------------------------------
# probe_load_avg / probe_cpu_count — public live-probe helpers
# ---------------------------------------------------------------------------


def test_probe_load_avg_returns_number_or_none():
    """Live probe returns a non-negative float or None — never raises."""
    result = probe_load_avg()
    if result is not None:
        assert result >= 0.0


def test_probe_cpu_count_returns_positive_or_none():
    """Live probe returns a positive int or None — never raises."""
    result = probe_cpu_count()
    if result is not None:
        assert result >= 1


# ---------------------------------------------------------------------------
# recommend_concurrency() — the CPU+RAM-aware live helper
# ---------------------------------------------------------------------------


def test_recommend_concurrency_busy_host_clamps_to_1():
    """Mac mini T2-43 scenario: busy host must return 1, not 7."""
    result = recommend_concurrency(
        free_gb=18.0,
        cpu_count=10,
        load_avg_5min=13.0,
    )
    # cpu_envelope = max(1, 10 - 13) = 1 — dominates
    assert result == 1


def test_recommend_concurrency_idle_host_ram_dominates():
    """Idle host, low RAM: RAM is the binding constraint."""
    result = recommend_concurrency(
        free_gb=9.0,   # usable = 9 - 8 = 1 GB → 2 slots
        cpu_count=10,
        load_avg_5min=0.5,
        worker_gb=0.5,
        reserved_gb=8.0,
    )
    # ram_envelope = max(1, int(1.0 / 0.5)) = 2
    # cpu_envelope = max(1, int(10 - 0.5)) = 9
    # min(2, 9, 20) = 2
    assert result == 2


def test_recommend_concurrency_partial_load_cpu_dominates():
    """Partial load on a RAM-rich host: CPU is binding."""
    result = recommend_concurrency(
        free_gb=30.0,
        cpu_count=8,
        load_avg_5min=5.5,
        worker_gb=0.5,
        reserved_gb=8.0,
    )
    # ram_envelope = int((30 - 8) / 0.5) = 44 (capped by ceiling=20)
    # cpu_envelope = max(1, int(8 - 5.5)) = 2
    # min(44, 2, 20) = 2
    assert result == 2


def test_recommend_concurrency_no_cpu_data_ram_only():
    """Without CPU data, RAM and ceiling dominate — no artificial clamping."""
    result = recommend_concurrency(
        free_gb=9.0,
        cpu_count=None,
        load_avg_5min=None,
        worker_gb=0.5,
        reserved_gb=8.0,
        ceiling=20,
    )
    # ram_envelope = max(1, int(1.0 / 0.5)) = 2; cpu_envelope = ceiling = 20
    assert result == 2


def test_recommend_concurrency_probe_fail_returns_1():
    """All probes fail: free_gb=None, cpu=None, load=None → 1 (safe default)."""
    result = recommend_concurrency(
        free_gb=None,
        cpu_count=None,
        load_avg_5min=None,
    )
    assert result == 1


def test_recommend_concurrency_ceiling_honoured():
    """Hard ceiling is always respected, even with abundant RAM and low load."""
    result = recommend_concurrency(
        free_gb=200.0,
        cpu_count=64,
        load_avg_5min=0.1,
        ceiling=5,
    )
    assert result <= 5


def test_recommend_concurrency_floor_is_1():
    """Result is always at least 1, even on a saturated host."""
    result = recommend_concurrency(
        free_gb=8.0,
        cpu_count=4,
        load_avg_5min=100.0,  # wildly saturated
        reserved_gb=8.0,
    )
    assert result == 1


def test_recommend_concurrency_less_than_ram_alone_on_busy_host():
    """Core acceptance criterion: CPU-aware result < RAM-only result on busy host."""
    free_gb = 18.0
    cpu_count = 10
    load_avg_busy = 13.0

    cpu_aware = recommend_concurrency(
        free_gb=free_gb,
        cpu_count=cpu_count,
        load_avg_5min=load_avg_busy,
    )
    ram_only = recommend_ram_concurrency(free_gb=free_gb)

    assert cpu_aware < ram_only, (
        f"CPU-aware ({cpu_aware}) should be < RAM-only ({ram_only}) on a busy host"
    )


# ---------------------------------------------------------------------------
# recommend_ram_concurrency() — backward-compat shim
# ---------------------------------------------------------------------------


def test_recommend_ram_concurrency_backward_compat():
    """recommend_ram_concurrency must still work and ignore CPU load."""
    result = recommend_ram_concurrency(
        free_gb=9.0,
        worker_gb=0.5,
        reserved_gb=8.0,
    )
    # ram_envelope = max(1, int(1.0 / 0.5)) = 2; no CPU applied
    assert result == 2
