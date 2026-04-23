"""GitHubClient protocol + a production implementation that shells to ``gh``.

The protocol lets the rest of the engine (pr_watcher, auto_merge) talk
to GitHub without knowing whether the backing is a real ``gh`` CLI, a
REST client, or a test fake. Tests inject a deterministic
``FakeGitHubClient``; production wires ``GhCliClient``.

The surface is minimal — only the PR-state operations the engine
actually needs:

- ``list_open_prs(repo, head_prefix)`` — find PRs matching a branch
  pattern (used by pr_watcher to discover worker-opened PRs).
- ``get_pr(repo, pr_number)`` — read one PR's state + CI status.
- ``merge_pr(repo, pr_number, method)`` — squash-merge a PR (used by
  auto_merge; not by pr_watcher itself).

Everything is sync. pr_watcher runs from a timer; latency here is
bounded by the ``gh`` CLI's own round-trip.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class PRState(Enum):
    """Normalized PR state. Raw ``gh`` returns strings; we translate."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"  # closed without merge


class PRCheckStatus(Enum):
    """Normalized CI status across checks on a PR.

    - PENDING: at least one check is still running.
    - SUCCESS: all required checks passed.
    - FAILURE: at least one check failed or was cancelled.
    - UNKNOWN: no checks reported yet.
    """

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PullRequest:
    """The subset of PR state the engine cares about."""

    number: int
    title: str
    head_branch: str
    state: PRState
    check_status: PRCheckStatus
    mergeable: bool | None  # None when GitHub hasn't decided yet


class GitHubClient(Protocol):
    """Minimal interface. Forks can substitute any implementation."""

    def list_open_prs(
        self, repo: str, head_prefix: str | None = None
    ) -> tuple[PullRequest, ...]:
        """Return open PRs in ``repo``, optionally filtered by ``head_prefix``."""

    def get_pr(self, repo: str, pr_number: int) -> PullRequest | None:
        """Fetch one PR by number, or None if it doesn't exist."""

    def merge_pr(
        self, repo: str, pr_number: int, *, method: str = "squash"
    ) -> bool:
        """Merge a PR. Returns True on success, False on failure.

        ``method`` is one of 'merge', 'squash', 'rebase'. Default is
        'squash' because that's what most oxi-adjacent projects use.
        """


class GitHubError(RuntimeError):
    """Raised when the gh CLI returns a non-zero exit for a non-404 reason."""


# ---------------------------------------------------------------------------
# gh CLI implementation
# ---------------------------------------------------------------------------


class GhCliClient:
    """Production implementation — shells out to ``gh``.

    Uses argv-form subprocess invocation only (never ``shell=True``).
    Errors from ``gh`` propagate as ``GitHubError`` unless they're
    404-style "PR does not exist" which return None.

    Tests should use ``FakeGitHubClient`` instead.
    """

    def __init__(self, binary: str = "gh") -> None:
        self._binary = binary

    def list_open_prs(
        self, repo: str, head_prefix: str | None = None
    ) -> tuple[PullRequest, ...]:
        args = [
            self._binary, "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,title,headRefName,state,mergeable,statusCheckRollup",
            "--limit", "100",
        ]
        out = self._run(args)
        items = json.loads(out) if out.strip() else []
        prs = [self._parse_pr(it) for it in items]
        if head_prefix is not None:
            prs = [p for p in prs if p.head_branch.startswith(head_prefix)]
        return tuple(prs)

    def get_pr(self, repo: str, pr_number: int) -> PullRequest | None:
        args = [
            self._binary, "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "number,title,headRefName,state,mergeable,statusCheckRollup",
        ]
        try:
            out = self._run(args)
        except GitHubError as e:
            if "not found" in str(e).lower() or "404" in str(e):
                return None
            raise
        if not out.strip():
            return None
        return self._parse_pr(json.loads(out))

    def merge_pr(
        self, repo: str, pr_number: int, *, method: str = "squash"
    ) -> bool:
        flag_map = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}
        if method not in flag_map:
            raise ValueError(f"unknown merge method {method!r}")
        args = [
            self._binary, "pr", "merge", str(pr_number),
            "--repo", repo,
            flag_map[method],
        ]
        try:
            self._run(args)
            return True
        except GitHubError:
            return False

    # ---- Internals ---------------------------------------------------

    def _run(self, argv: list[str]) -> str:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, check=False
            )
        except FileNotFoundError as exc:
            raise GitHubError(
                f"{self._binary} is not on PATH"
            ) from exc
        if proc.returncode != 0:
            raise GitHubError(
                f"{' '.join(argv)} failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return proc.stdout

    @staticmethod
    def _parse_pr(raw: dict) -> PullRequest:
        state_raw = (raw.get("state") or "").lower()
        try:
            state = PRState(state_raw)
        except ValueError:
            state = PRState.OPEN  # conservative default

        mergeable_raw = raw.get("mergeable")
        if mergeable_raw is None or mergeable_raw == "UNKNOWN":
            mergeable = None
        else:
            mergeable = mergeable_raw == "MERGEABLE"

        return PullRequest(
            number=int(raw["number"]),
            title=raw.get("title", ""),
            head_branch=raw.get("headRefName", ""),
            state=state,
            check_status=_rollup_to_status(raw.get("statusCheckRollup", [])),
            mergeable=mergeable,
        )


def _rollup_to_status(rollup: list) -> PRCheckStatus:
    """Collapse ``gh``'s statusCheckRollup list into a single PRCheckStatus.

    ``gh pr view --json statusCheckRollup`` returns a list of check
    dicts with keys like ``conclusion`` (for check runs) or ``state``
    (for commit statuses). We look at both.

    - Any 'failure', 'cancelled', 'timed_out', 'action_required' → FAILURE
    - All 'success' / 'neutral' / 'skipped' → SUCCESS
    - Anything in progress → PENDING
    - Empty list → UNKNOWN
    """
    if not rollup:
        return PRCheckStatus.UNKNOWN

    failure_vals = {
        "failure", "cancelled", "timed_out",
        "action_required", "error", "startup_failure",
    }
    success_vals = {"success", "neutral", "skipped"}

    any_pending = False
    any_failure = False
    all_success = True

    for item in rollup:
        # conclusion is preferred (completed checks); state is fallback
        conclusion = (item.get("conclusion") or "").lower()
        state = (item.get("state") or "").lower()
        status = (item.get("status") or "").lower()

        if status in ("in_progress", "queued", "pending", "waiting"):
            any_pending = True
            all_success = False
            continue

        verdict = conclusion or state
        if verdict in failure_vals:
            any_failure = True
            all_success = False
        elif verdict in success_vals or verdict == "":
            # Empty can mean "no completed status yet" — treat as pending.
            if not verdict:
                any_pending = True
                all_success = False
        else:
            # Unknown verdict — conservative: treat as pending.
            any_pending = True
            all_success = False

    if any_failure:
        return PRCheckStatus.FAILURE
    if any_pending:
        return PRCheckStatus.PENDING
    if all_success:
        return PRCheckStatus.SUCCESS
    return PRCheckStatus.UNKNOWN


__all__ = [
    "GhCliClient",
    "GitHubClient",
    "GitHubError",
    "PRCheckStatus",
    "PRState",
    "PullRequest",
]
