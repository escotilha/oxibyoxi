"""SelfAdapter — oxi operating on its own repo.

Conservative by design:
  - auto_merge=False (Pierre reviews every PR)
  - daily_hard_cap=$20 (runaway loop halts)
  - max_concurrent=1 (no fan-out)
  - plan_tier="20x" (Max plan, see memory:tech-insight-psos-plan-tier-20x)

Forks should NOT copy this adapter. Fork authors write their own adapter
against the reference pattern.
"""

from __future__ import annotations

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
        return BudgetCaps(
            daily_soft_warn=5.0,
            daily_hard_cap=20.0,
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
        return (
            DispatchHost(
                name="local",
                ssh_alias=None,
                max_concurrent=1,
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
            auto_merge=False,
            tier_zero_absorb=True,
        )
