"""Critic — the pre-merge gate.

The critic is what keeps auto-merge from shipping garbage. It receives
the roadmap item the PR is meant to satisfy, the unified diff, and
the CI status, and returns either APPROVE or REJECT with a one-line
reason.

Architecture: a ``CriticBackend`` protocol so the production backend
(real Claude invocation) can be swapped with a deterministic stub in
tests. This mirrors the ``GitHubClient`` pattern.

Three backends are defined here:

- ``FunctionCriticBackend`` — takes a Python callable. Tests use this
  to drive precise scenarios (approve-everything, reject-everything,
  reject-on-condition).
- ``AlwaysApproveBackend`` — convenience for happy-path tests.
- ``AlwaysRejectBackend`` — convenience for rejection-path tests.

A real ``ClaudeCriticBackend`` is deferred to Phase 2. It will take a
``DispatchInvocation`` pre-filled with ``critic_prompt(...)`` output,
call ``invoke()``, parse the first line of the last assistant event
as APPROVE/REJECT. Not built here because real Claude invocation is
gated on operator approval for Phase 2 dogfooding.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..adapter import RoadmapItem


class Verdict(Enum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class CriticInput:
    """Everything a critic needs to reach a verdict.

    Attributes:
        item: the roadmap item the PR is meant to satisfy.
        pr_number: the PR being reviewed.
        diff: unified diff of PR head vs base. May be empty string.
        ci_status: human-readable CI summary (e.g. "pass", "fail").
        head_branch: the PR's head branch name (for context).
    """

    item: RoadmapItem
    pr_number: int
    diff: str
    ci_status: str
    head_branch: str


@dataclass(frozen=True)
class CriticVerdict:
    """What a critic returns.

    Attributes:
        verdict: APPROVE or REJECT.
        reason: one-line rationale (from the critic's model output).
    """

    verdict: Verdict
    reason: str


class CriticBackend(Protocol):
    """Minimal interface every critic implementation satisfies."""

    def review(self, input: CriticInput) -> CriticVerdict: ...


# ---------------------------------------------------------------------------
# Stub / test implementations
# ---------------------------------------------------------------------------


@dataclass
class FunctionCriticBackend:
    """Critic backed by a Python callable.

    The callable takes a ``CriticInput`` and returns a ``CriticVerdict``
    (or raises — the caller handles). Useful for tests that want
    deterministic, condition-driven verdicts.
    """

    fn: Callable[[CriticInput], CriticVerdict]

    def review(self, input: CriticInput) -> CriticVerdict:
        return self.fn(input)


@dataclass(frozen=True)
class AlwaysApproveBackend:
    """Returns APPROVE for every input. Useful for happy-path tests."""

    reason: str = "auto-approve"

    def review(self, input: CriticInput) -> CriticVerdict:  # noqa: ARG002
        return CriticVerdict(verdict=Verdict.APPROVE, reason=self.reason)


@dataclass(frozen=True)
class AlwaysRejectBackend:
    """Returns REJECT for every input. Useful for rejection-path tests."""

    reason: str = "auto-reject"

    def review(self, input: CriticInput) -> CriticVerdict:  # noqa: ARG002
        return CriticVerdict(verdict=Verdict.REJECT, reason=self.reason)


# ---------------------------------------------------------------------------
# Parser for future ClaudeCriticBackend
# ---------------------------------------------------------------------------


def parse_critic_response(text: str) -> CriticVerdict:
    """Parse a critic model's raw response text into a verdict.

    Expected format (as prescribed by ``prompts.critic_prompt``):

        APPROVE
        <one-line rationale>

    or

        REJECT
        <one-line reason>

    Returns REJECT on unparseable input — safer default. The reason
    field in that case is the raw input truncated to 200 chars.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return CriticVerdict(verdict=Verdict.REJECT, reason="empty response")

    verdict_token = lines[0].upper()
    reason = lines[1] if len(lines) > 1 else ""

    if verdict_token == "APPROVE":
        return CriticVerdict(verdict=Verdict.APPROVE, reason=reason)
    if verdict_token == "REJECT":
        return CriticVerdict(verdict=Verdict.REJECT, reason=reason)

    return CriticVerdict(
        verdict=Verdict.REJECT,
        reason=f"unparseable: {text[:200]}",
    )


__all__ = [
    "AlwaysApproveBackend",
    "AlwaysRejectBackend",
    "CriticBackend",
    "CriticInput",
    "CriticVerdict",
    "FunctionCriticBackend",
    "Verdict",
    "parse_critic_response",
]
