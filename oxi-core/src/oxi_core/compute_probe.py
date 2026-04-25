"""Compute-aware onboarding probe.

Used by `oxi init` to detect host capacity and recommend sensible
default values for `max_concurrent` and `daily_hard_cap`. Operators
who don't probe still get the safe default (1, $5); operators who
probe get a recommendation calibrated to their actual machine.

Design choices
--------------

- **Read-only.** The probe never writes a file or modifies state. It
  returns a `ComputeRecommendation` dataclass; the wizard renders it
  as default values in the prompts.
- **Best-effort.** If a probe step fails (no `sysctl`, no
  `/proc/cpuinfo`, no `ANTHROPIC_API_KEY`, etc.), the probe degrades
  to safe defaults instead of erroring. The CLI surface treats a
  "probe failed" outcome the same as "probe found a tiny machine."
- **Cross-platform.** macOS uses `sysctl`; Linux reads `/proc/meminfo`
  and `/proc/cpuinfo`; Windows is not yet supported (returns
  defaults). Tests cover both macOS and Linux paths via the
  `_read_*` helpers being injectable.
- **No network.** The probe doesn't call the Anthropic API. Plan tier
  is read from environment variables the operator sets manually
  (`ANTHROPIC_PLAN`) or inferred from `ANTHROPIC_API_KEY` length as a
  weak heuristic. If the operator passes the wrong tier, they get
  bigger or smaller recommended numbers, not a broken machine.
- **Defensive about RAM.** Workers consume ~1.5 GB resident under
  normal load; we cap recommended concurrency at
  ``ram_gb // 4`` to leave headroom for the supervisor, dashboard,
  IDE, browser. A 48 GB Mac mini → 12 max; we then floor at the
  hardware envelope of 10 (validated empirically — see PR #66).

Plan-tier hints
---------------

The Anthropic plan tier governs how aggressively concurrency can
scale. The probe's mapping:

- **standard / unknown** → 3 concurrent (rate-limit safe)
- **max_5x** → 5 concurrent
- **max_20x** → 10 concurrent (the hardware envelope from PR #66)

These can always be overridden in the adapter. The probe is a
*recommendation*, not a constraint.
"""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Tunables — module-level so tests + forks can monkeypatch
# ---------------------------------------------------------------------------

#: Approximate resident memory of a single claude-code worker, GB.
#:
#: Lowered 1.5 → 0.5 on 2026-04-25 PM after measuring actual RSS of
#: 5 concurrent workers on a Mac mini (range 295-328 MB, well under
#: 0.5 GB). The old 1.5 GB was a Day-1 safety estimate; the line
#: in `recommend()` already adds another 2x headroom (see
#: `WORKER_MEM_GB * 2` divisor below), so this still leaves a
#: comfortable 3-4x cushion vs reality.
#:
#: Net effect: a 48 GB Mac mini with 18 GB free RAM (typical with
#: dashboard + a few other apps open) goes from probed concurrency
#: of 5 → ~16 (capped at HARDWARE_CONCURRENCY_CEILING=20).
WORKER_MEM_GB: float = 0.5

#: Ceiling for concurrency derived from RAM. Bumped from 10 (the
#: original validated envelope) to 20 once the dogfood loop proved
#: stable with parallel dispatch. The actual cap at runtime is
#: min(ceiling, free_ram_gb / WORKER_MEM_GB) so we never overcommit
#: a machine with less RAM than the ceiling implies.
HARDWARE_CONCURRENCY_CEILING: int = 20

#: Reserved RAM (GB) we never let workers consume. Covers OS,
#: the engine itself, dashboard, sshd, and any user processes the
#: operator is running on the same host. 8 GB is generous enough
#: that a 16 GB machine still gets ~5 concurrent slots without
#: paging.
RAM_RESERVED_GB: float = 8.0

#: Default budget caps when the probe can't determine plan tier.
DEFAULT_DAILY_SOFT_WARN_USD: float = 5.0
DEFAULT_DAILY_HARD_CAP_USD: float = 20.0
DEFAULT_PER_TASK_OPUS_USD: float = 2.0
DEFAULT_PER_TASK_SONNET_USD: float = 0.50


# Plan-tier-specific budget recommendations (USD/day hard cap).
PLAN_TIER_HARD_CAPS: dict[str, float] = {
    "standard": 10.0,
    "max_5x": 30.0,
    "max_20x": 100.0,
}


# Plan-tier-specific concurrency recommendations.
PLAN_TIER_CONCURRENCY: dict[str, int] = {
    "standard": 3,
    "max_5x": 5,
    "max_20x": 10,
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostCapacity:
    """What the probe physically detected on the host.

    None values mean the probe couldn't read that field — caller
    should fall back to the default for that field, not propagate
    the unknown.
    """

    ram_gb: float | None
    cpu_cores: int | None
    plan_tier: str | None  # "standard", "max_5x", "max_20x", or None
    platform_name: str  # "Darwin", "Linux", "Windows", "Unknown"


@dataclass(frozen=True)
class ComputeRecommendation:
    """What the wizard should suggest as defaults.

    Every field has a sane fallback even if the probe couldn't
    determine the host. The wizard is free to override based on
    operator answers.
    """

    max_concurrent: int
    daily_soft_warn_usd: float
    daily_hard_cap_usd: float
    per_task_opus_usd: float
    per_task_sonnet_usd: float
    rationale: str  # human-readable explanation for the wizard prompt


# ---------------------------------------------------------------------------
# Probe primitives — injectable for tests
# ---------------------------------------------------------------------------


def _read_ram_gb_macos(_run: Callable[[list[str]], str]) -> float | None:
    """Use `sysctl hw.memsize`. Returns GB or None on failure."""
    try:
        out = _run(["sysctl", "-n", "hw.memsize"])
        return int(out.strip()) / (1024**3)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None


def _read_ram_gb_linux(meminfo_text: str | None) -> float | None:
    """Parse `/proc/meminfo` `MemTotal:` line. Returns GB or None."""
    if meminfo_text is None:
        return None
    for line in meminfo_text.splitlines():
        if line.startswith("MemTotal:"):
            # Format: "MemTotal:        49283456 kB"
            parts = line.split()
            if len(parts) >= 2:
                try:
                    kb = int(parts[1])
                    return kb / (1024**2)  # kB → GB
                except ValueError:
                    return None
    return None


def _read_cpu_cores_macos(_run: Callable[[list[str]], str]) -> int | None:
    """Use `sysctl hw.ncpu`. Returns core count or None."""
    try:
        out = _run(["sysctl", "-n", "hw.ncpu"])
        return int(out.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None


def _read_cpu_cores_linux(cpuinfo_text: str | None) -> int | None:
    """Count `processor :` lines in `/proc/cpuinfo`. Returns count or None."""
    if cpuinfo_text is None:
        return None
    n = sum(1 for line in cpuinfo_text.splitlines() if line.startswith("processor"))
    return n if n > 0 else None


def _read_plan_tier(env: dict[str, str]) -> str | None:
    """Read plan tier from env. Operators can set ANTHROPIC_PLAN explicitly.

    Returns one of: "standard", "max_5x", "max_20x", None.
    """
    raw = env.get("ANTHROPIC_PLAN", "").strip().lower()
    if raw in ("standard", "max_5x", "max_20x"):
        return raw
    return None


# ---------------------------------------------------------------------------
# Default subprocess runner — separated for tests
# ---------------------------------------------------------------------------


def _default_run(cmd: list[str]) -> str:
    """Run a command and return its stdout. Raises on nonzero exit."""
    proc = subprocess.run(  # noqa: S603 — argv-form, env-managed
        cmd,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return proc.stdout


def _default_read_file(path: str) -> str | None:
    """Read a file, returning None if it doesn't exist or isn't readable."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def probe_host(
    *,
    run: Callable[[list[str]], str] = _default_run,
    read_file: Callable[[str], str | None] = _default_read_file,
    env: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> HostCapacity:
    """Detect host capacity. Best-effort; returns Nones on failure.

    Cross-platform: dispatches to macOS or Linux primitives based on
    `platform.system()`. Windows currently returns all-None (probe
    "failed"); the recommendation falls back to safe defaults.
    """
    e = env if env is not None else os.environ
    p = platform_name if platform_name is not None else platform.system()

    ram_gb: float | None = None
    cpu_cores: int | None = None

    if p == "Darwin":
        ram_gb = _read_ram_gb_macos(run)
        cpu_cores = _read_cpu_cores_macos(run)
    elif p == "Linux":
        ram_gb = _read_ram_gb_linux(read_file("/proc/meminfo"))
        cpu_cores = _read_cpu_cores_linux(read_file("/proc/cpuinfo"))
    # Windows / unknown → leave as None

    plan_tier = _read_plan_tier(dict(e))

    return HostCapacity(
        ram_gb=ram_gb,
        cpu_cores=cpu_cores,
        plan_tier=plan_tier,
        platform_name=p,
    )


def recommend(capacity: HostCapacity) -> ComputeRecommendation:
    """Map host capacity → wizard defaults.

    Logic:

    1. Concurrency: min of plan-tier recommendation and RAM-based
       envelope (`ram_gb // (worker_mem_gb * 2)`, leaving 50% headroom).
       Floor at 1; ceil at HARDWARE_CONCURRENCY_CEILING.
    2. Daily hard cap: plan-tier-specific value if known; default
       otherwise.
    3. Per-task caps: stay at module defaults regardless of plan
       (operator can bump them in the adapter; runaway protection is
       what matters).
    """
    plan_tier_concurrency = PLAN_TIER_CONCURRENCY.get(
        capacity.plan_tier or "", PLAN_TIER_CONCURRENCY["standard"]
    )

    if capacity.ram_gb is not None:
        # Each concurrent worker needs ~WORKER_MEM_GB; keep 50% headroom.
        ram_envelope = max(1, int(capacity.ram_gb // (WORKER_MEM_GB * 2)))
    else:
        ram_envelope = HARDWARE_CONCURRENCY_CEILING  # let plan tier dominate

    max_concurrent = min(
        plan_tier_concurrency,
        ram_envelope,
        HARDWARE_CONCURRENCY_CEILING,
    )
    max_concurrent = max(1, max_concurrent)

    hard_cap = PLAN_TIER_HARD_CAPS.get(
        capacity.plan_tier or "", DEFAULT_DAILY_HARD_CAP_USD
    )
    soft_warn = min(DEFAULT_DAILY_SOFT_WARN_USD, hard_cap / 2)

    rationale = _build_rationale(capacity, max_concurrent, hard_cap)

    return ComputeRecommendation(
        max_concurrent=max_concurrent,
        daily_soft_warn_usd=soft_warn,
        daily_hard_cap_usd=hard_cap,
        per_task_opus_usd=DEFAULT_PER_TASK_OPUS_USD,
        per_task_sonnet_usd=DEFAULT_PER_TASK_SONNET_USD,
        rationale=rationale,
    )


def _build_rationale(
    capacity: HostCapacity,
    max_concurrent: int,
    hard_cap: float,
) -> str:
    """Human-readable explanation. The wizard prints this above the prompt."""
    lines: list[str] = []
    if capacity.ram_gb is not None:
        lines.append(f"  RAM         : {capacity.ram_gb:.1f} GB detected")
    if capacity.cpu_cores is not None:
        lines.append(f"  CPU cores   : {capacity.cpu_cores}")
    if capacity.plan_tier is not None:
        lines.append(f"  plan tier   : {capacity.plan_tier} (from ANTHROPIC_PLAN)")
    else:
        lines.append("  plan tier   : unknown (set ANTHROPIC_PLAN to refine)")
    lines.append(f"  → recommended max_concurrent = {max_concurrent}")
    lines.append(f"  → recommended daily_hard_cap = ${hard_cap:.0f}/day")
    return "\n".join(lines)


def probe_and_recommend(
    *,
    run: Callable[[list[str]], str] = _default_run,
    read_file: Callable[[str], str | None] = _default_read_file,
    env: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> tuple[HostCapacity, ComputeRecommendation]:
    """Convenience: probe + recommend in one call. Used by `oxi init`."""
    capacity = probe_host(
        run=run, read_file=read_file, env=env, platform_name=platform_name
    )
    return capacity, recommend(capacity)


__all__ = [
    "ComputeRecommendation",
    "DEFAULT_DAILY_HARD_CAP_USD",
    "DEFAULT_DAILY_SOFT_WARN_USD",
    "DEFAULT_PER_TASK_OPUS_USD",
    "DEFAULT_PER_TASK_SONNET_USD",
    "HARDWARE_CONCURRENCY_CEILING",
    "HostCapacity",
    "PLAN_TIER_CONCURRENCY",
    "PLAN_TIER_HARD_CAPS",
    "WORKER_MEM_GB",
    "probe_and_recommend",
    "probe_host",
    "recommend",
]


# ---------------------------------------------------------------------------
# Live free-RAM probe — used by SelfAdapter.dispatch_hosts() to size
# concurrency at tick time, not at install time
# ---------------------------------------------------------------------------


def _free_ram_gb_macos() -> float | None:
    """Read free + inactive pages from `vm_stat`. Returns GB or None.

    macOS `vm_stat` reports pages of 4096 bytes. "Free" + "Inactive"
    is the closest to "memory the kernel can hand out without paging."
    """
    try:
        out = subprocess.check_output(
            ["vm_stat"], text=True, stderr=subprocess.DEVNULL
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    page_size = 4096
    free_pages = 0
    inactive_pages = 0
    for line in out.splitlines():
        if "page size of" in line:
            # Format: "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
            try:
                page_size = int(line.split("page size of")[1].split()[0])
            except (IndexError, ValueError):
                pass
        if line.startswith("Pages free:"):
            try:
                free_pages = int(line.split(":")[1].strip().rstrip("."))
            except ValueError:
                return None
        elif line.startswith("Pages inactive:"):
            try:
                inactive_pages = int(line.split(":")[1].strip().rstrip("."))
            except ValueError:
                return None
    bytes_free = (free_pages + inactive_pages) * page_size
    return bytes_free / (1024**3) if bytes_free > 0 else None


def _free_ram_gb_linux() -> float | None:
    """Read MemAvailable from /proc/meminfo. Returns GB or None.

    MemAvailable is what the kernel can give to a new process
    without swapping — exactly what we want to budget against.
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            return int(parts[1]) / (1024**2)  # kB → GB
                        except ValueError:
                            return None
    except OSError:
        return None
    return None


def probe_free_ram_gb() -> float | None:
    """Best-effort live free-RAM probe.

    Returns None on platforms we don't recognize or when probing
    fails (sandboxed env, missing binary, etc). Callers should fall
    back to a static cap when None.
    """
    import platform
    name = platform.system().lower()
    if name == "darwin":
        return _free_ram_gb_macos()
    if name == "linux":
        return _free_ram_gb_linux()
    return None


def recommend_ram_concurrency(
    *,
    ceiling: int = HARDWARE_CONCURRENCY_CEILING,
    reserved_gb: float = RAM_RESERVED_GB,
    worker_gb: float = WORKER_MEM_GB,
    free_gb: float | None = None,
) -> int:
    """Concurrency that fits in available RAM with headroom.

    Formula: max(1, min(ceiling, floor((free_gb - reserved) / worker_gb)))

    When ``free_gb`` is None and the live probe also fails, returns 1
    — the safest default. The caller should log the fallback so
    operators can see why concurrency is conservative.
    """
    if free_gb is None:
        free_gb = probe_free_ram_gb()
    if free_gb is None:
        return 1
    usable = max(0.0, free_gb - reserved_gb)
    headroom_slots = int(usable / worker_gb)
    return max(1, min(ceiling, headroom_slots))
