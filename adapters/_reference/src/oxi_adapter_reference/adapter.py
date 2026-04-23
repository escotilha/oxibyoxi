"""Reference adapter implementation — drives the `sample-project/` fixture.

All values here are deliberately small. Budget caps are low enough that
accidentally pointing this adapter at a real production repo would
halt dispatch after a handful of tasks. Dispatch host is localhost
only, concurrency 1 — no remote hosts, no fan-out.

Every field is a real value. No NotImplementedError, no stubs.
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
class ReferenceAdapter:
    """Adapter implementation for the sample-project fixture.

    `repo_root` is the directory containing `sample-project/` — typically
    the directory where the fixture was cloned. The adapter computes
    all derived paths relative to it.
    """

    repo_root: Path

    # ---- Identity ----

    def naming(self) -> NamingConfig:
        return NamingConfig(
            instance_name="reference",
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
            daily_soft_warn=1.0,
            daily_hard_cap=5.0,
            per_task_opus=0.50,
            per_task_sonnet=0.20,
        )

    # ---- Target repo ----

    def github_repo(self) -> str:
        # Placeholder slug — sample-project has no real remote. Prompts
        # render it so worker sessions know what repo they'd be pushing
        # to, but the fixture is driven locally.
        return "owner/sample-project"

    def roadmap_location(self) -> str:
        return "roadmap.md"

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
        # Reference adapter has no staging/production split. Forks that
        # need one return a configured PromoteRecipe here.
        return None

    # ---- Plan tier + policy ----

    def plan_tier(self) -> str:
        return "standard"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy(
            skill_weights={},
            auto_merge=False,
            tier_zero_absorb=True,
        )
