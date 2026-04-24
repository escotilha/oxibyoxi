"""Critic — the pre-merge gate.

The critic is what keeps auto-merge from shipping garbage. It receives
the roadmap item the PR is meant to satisfy, the unified diff, and
the CI status, and returns either APPROVE or REJECT with a one-line
reason.

Architecture: a ``CriticBackend`` protocol so the production backend
(real Claude invocation) can be swapped with a deterministic stub in
tests. This mirrors the ``GitHubClient`` pattern.

Backends defined here:

- ``FunctionCriticBackend`` — takes a Python callable. Tests use this
  to drive precise scenarios (approve-everything, reject-everything,
  reject-on-condition).
- ``AlwaysApproveBackend`` — convenience for happy-path tests.
- ``AlwaysRejectBackend`` — convenience for rejection-path tests.
- ``ClaudeCriticBackend`` — real Claude invocation via
  ``dispatch_invoke.invoke``. Wired here but gated behind an explicit
  opt-in (the CLI default is a stub). Never invoked in the test
  suite; covered by tests that stub out the subprocess.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from ..adapter import RoadmapItem
from ..prompts import critic_prompt


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
# ClaudeCriticBackend — real Claude invocation
# ---------------------------------------------------------------------------


@dataclass
class ClaudeCriticBackend:
    """Invoke ``claude -p`` to review a PR.

    This backend composes a prompt via ``prompts.critic_prompt``,
    hands it to ``dispatch_invoke.invoke``, and parses the last
    assistant event's text via ``parse_critic_response``.

    Safety:

    - Never runs by default. The CLI opts in via ``--real-claude``.
    - Budget is per-invocation via ``max_budget_usd`` (defaults to a
      conservative $0.30 because critic is a short, Sonnet-scale task).
    - Claude exit 143 / rate-limit-exhausted surfaces as REJECT with
      reason="claude_unavailable". The supervisor should treat REJECT
      here as transient-ish (don't re-dispatch the task, but revisit
      on the next tick — ``auto_merge`` will retry).

    The backend requires an active adapter and an event loop. The
    public ``review`` method runs the coroutine synchronously via
    ``asyncio.run`` so callers that are not async-aware can use it
    unchanged. Callers already in an event loop should use
    ``review_async`` instead.
    """

    # Path to a working directory the invocation should use as cwd.
    # Defaults to the current directory; tests can override.
    cwd: Path = field(default_factory=Path.cwd)

    # Claude model to invoke. Defaults to Sonnet for cost reasons —
    # critic work doesn't need Opus reasoning for most diffs.
    model: str = "claude-sonnet-4-6"

    # Per-review budget cap in USD. Conservative.
    max_budget_usd: float = 0.30

    # Per-review turn limit. Critic should reach a verdict in one turn.
    max_turns: int = 3

    # Subprocess binary. Overridable for tests.
    binary: str = "claude"

    # Wall-clock timeout per review.
    wall_clock_timeout_s: float = 300.0

    # Extra env vars passed to the subprocess. Tests use this to pipe
    # FAKE_CLAUDE_SCENARIO through the env whitelist.
    extra_env: dict[str, str] = field(default_factory=dict)

    def review(self, input: CriticInput) -> CriticVerdict:
        """Synchronous entry — wraps ``review_async`` via ``asyncio.run``.

        Raises ``RuntimeError`` if called from within a running event
        loop (Python forbids nested ``asyncio.run``). Async callers
        must use ``review_async`` directly.
        """
        return asyncio.run(self.review_async(input))

    async def review_async(self, input: CriticInput) -> CriticVerdict:
        """Async variant. Invokes claude and parses the response."""
        # Local import to avoid a circular edge (dispatch_invoke is in
        # the same package and imports from ``adapter`` which critic
        # also touches).
        from .dispatch_invoke import (
            Classification,
            DispatchInvocation,
            generate_session_id,
            invoke,
        )

        prompt = critic_prompt(
            item=input.item,
            pr_number=input.pr_number,
            diff=input.diff,
            ci_status=input.ci_status,
        )
        invocation = DispatchInvocation(
            prompt=prompt,
            cwd=self.cwd,
            session_id=generate_session_id(),
            model=self.model,
            max_budget_usd=self.max_budget_usd,
            max_turns=self.max_turns,
            allowed_tools=(),  # Critic needs to read the diff, not act.
            extra_env=dict(self.extra_env),
            wall_clock_timeout_s=self.wall_clock_timeout_s,
            binary=self.binary,
        )
        result = await invoke(invocation)

        if result.classification is not Classification.SUCCESS:
            # Non-success means we couldn't get a clean verdict.
            # Conservative: REJECT with a clear reason so auto_merge
            # records why it held, not a silent timeout.
            return CriticVerdict(
                verdict=Verdict.REJECT,
                reason=f"claude_unavailable: {result.classification.value}",
            )

        text = _extract_last_assistant_text(result.events)
        return parse_critic_response(text)


def _extract_last_assistant_text(events: list[dict]) -> str:
    """Pull the concatenated text from the final assistant event.

    Stream-json assistant events carry ``message.content`` as a list
    of content blocks; each text block has ``type='text'`` and a
    ``text`` string. We concatenate all text blocks of the last
    assistant event in order.
    """
    last_assistant: dict | None = None
    for evt in events:
        if evt.get("type") == "assistant":
            last_assistant = evt
    if last_assistant is None:
        return ""
    content = last_assistant.get("message", {}).get("content", [])
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Response parser (shared by ClaudeCriticBackend and tests)
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
    "ClaudeCriticBackend",
    "CriticBackend",
    "CriticInput",
    "CriticVerdict",
    "FunctionCriticBackend",
    "Verdict",
    "parse_critic_response",
]
