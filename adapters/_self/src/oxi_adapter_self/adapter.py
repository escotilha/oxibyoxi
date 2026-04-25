"""SelfAdapter — oxi operating on its own repo.

Configured for serious dogfood throughput:
  - auto_merge=True (engine PRs merge after critic + CI green)
  - daily_hard_cap=$100 (Pierre's directive, hard-coded for dogfood)
  - max_concurrent: probed from free RAM at tick time (1..20 slots)
  - plan_tier="20x" (Max plan)

Forks should NOT copy this adapter. Fork authors write their own adapter
against the reference pattern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    PromoteRecipe,
)
from oxi_core.compute_probe import recommend_ram_concurrency

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelfAdapter:
    """Adapter pointing oxi at the local oxi repo.

    `repo_root` is the working copy of `escotilha/oxi`. The adapter
    derives every path from it; nothing else needs to be configured.
    """

    repo_root: Path

    # ---- Identity ----

    def naming(self) -> NamingConfig:
        return NamingConfig(
            instance_name="oxi-dogfood",
            branch_prefixes=("feat/", "fix/", "chore/"),
        )

    # ---- Paths ----

    def paths(self) -> PathsConfig:
        return PathsConfig(
            repo_root=str(self.repo_root),
            oxi_dir=".oxi",
            db_path=str(self.repo_root / ".oxi" / "oxi.db"),
            brief_path=str(self.repo_root / ".oxi" / "brief.md"),
            dashboard_path=str(self.repo_root / ".oxi" / "dashboard.html"),
            release_lock_path=str(self.repo_root / ".oxi" / "RELEASE_LOCK"),
            daily_recap_path=str(self.repo_root / ".oxi" / "daily-recap.md"),
        )

    # ---- Budget ----

    def budget(self) -> BudgetCaps:
        # Hard-coded $100/day per Pierre's directive 2026-04-25 once
        # parallel dispatch + auto_merge + 22-item plan ingest landed.
        # The soft-warn at $25 lets the dashboard flag pressure before
        # the hard-stop kicks in.
        return BudgetCaps(
            daily_soft_warn=25.0,
            daily_hard_cap=100.0,
            per_task_opus=2.0,
            per_task_sonnet=0.50,
        )

    # ---- Target repo ----

    def github_repo(self) -> str:
        return "escotilha/oxi"

    def roadmap_location(self) -> str:
        return "docs/roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/", "fix/", "chore/")

    # ---- Dispatch ----

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        # Concurrency is *probed* at every call, not hardcoded.
        # `recommend_ram_concurrency` reads live free RAM (vm_stat on
        # macOS, /proc/meminfo MemAvailable on Linux), reserves
        # RAM_RESERVED_GB (default 8 GB) for OS + dashboard + IDE,
        # and divides what's left by WORKER_MEM_GB (default 1.5 GB)
        # to get a slot count. Capped at HARDWARE_CONCURRENCY_CEILING
        # (default 20).
        #
        # If the probe fails (sandboxed env, unrecognized platform),
        # falls back to 1 — never silently runs more than the
        # operator's machine can comfortably handle.
        #
        # Operator override: set OXI_MAX_CONCURRENT in the engine's
        # environment to force a specific value, bypassing the probe.
        import os
        forced = os.environ.get("OXI_MAX_CONCURRENT")
        if forced:
            try:
                max_concurrent = max(1, int(forced))
                logger.info(
                    "self_adapter.concurrency.forced",
                    extra={"max_concurrent": max_concurrent},
                )
            except ValueError:
                max_concurrent = recommend_ram_concurrency()
        else:
            max_concurrent = recommend_ram_concurrency()
            logger.info(
                "self_adapter.concurrency.probed",
                extra={"max_concurrent": max_concurrent},
            )

        return (
            DispatchHost(
                name="local",
                ssh_alias=None,
                max_concurrent=max_concurrent,
                worktree_root=str(self.repo_root / ".oxi" / "worktrees"),
            ),
        )

    def promote_recipe(self) -> PromoteRecipe | None:
        # Dogfood loop has no staging/production split — PyPI release is
        # manual via scripts/release.sh. Kept None intentionally.
        return None

    # ---- Plan tier + policy ----

    def plan_tier(self) -> str:
        return "20x"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy(
            skill_weights={},
            # Auto-merge engine PRs after the critic passes. Flipped
            # 2026-04-25 per Pierre's "auto-merge what we originate"
            # directive after the engine had a clean PR-merge track
            # record across 90+ dogfood dispatches.
            auto_merge=True,
            tier_zero_absorb=True,
        )
