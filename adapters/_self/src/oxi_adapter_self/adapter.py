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
import os
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
from oxi_core.compute_probe import recommend_concurrency

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gateway configuration dataclass (T2-36)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferenceGatewayConfig:
    """Connection details for the Mac Mini LiteLLM gateway.

    Attributes:
        base_url: LiteLLM proxy root URL, e.g.
            ``"http://mac-mini.tail1234.ts.net:4000"``.
            Sourced from ``OXI_GATEWAY_BASE_URL``; empty string when unset
            (gateway is unconfigured).
        virtual_keys: mapping of role name → virtual API key secret
            (the ``sk-...`` value returned by LiteLLM's key/generate
            endpoint).  Keys present only when the corresponding env var
            is set; callers must treat absence as "key not provisioned".
    """

    base_url: str
    virtual_keys: dict[str, str]


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
            instance_name="oxibyoxi",
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
        #
        # per_task_opus raised 2.0 → 20.0 on 2026-04-25 after T1-B9
        # validation showed Opus 4.7 hits the $2 cap mid-implementation
        # on substantive auto-improve tasks, killing the worker before
        # commit/push/PR. $20 keeps the daily $100 cap as the real
        # envelope (5 max dispatches per day at the ceiling, far more
        # at typical $1-3 spend).
        return BudgetCaps(
            daily_soft_warn=25.0,
            daily_hard_cap=100.0,
            per_task_opus=20.0,
            per_task_sonnet=0.50,
        )

    # ---- Target repo ----

    def github_repo(self) -> str:
        return "escotilha/oxibyoxi"

    def roadmap_location(self) -> str:
        return "docs/roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/", "fix/", "chore/")

    # ---- Dispatch ----

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        # Concurrency is *probed* at every call, not hardcoded.
        # `recommend_concurrency` reads live free RAM (vm_stat on macOS,
        # /proc/meminfo MemAvailable on Linux) *and* the 5-minute load
        # average (os.getloadavg) to compute both a RAM envelope and a
        # CPU envelope.  The final concurrency is the minimum of:
        #   - ram_envelope  = floor((free_gb - reserved) / worker_gb)
        #   - cpu_envelope  = max(1, cpu_count - load_avg_5min)
        #   - HARDWARE_CONCURRENCY_CEILING (default 20)
        #
        # On a busy host (load avg 13 on a 10-core Mac mini) the CPU
        # envelope prevents oxi from adding workers that would push load
        # to 36+.  On an idle host, RAM is the binding constraint.
        #
        # If any probe fails (sandboxed env, unrecognized platform),
        # only the successfully-probed envelopes are applied; unprobed
        # axes default to HARDWARE_CONCURRENCY_CEILING so they don't
        # artificially constrain. Absolute floor is 1.
        #
        # Operator override: set OXI_MAX_CONCURRENT in the engine's
        # environment to force a specific value, bypassing all probes.
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
                max_concurrent = recommend_concurrency()
        else:
            max_concurrent = recommend_concurrency()
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

    # ---- Inference gateway (T2-36) ----

    def inference_gateway_url(self) -> InferenceGatewayConfig:
        """Return connection details for the Mac Mini LiteLLM gateway.

        The gateway URL and per-role virtual keys are read from environment
        variables set by the operator following
        ``docs/runbooks/litellm-gateway.md``.

        Returns an ``InferenceGatewayConfig`` with:
          - ``base_url``: value of ``OXI_GATEWAY_BASE_URL`` (empty string
            when unset, meaning the gateway is not configured).
          - ``virtual_keys``: mapping of role → key secret for each role
            whose ``OXI_GATEWAY_KEY_<ROLE>`` env var is set.  Roles whose
            env vars are absent are omitted so callers can detect
            "not provisioned" without catching exceptions.

        Coupling note: T2-38 wires the heartbeat-triage path to call this
        method and falls back to no-LLM behaviour when ``base_url`` is
        empty or ``"heartbeat"`` is absent from ``virtual_keys``.
        """
        base_url = os.environ.get("OXI_GATEWAY_BASE_URL", "").rstrip("/")

        _KEY_ENV_VARS: dict[str, str] = {
            "heartbeat": "OXI_GATEWAY_KEY_HEARTBEAT",
            "classifier": "OXI_GATEWAY_KEY_CLASSIFIER",
            "summary": "OXI_GATEWAY_KEY_SUMMARY",
        }
        virtual_keys: dict[str, str] = {}
        for role, env_var in _KEY_ENV_VARS.items():
            value = os.environ.get(env_var, "")
            if value:
                virtual_keys[role] = value

        if base_url:
            logger.info(
                "self_adapter.inference_gateway.configured",
                extra={
                    "base_url": base_url,
                    "roles_provisioned": sorted(virtual_keys.keys()),
                },
            )
        else:
            logger.debug("self_adapter.inference_gateway.unconfigured")

        return InferenceGatewayConfig(
            base_url=base_url,
            virtual_keys=virtual_keys,
        )

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
