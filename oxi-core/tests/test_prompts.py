"""Tests for oxi_core.prompts — deterministic rendering, no leaks."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
from oxi_core.prompts import critic_prompt, dispatch_prompt, planner_prompt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _TestAdapter:
    """Adapter whose name and repo are configurable per test."""

    instance_name: str = "oxi"
    repo: str = "owner/repo"

    def naming(self) -> NamingConfig:
        return NamingConfig(instance_name=self.instance_name)

    def paths(self) -> PathsConfig:
        return PathsConfig()

    def budget(self) -> BudgetCaps:
        return BudgetCaps()

    def github_repo(self) -> str:
        return self.repo

    def roadmap_location(self) -> str:
        return "roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/",)

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (
            DispatchHost(
                name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"
            ),
        )

    def promote_recipe(self) -> None:
        return None

    def plan_tier(self) -> str:
        return "standard"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


def _item(
    identifier: str = "T0-1",
    tier: int = 0,
    title: str = "do the thing",
    subtitle: str = "",
) -> RoadmapItem:
    return RoadmapItem(
        identifier=identifier, tier=tier, title=title, subtitle=subtitle
    )


# ---------------------------------------------------------------------------
# Planner prompt
# ---------------------------------------------------------------------------


def test_planner_prompt_contains_instance_name_and_repo():
    register_adapter(_TestAdapter(instance_name="demo", repo="owner/demo"))
    text = planner_prompt(pending=[], in_flight=[])
    assert "demo" in text
    assert "owner/demo" in text


def test_planner_prompt_lists_pending_items_with_tier_and_title():
    register_adapter(_TestAdapter())
    pending = [
        _item("T0-1", 0, "first"),
        _item("T1-2", 1, "second", subtitle="has a subtitle"),
    ]
    text = planner_prompt(pending=pending, in_flight=[])
    assert "T0-1 (tier 0): first" in text
    assert "T1-2 (tier 1): second" in text
    assert "has a subtitle" in text


def test_planner_prompt_shows_none_when_empty():
    register_adapter(_TestAdapter())
    text = planner_prompt(pending=[], in_flight=[])
    assert "(none)" in text


def test_planner_prompt_lists_in_flight_identifiers():
    register_adapter(_TestAdapter())
    text = planner_prompt(pending=[], in_flight=["T0-1", "T2-5"])
    assert "T0-1" in text
    assert "T2-5" in text


def test_planner_prompt_specifies_response_shape():
    register_adapter(_TestAdapter())
    text = planner_prompt(pending=[_item()], in_flight=[])
    assert "DISPATCH" in text
    assert "WAIT" in text


def test_planner_prompt_is_deterministic():
    register_adapter(_TestAdapter())
    pending = [_item("T0-1")]
    assert planner_prompt(pending=pending, in_flight=[]) == planner_prompt(
        pending=pending, in_flight=[]
    )


# ---------------------------------------------------------------------------
# Dispatch prompt
# ---------------------------------------------------------------------------


def test_dispatch_prompt_names_branch_and_item():
    register_adapter(_TestAdapter())
    text = dispatch_prompt(_item("T0-1", 0, "add feature"), branch_name="feat/x")
    assert "feat/x" in text
    assert "T0-1" in text
    assert "add feature" in text


def test_dispatch_prompt_includes_subtitle_when_present():
    register_adapter(_TestAdapter())
    item = _item(subtitle="details matter")
    text = dispatch_prompt(item, branch_name="feat/x")
    assert "details matter" in text


def test_dispatch_prompt_omits_subtitle_block_when_absent():
    register_adapter(_TestAdapter())
    text = dispatch_prompt(_item(subtitle=""), branch_name="feat/x")
    assert "Subtitle:" not in text


def test_dispatch_prompt_prescribes_commit_message_shape():
    register_adapter(_TestAdapter())
    item = _item("T0-1", 0, "add feature")
    text = dispatch_prompt(item, branch_name="feat/x")
    assert "T0-1: add feature" in text


def test_dispatch_prompt_mentions_hook_discipline():
    register_adapter(_TestAdapter())
    text = dispatch_prompt(_item(), branch_name="feat/x")
    # Anti-pattern #9 carries through: the worker must not bypass hooks.
    assert "hook" in text.lower()


# ---------------------------------------------------------------------------
# Critic prompt
# ---------------------------------------------------------------------------


def test_critic_prompt_embeds_diff_verbatim():
    register_adapter(_TestAdapter())
    diff = "@@ -0,0 +1,1 @@\n+new line\n"
    text = critic_prompt(_item(), pr_number=42, diff=diff, ci_status="pass")
    assert diff in text
    assert "PR #42" in text


def test_critic_prompt_mentions_ci_status():
    register_adapter(_TestAdapter())
    text = critic_prompt(_item(), pr_number=1, diff="", ci_status="FAIL")
    assert "FAIL" in text


def test_critic_prompt_specifies_approve_reject_shape():
    register_adapter(_TestAdapter())
    text = critic_prompt(_item(), pr_number=1, diff="", ci_status="pass")
    assert "APPROVE" in text
    assert "REJECT" in text


def test_critic_prompt_prescribes_secret_check():
    register_adapter(_TestAdapter())
    text = critic_prompt(_item(), pr_number=1, diff="", ci_status="pass")
    # Anti-pattern #8 carries through into the critic's checklist.
    assert re.search(r"secret|credential|token", text.lower())


# ---------------------------------------------------------------------------
# Cross-prompt invariants
# ---------------------------------------------------------------------------


def test_all_prompts_omit_hardcoded_instance_name_when_adapter_changes():
    """Changing the adapter changes the rendered instance name everywhere."""
    register_adapter(_TestAdapter(instance_name="alpha"))
    alpha_planner = planner_prompt(pending=[], in_flight=[])
    alpha_dispatch = dispatch_prompt(_item(), branch_name="b")
    alpha_critic = critic_prompt(_item(), pr_number=1, diff="", ci_status="pass")

    clear_adapter()
    register_adapter(_TestAdapter(instance_name="beta"))
    beta_planner = planner_prompt(pending=[], in_flight=[])
    beta_dispatch = dispatch_prompt(_item(), branch_name="b")
    beta_critic = critic_prompt(_item(), pr_number=1, diff="", ci_status="pass")

    assert "alpha" in alpha_planner and "beta" in beta_planner
    assert "alpha" in alpha_dispatch and "beta" in beta_dispatch
    assert "alpha" in alpha_critic and "beta" in beta_critic
    # And never cross-contaminated:
    assert "beta" not in alpha_planner
    assert "alpha" not in beta_planner
