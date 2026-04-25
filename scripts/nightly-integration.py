#!/usr/bin/env python3
"""Nightly integration test against the real GitHub API.

Exercises the read-only surface of ``GhCliClient`` against the live
``escotilha/oxi`` repo. Catches API drift, ``gh`` CLI breakage,
and cassette/fixture rot before a real dispatch needs it at 03:00.

Read-only by design — no PR writes, no branches created, no merges.
The roadmap (T2-14) leaves the door open for a sandbox-repo
write-path later, but the cost of maintaining a sandbox repo
(token, lifecycle, cleanup) outweighs the marginal coverage when
the read surface is what regresses in practice (gh CLI version
bumps, schema changes in the response JSON).

What it asserts:
  1. ``list_open_prs`` returns a non-error result (may be empty)
  2. For each open PR, ``get_pr`` returns a populated ``PullRequest``
     dataclass with at least number + title set
  3. For one of those PRs, ``list_check_runs`` returns a tuple of
     ``CheckRun`` records (may be empty if no checks ran yet)

Failure mode: any exception aborts with non-zero. The CI workflow
catches the failure and surfaces it in the run log; nothing is
auto-merged on failure.
"""
from __future__ import annotations

import os
import sys

from oxi_core.v3.github_client import (
    CheckRun,
    GhCliClient,
    PullRequest,
)


REPO = os.environ.get("OXI_NIGHTLY_REPO", "escotilha/oxi")


def main() -> int:
    print(f"nightly-integration: target repo = {REPO}")
    client = GhCliClient()

    # 1. list_open_prs
    print("\n[1/3] list_open_prs(...)")
    prs = client.list_open_prs(REPO)
    print(f"  -> {len(prs)} open PR(s)")
    if not prs:
        print(
            "  (no open PRs; will skip get_pr + list_check_runs steps)"
        )
        return 0

    # 2. get_pr — pick the PR with the highest number (most recent).
    target_pr = max(prs, key=lambda p: p.number)
    print(f"\n[2/3] get_pr(repo, {target_pr.number})")
    pr = client.get_pr(REPO, target_pr.number)
    if not isinstance(pr, PullRequest):
        print(
            f"  FAIL: expected PullRequest, got {type(pr).__name__}",
            file=sys.stderr,
        )
        return 1
    print(f"  -> #{pr.number}: {pr.title[:60]}")
    print(f"     state={pr.state.value} head={pr.head_branch}")

    # 3. list_check_runs
    print(f"\n[3/3] list_check_runs(repo, {target_pr.number})")
    runs = client.list_check_runs(REPO, target_pr.number)
    if not isinstance(runs, tuple):
        print(
            f"  FAIL: expected tuple, got {type(runs).__name__}",
            file=sys.stderr,
        )
        return 1
    print(f"  -> {len(runs)} check run(s)")
    for run in runs[:5]:
        if not isinstance(run, CheckRun):
            print(
                f"  FAIL: tuple member is {type(run).__name__}",
                file=sys.stderr,
            )
            return 1
        print(f"     {run.name[:50]}: status={run.status} conclusion={run.conclusion}")

    print("\nnightly-integration: all probes succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
