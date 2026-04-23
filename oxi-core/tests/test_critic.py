"""Tests for oxi_core.v3.critic — backends + response parsing."""

from __future__ import annotations

from oxi_core.adapter import RoadmapItem
from oxi_core.v3.critic import (
    AlwaysApproveBackend,
    AlwaysRejectBackend,
    CriticInput,
    CriticVerdict,
    FunctionCriticBackend,
    Verdict,
    parse_critic_response,
)


def _input(identifier: str = "T0-1", pr_number: int = 42) -> CriticInput:
    return CriticInput(
        item=RoadmapItem(identifier=identifier, tier=0, title="t"),
        pr_number=pr_number,
        diff="@@ -0,0 +1,1 @@\n+line\n",
        ci_status="pass",
        head_branch="feat/sa-t0-1",
    )


# ---------------------------------------------------------------------------
# Stub backends
# ---------------------------------------------------------------------------


def test_always_approve_backend():
    b = AlwaysApproveBackend()
    v = b.review(_input())
    assert v.verdict is Verdict.APPROVE


def test_always_reject_backend():
    b = AlwaysRejectBackend(reason="nope")
    v = b.review(_input())
    assert v.verdict is Verdict.REJECT
    assert v.reason == "nope"


def test_function_backend_invokes_callable():
    called_with: list[CriticInput] = []

    def reviewer(inp: CriticInput) -> CriticVerdict:
        called_with.append(inp)
        return CriticVerdict(verdict=Verdict.APPROVE, reason="ok")

    b = FunctionCriticBackend(fn=reviewer)
    inp = _input()
    v = b.review(inp)
    assert v.verdict is Verdict.APPROVE
    assert called_with == [inp]


def test_function_backend_can_reject_on_condition():
    """Rejects based on input content — exactly what a real critic would do."""
    def reviewer(inp: CriticInput) -> CriticVerdict:
        if "secret" in inp.diff.lower():
            return CriticVerdict(verdict=Verdict.REJECT, reason="contains secret")
        return CriticVerdict(verdict=Verdict.APPROVE, reason="clean")

    b = FunctionCriticBackend(fn=reviewer)
    safe_input = _input()
    v = b.review(safe_input)
    assert v.verdict is Verdict.APPROVE

    bad_input = CriticInput(
        item=safe_input.item, pr_number=1,
        diff="+AWS_SECRET_ACCESS_KEY=xxxxxx\n",
        ci_status="pass", head_branch="x",
    )
    v = b.review(bad_input)
    assert v.verdict is Verdict.REJECT
    assert "secret" in v.reason.lower()


# ---------------------------------------------------------------------------
# parse_critic_response
# ---------------------------------------------------------------------------


def test_parse_approve():
    v = parse_critic_response("APPROVE\nclean diff, tests pass")
    assert v.verdict is Verdict.APPROVE
    assert v.reason == "clean diff, tests pass"


def test_parse_reject():
    v = parse_critic_response("REJECT\ntest weakened")
    assert v.verdict is Verdict.REJECT
    assert v.reason == "test weakened"


def test_parse_approve_without_reason():
    v = parse_critic_response("APPROVE")
    assert v.verdict is Verdict.APPROVE
    assert v.reason == ""


def test_parse_is_case_insensitive_on_verdict():
    assert parse_critic_response("approve\nok").verdict is Verdict.APPROVE
    assert parse_critic_response("Reject\nbad").verdict is Verdict.REJECT


def test_parse_unparseable_defaults_to_reject():
    """Safer to reject on ambiguity than to ship something."""
    v = parse_critic_response("I think this looks fine")
    assert v.verdict is Verdict.REJECT
    assert "unparseable" in v.reason


def test_parse_empty_defaults_to_reject():
    v = parse_critic_response("")
    assert v.verdict is Verdict.REJECT
    assert "empty" in v.reason.lower()


def test_parse_strips_whitespace():
    v = parse_critic_response("  \n\nAPPROVE\n  solid diff  \n\n")
    assert v.verdict is Verdict.APPROVE
    assert v.reason == "solid diff"


# ---------------------------------------------------------------------------
# CriticVerdict dataclass
# ---------------------------------------------------------------------------


def test_critic_verdict_is_frozen():
    from dataclasses import FrozenInstanceError

    import pytest
    v = CriticVerdict(verdict=Verdict.APPROVE, reason="x")
    with pytest.raises(FrozenInstanceError):
        v.reason = "y"  # type: ignore[misc]
