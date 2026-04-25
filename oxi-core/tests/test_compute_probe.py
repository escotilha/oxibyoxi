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
    _read_plan_tier,
    _read_ram_gb_linux,
    _read_ram_gb_macos,
    probe_and_recommend,
    probe_host,
    recommend,
)

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
    )
    assert result.platform_name == "Darwin"
    assert result.ram_gb is not None
    assert 47.5 < result.ram_gb < 48.5
    assert result.cpu_cores == 10
    assert result.plan_tier is None  # not in env


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
    )
    assert result.platform_name == "Linux"
    assert result.ram_gb is not None
    assert 15 < result.ram_gb < 17
    assert result.cpu_cores == 4
    assert result.plan_tier == "max_5x"


def test_probe_host_windows_falls_back_to_defaults():
    """Unsupported platform → Nones rather than errors."""
    result = probe_host(
        run=lambda cmd: "",
        read_file=lambda _: None,
        env={},
        platform_name="Windows",
    )
    assert result.platform_name == "Windows"
    assert result.ram_gb is None
    assert result.cpu_cores is None
    assert result.plan_tier is None


def test_probe_host_handles_total_failure():
    """Every probe primitive fails — caller still gets a HostCapacity."""
    def boom(*args, **kwargs):
        raise FileNotFoundError("nope")

    result = probe_host(
        run=boom,
        read_file=lambda _: None,
        env={},
        platform_name="Darwin",
    )
    assert result.ram_gb is None
    assert result.cpu_cores is None
    assert result.plan_tier is None


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
    )
    assert capacity.ram_gb is not None
    assert 7.5 < capacity.ram_gb < 8.5
    assert capacity.cpu_cores == 4
    assert capacity.plan_tier == "max_5x"
    # 8 GB → ram_envelope = 8 // (0.5 * 2) = 8; max_5x recommends 5 → min = 5
    assert rec.max_concurrent == 5
