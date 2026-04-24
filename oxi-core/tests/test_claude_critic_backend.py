"""Tests for oxi_core.v3.critic.ClaudeCriticBackend.

Runs against the fake claude binary (no real Claude invocation). The
fake's new scenarios (critic_approve, critic_reject, critic_garbage)
emit the exact response shapes the backend's parser expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    RoadmapItem,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3.critic import (
    ClaudeCriticBackend,
    CriticInput,
    Verdict,
)

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"


# ---------------------------------------------------------------------------
# Adapter fixture (ClaudeCriticBackend needs an active adapter indirectly
# through critic_prompt → adapter.naming + adapter.github_repo).
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    def naming(self): return NamingConfig(instance_name="oxi-critic-test")
    def paths(self): return PathsConfig()
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (
            DispatchHost(
                name="local", ssh_alias=None,
                max_concurrent=1, worktree_root="/tmp",
            ),
        )
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    register_adapter(_Adapter())
    yield
    clear_adapter()


def _input(pr_number: int = 42) -> CriticInput:
    return CriticInput(
        item=RoadmapItem(identifier="T0-1", tier=0, title="do the thing"),
        pr_number=pr_number,
        diff="@@ -0,0 +1,1 @@\n+line\n",
        ci_status="pass",
        head_branch="feat/sa-t0-1",
    )


def _backend_for(scenario: str, tmp_path: Path) -> ClaudeCriticBackend:
    """Build a backend pointed at the fake binary for a given scenario.

    Passes the scenario via extra_env so it reaches the child (the
    env whitelist in dispatch_invoke.build_env strips anything not in
    the base set unless explicitly included).
    """
    return ClaudeCriticBackend(
        cwd=tmp_path,
        binary=str(FAKE_CLAUDE),
        wall_clock_timeout_s=10.0,
        extra_env={"FAKE_CLAUDE_SCENARIO": scenario},
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_scenario_returns_approve(tmp_path: Path):
    backend = _backend_for("critic_approve", tmp_path)
    verdict = await backend.review_async(_input())
    assert verdict.verdict is Verdict.APPROVE
    assert "clean diff" in verdict.reason


@pytest.mark.asyncio
async def test_reject_scenario_returns_reject(tmp_path: Path):
    backend = _backend_for("critic_reject", tmp_path)
    verdict = await backend.review_async(_input())
    assert verdict.verdict is Verdict.REJECT
    assert "commented-out" in verdict.reason


@pytest.mark.asyncio
async def test_garbage_response_defaults_to_reject(tmp_path: Path):
    """Unparseable model output → REJECT with reason=unparseable.

    Critical invariant: never claim APPROVE when the response is
    ambiguous. auto_merge must have a clear signal.
    """
    backend = _backend_for("critic_garbage", tmp_path)
    verdict = await backend.review_async(_input())
    assert verdict.verdict is Verdict.REJECT
    assert "unparseable" in verdict.reason.lower()


# ---------------------------------------------------------------------------
# Claude unavailable paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_143_surfaces_as_claude_unavailable(tmp_path: Path):
    """The Bash-timeout SIGTERM bug → REJECT with claude_unavailable.

    Matches the dispatch_invoke classification of RETRYABLE_TRANSIENT.
    auto_merge records the rejection; task stays dispatched; next
    tick retries.
    """
    backend = _backend_for("exit_143", tmp_path)
    verdict = await backend.review_async(_input())
    assert verdict.verdict is Verdict.REJECT
    assert "claude_unavailable" in verdict.reason
    assert "retryable_transient" in verdict.reason


@pytest.mark.asyncio
async def test_timeout_surfaces_as_claude_unavailable(tmp_path: Path):
    backend = ClaudeCriticBackend(
        cwd=tmp_path,
        binary=str(FAKE_CLAUDE),
        wall_clock_timeout_s=1.5,
        extra_env={"FAKE_CLAUDE_SCENARIO": "hung"},
    )
    verdict = await backend.review_async(_input())
    assert verdict.verdict is Verdict.REJECT
    assert "claude_unavailable" in verdict.reason
    assert "timeout" in verdict.reason


# ---------------------------------------------------------------------------
# Dataclass defaults
# ---------------------------------------------------------------------------


def test_default_model_is_sonnet(tmp_path: Path):
    b = ClaudeCriticBackend(cwd=tmp_path)
    assert "sonnet" in b.model


def test_default_budget_is_conservative(tmp_path: Path):
    b = ClaudeCriticBackend(cwd=tmp_path)
    # Critic is Sonnet-scale work; default cap should be small enough
    # that an accidentally-enabled backend can't blow budget on a
    # single review.
    assert b.max_budget_usd <= 1.0


def test_cwd_defaults_to_current_directory():
    b = ClaudeCriticBackend()
    assert b.cwd == Path.cwd()


# ---------------------------------------------------------------------------
# Text extraction helper
# ---------------------------------------------------------------------------


def test_extract_last_assistant_text_joins_blocks():
    from oxi_core.v3.critic import _extract_last_assistant_text

    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "first "},
                    {"type": "text", "text": "second"},
                ],
            },
        },
    ]
    assert _extract_last_assistant_text(events) == "first second"


def test_extract_last_assistant_text_prefers_final_assistant():
    from oxi_core.v3.critic import _extract_last_assistant_text

    events = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "earlier"},
            ]},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "final"},
            ]},
        },
    ]
    assert _extract_last_assistant_text(events) == "final"


def test_extract_last_assistant_text_empty_when_no_assistant():
    from oxi_core.v3.critic import _extract_last_assistant_text

    events = [{"type": "system"}]
    assert _extract_last_assistant_text(events) == ""


def test_extract_last_assistant_text_skips_non_text_blocks():
    from oxi_core.v3.critic import _extract_last_assistant_text

    events = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read"},
                {"type": "text", "text": "only text"},
            ]},
        },
    ]
    assert _extract_last_assistant_text(events) == "only text"
