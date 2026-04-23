"""Templated prompts used by the planner, critic, and dispatch.

Prompts are plain strings built by small functions. Every function that
returns a prompt reads instance identity (name, roadmap location, etc.)
from the active adapter; no prompt contains a hardcoded project name.

The functions here are pure — same adapter state, same arguments, same
output. That property matters for tests (deterministic fixtures) and
for audit (diffing a rendered prompt against git history).

Style notes

- Prompts are written in second-person imperative: "Do X. Don't do Y."
- Prompts end with an explicit expected output shape. No model should
  have to guess the format.
- No chain-of-thought instructions. Downstream Claude sessions reason
  adaptively.
"""

from __future__ import annotations

from .adapter import RoadmapItem, get_active_adapter

# ---------------------------------------------------------------------------
# Shared chrome
# ---------------------------------------------------------------------------


def _instance_name() -> str:
    return get_active_adapter().naming().instance_name


def _github_repo() -> str:
    return get_active_adapter().github_repo()


# ---------------------------------------------------------------------------
# Planner — decides what to dispatch next
# ---------------------------------------------------------------------------


def planner_prompt(pending: list[RoadmapItem], in_flight: list[str]) -> str:
    """Prompt for the planner step.

    Arguments:
        pending: roadmap items with status 'planned' (not yet dispatched).
        in_flight: identifiers of tasks currently dispatched or in review.

    The model picks the next item to dispatch (or returns "wait" if
    every pending item is blocked by something in flight).
    """
    instance = _instance_name()
    repo = _github_repo()

    pending_block = (
        "\n".join(
            f"- {item.identifier} (tier {item.tier}): {item.title}"
            + (f" — {item.subtitle}" if item.subtitle else "")
            for item in pending
        )
        or "(none)"
    )
    in_flight_block = ", ".join(in_flight) if in_flight else "(none)"

    return f"""You are the planner for the {instance} engine, running
against the {repo} repository.

Pick the next roadmap item to dispatch. Prefer lower-tier items (tier 0
is highest priority). Skip any item whose expected file scope overlaps
with an in-flight task.

Pending items:
{pending_block}

In flight: {in_flight_block}

Respond with exactly one line in this shape:
  DISPATCH <identifier>
  WAIT
Nothing else.
""".strip()


# ---------------------------------------------------------------------------
# Dispatch — tells a worker what to build
# ---------------------------------------------------------------------------


def dispatch_prompt(item: RoadmapItem, branch_name: str) -> str:
    """Prompt handed to the `claude -p` session that will implement the item.

    The worker is given the roadmap item verbatim and a branch name.
    It is expected to write code, commit, push, and open a PR.
    """
    instance = _instance_name()
    repo = _github_repo()
    subtitle = f"\n\nSubtitle: {item.subtitle}" if item.subtitle else ""

    return f"""You are a worker session spawned by the {instance} engine.
Target repository: {repo}. You are operating on branch `{branch_name}`,
already checked out in the current worktree.

Roadmap item: {item.identifier} (tier {item.tier})
Title: {item.title}{subtitle}

Implement this item. Follow the repo's existing conventions. When done:

1. Commit with a message of the form `{item.identifier}: {item.title}`.
2. Push the branch to origin.
3. Open a pull request against the default branch with a description
   that includes the verification steps a reviewer should run.

Do not modify unrelated files. Do not bypass commit hooks. If the scope
is larger than a single PR, stop and report back instead of forking
follow-up work.
""".strip()


# ---------------------------------------------------------------------------
# Critic — reviews a PR before auto-merge
# ---------------------------------------------------------------------------


def critic_prompt(
    item: RoadmapItem, pr_number: int, diff: str, ci_status: str
) -> str:
    """Prompt for the critic that gates auto-merge.

    Arguments:
        item: the roadmap item this PR is meant to satisfy.
        pr_number: the GitHub PR number.
        diff: unified diff of PR head vs base.
        ci_status: pass/fail summary of the PR's CI checks.
    """
    instance = _instance_name()

    return f"""You are the critic for the {instance} engine. You gate
auto-merge decisions.

The PR below is meant to satisfy roadmap item {item.identifier}: {item.title}.
CI status: {ci_status}.

Review the diff and decide whether to merge.

Merge only if ALL of:
- The diff implements the roadmap item (not something else).
- The diff stays in scope — no unrelated refactors, no dead code left behind.
- CI is green.
- There are no obvious regressions (commented-out tests, skipped hooks,
  weakened assertions).
- No secrets, tokens, or credentials appear in the diff.

If you'd approve in a human review, respond:
  APPROVE
  <one-line rationale>

If you'd reject, respond:
  REJECT
  <one-line reason>

Nothing else. No prose between lines.

PR #{pr_number} diff:
---
{diff}
---
""".strip()


__all__ = [
    "critic_prompt",
    "dispatch_prompt",
    "planner_prompt",
]
