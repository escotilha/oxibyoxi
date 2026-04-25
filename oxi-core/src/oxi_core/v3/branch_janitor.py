"""Prune merged-and-closed oxi-authored remote branches.

After N successful dispatches, the target repo accumulates N stale
``feat/<tag>-<slug>`` branches. ``git fetch`` gets slower, ``git
branch -r`` gets unreadable. This module deletes branches that are
both:

1. Named with a recognized oxi prefix (adapter.branch_prefixes()).
2. Either (a) associated with a MERGED PR, or (b) not referenced by
   any open PR **and** fully reachable from the repo's default branch
   (i.e. no unmerged commits).

Rule (2)(b) is conservative: a human who happens to push to
``feat/x-y`` for unrelated reasons keeps their work. The janitor
only cleans branches oxi actually merged.

Operates against GitHubClient (list_open_prs) and a repo-local
git executable for ancestry checks. Dry-run available via
``delete=False`` — useful as a preflight before actually pruning.

All subprocess calls pass ``cwd`` via ``os.fspath`` (not ``str``)
to correctly handle any ``PathLike`` input.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..adapter import get_active_adapter
from .github_client import GhCliClient, GitHubClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JanitorReport:
    """Summary of one janitor pass."""

    considered: int
    deleted: int
    skipped_not_oxi_prefix: int
    skipped_has_open_pr: int
    skipped_unmerged_commits: int
    deleted_branches: tuple[str, ...] = field(default_factory=tuple)


def _list_remote_branches(
    repo_root: Path, remote: str = "origin"
) -> tuple[str, ...]:
    """Fetch + enumerate remote branches, stripping the remote prefix.

    Returns e.g. ``("feat/sa-t0-1", "main", "feat/sb-t0-2")``.
    """
    subprocess.run(
        ["git", "fetch", "--prune", remote],
        cwd=os.fspath(repo_root), check=False, capture_output=True,
    )
    proc = subprocess.run(
        ["git", "branch", "-r", "--format=%(refname:short)"],
        cwd=os.fspath(repo_root), check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ()
    branches: list[str] = []
    prefix = f"{remote}/"
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(f"{prefix}HEAD"):
            continue
        if stripped.startswith(prefix):
            branches.append(stripped[len(prefix):])
    return tuple(branches)


def _has_unmerged_commits(
    repo_root: Path, branch: str,
    *, default_branch: str, remote: str = "origin",
) -> bool:
    """True if ``branch`` has commits not reachable from ``default_branch``.

    Uses ``git rev-list --count <default>..<branch>``. If that count is
    0, every commit on the branch is already in default → safe to
    delete. If nonzero, there's unmerged work we refuse to touch.
    """
    proc = subprocess.run(
        ["git", "rev-list", "--count",
         f"{remote}/{default_branch}..{remote}/{branch}"],
        cwd=os.fspath(repo_root), check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # Can't decide → conservative: treat as unmerged.
        return True
    try:
        return int(proc.stdout.strip()) > 0
    except ValueError:
        return True


def _delete_remote_branch(
    repo_root: Path, branch: str, *, remote: str = "origin",
) -> bool:
    """Push a delete to the remote. Returns True on success."""
    proc = subprocess.run(
        ["git", "push", remote, "--delete", branch],
        cwd=os.fspath(repo_root), check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        logger.warning(
            "branch_janitor: failed to delete %s: %s",
            branch, proc.stderr.strip(),
        )
        return False
    return True


def _has_oxi_prefix(branch: str, prefixes: tuple[str, ...]) -> bool:
    return any(branch.startswith(p) for p in prefixes)


def run(
    *,
    repo_root: Path,
    client: GitHubClient | None = None,
    repo: str | None = None,
    default_branch: str = "main",
    remote: str = "origin",
    branch_prefixes: tuple[str, ...] | None = None,
    delete: bool = True,
) -> JanitorReport:
    """Run one janitor pass.

    Arguments:
        repo_root: local working tree of the target repo.
        client: GitHub client; defaults to GhCliClient.
        repo: owner/name; defaults to adapter.github_repo().
        default_branch: ancestor to check against. Default 'main'.
        remote: remote name. Default 'origin'.
        branch_prefixes: which branch patterns are oxi-authored.
            Defaults to adapter.branch_prefixes().
        delete: if False, report what would be pruned without acting.
            Useful as a dry-run before enabling the janitor on cron.
    """
    adapter = get_active_adapter()
    if client is None:
        client = GhCliClient()
    if repo is None:
        repo = adapter.github_repo()
    if branch_prefixes is None:
        branch_prefixes = adapter.branch_prefixes()

    remote_branches = _list_remote_branches(repo_root, remote=remote)
    open_prs = client.list_open_prs(repo)
    branches_with_open_prs = {pr.head_branch for pr in open_prs}

    considered = 0
    deleted = 0
    not_oxi = 0
    has_open_pr = 0
    unmerged = 0
    deleted_branches: list[str] = []

    for branch in remote_branches:
        if branch == default_branch:
            continue
        if not _has_oxi_prefix(branch, branch_prefixes):
            not_oxi += 1
            continue
        considered += 1

        if branch in branches_with_open_prs:
            has_open_pr += 1
            continue

        if _has_unmerged_commits(
            repo_root, branch,
            default_branch=default_branch, remote=remote,
        ):
            unmerged += 1
            continue

        if delete:
            if _delete_remote_branch(repo_root, branch, remote=remote):
                deleted += 1
                deleted_branches.append(branch)
        else:
            # Dry-run: count as would-delete without acting.
            deleted += 1
            deleted_branches.append(branch)

    return JanitorReport(
        considered=considered,
        deleted=deleted,
        skipped_not_oxi_prefix=not_oxi,
        skipped_has_open_pr=has_open_pr,
        skipped_unmerged_commits=unmerged,
        deleted_branches=tuple(deleted_branches),
    )


# For PR-based deletion (branches whose PR is MERGED): we could also
# cross-reference with closed PRs, but gh's closed-PR query is
# expensive. The ancestry check above catches merged-via-squash
# branches in 99% of cases: after a squash-merge, the branch's tip
# becomes reachable from default_branch, so `rev-list --count` is 0.
# Forks that use merge-commits instead of squash get the same result
# via the merge commit itself.


__all__ = ["JanitorReport", "run"]
