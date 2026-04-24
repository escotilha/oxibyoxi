"""File-level conflict gate: block dispatch when planned files touch an open PR.

dispatch.py already prevents two in-flight tasks from touching the same
file. But the engine can lose visibility in one case: a Claude session
exits cleanly, opens a PR, heartbeat marks the task ``dispatched`` with
``pr_number`` set, then a different task is planned whose
``files_touched`` overlaps with that open PR. Without this gate the
second task dispatches anyway, produces a conflicting diff, and
wastes a dispatch + triggers a merge conflict.

``check_overlap`` answers: *given a list of files this planned task
would touch, which open PRs on the target repo already touch any of
them?*

The fetch is cached by PR number for the duration of one pass so
multiple planned tasks against the same PR set don't re-hit GitHub.
Cache clears at the end of the pass (caller instantiates a new
``OverlapChecker`` each time).

GitHub file-list access uses a GhCliClient method that isn't in the
GitHubClient protocol yet (``pr_files``). We add it here — tests
inject a FakeGitHubClient that implements the same shape.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field

from .github_client import GhCliClient, GitHubClient

logger = logging.getLogger(__name__)


class PrFilesClient:
    """Minimal extension of GitHubClient: fetch an open PR's file list.

    Defined here rather than in github_client because it's only used
    by pr_overlap; core engine modules don't need the file list.
    """

    def pr_files(self, repo: str, pr_number: int) -> tuple[str, ...]:
        """Return the tuple of paths changed by ``pr_number``."""
        raise NotImplementedError


class GhCliPrFilesClient(GhCliClient):
    """GhCliClient + pr_files via ``gh pr view --json files``."""

    def pr_files(self, repo: str, pr_number: int) -> tuple[str, ...]:
        args = [
            self._binary, "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "files",
        ]
        try:
            out = self._run(args)
        except Exception:
            # Any gh failure → treat as "no known files" so overlap
            # returns False (fail-open for this specific gate; other
            # gates catch real problems).
            return ()
        try:
            payload = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            return ()
        files = payload.get("files") or []
        return tuple(
            f.get("path", "")
            for f in files
            if isinstance(f, dict) and f.get("path")
        )


@dataclass
class OverlapChecker:
    """Cached file-list lookups over a GitHubClient with ``pr_files``.

    One instance per planner pass. Keeps fetches down to O(open_prs)
    rather than O(planned_tasks × open_prs).
    """

    client: GitHubClient  # must also implement pr_files (duck-typed)
    repo: str
    _cache: dict[int, tuple[str, ...]] = field(default_factory=dict)

    def files_for(self, pr_number: int) -> tuple[str, ...]:
        """Fetch + cache the file list for one PR."""
        if pr_number not in self._cache:
            fetch = getattr(self.client, "pr_files", None)
            if fetch is None:
                logger.info(
                    "pr_overlap: client lacks pr_files; skipping overlap check"
                )
                self._cache[pr_number] = ()
            else:
                self._cache[pr_number] = fetch(self.repo, pr_number)
        return self._cache[pr_number]


@dataclass(frozen=True)
class OverlapReport:
    """What ``check_overlap`` returns.

    Attributes:
        overlaps: True if any planned file appears in any open PR's
            file list. dispatch should skip planning this task.
        overlapping_prs: PR numbers whose files intersect with the
            planned task's files.
        overlapping_files: the specific intersection set.
    """

    overlaps: bool
    overlapping_prs: tuple[int, ...]
    overlapping_files: tuple[str, ...]


def check_overlap(
    *,
    planned_files: tuple[str, ...],
    open_pr_numbers: tuple[int, ...],
    checker: OverlapChecker,
) -> OverlapReport:
    """Does any planned file appear in any open PR?

    Empty ``planned_files`` → no overlap (nothing to conflict with).
    Empty ``open_pr_numbers`` → no overlap (no PRs to conflict with).
    """
    planned_set = {f for f in planned_files if f}
    if not planned_set or not open_pr_numbers:
        return OverlapReport(
            overlaps=False, overlapping_prs=(), overlapping_files=(),
        )

    overlapping_prs: list[int] = []
    overlapping_files: set[str] = set()

    for pr in open_pr_numbers:
        pr_files = set(checker.files_for(pr))
        intersection = planned_set & pr_files
        if intersection:
            overlapping_prs.append(pr)
            overlapping_files.update(intersection)

    return OverlapReport(
        overlaps=bool(overlapping_prs),
        overlapping_prs=tuple(overlapping_prs),
        overlapping_files=tuple(sorted(overlapping_files)),
    )


__all__ = [
    "GhCliPrFilesClient",
    "OverlapChecker",
    "OverlapReport",
    "PrFilesClient",
    "check_overlap",
]


_ = subprocess  # reserved for a direct-subprocess variant if needed
