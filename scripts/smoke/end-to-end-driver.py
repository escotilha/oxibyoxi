#!/usr/bin/env python3
"""Python driver for the end-to-end smoke test.

Orchestrates every engine module together against the sample-project
fixture, using the fake claude binary and a fake GitHub client. This
is the Phase 1 exit criterion.

Flow:

1. Copy `sample-project/` into a tmp dir and `git init` it so
   worktree_provision has a real upstream to branch from.
2. Register ReferenceAdapter pointed at the tmp clone with a tmp
   SQLite DB.
3. Seed tasks from sample-project/roadmap.md via planner.plan().
4. Run five ticks:
    - dispatch.dispatch_one() with FAKE_CLAUDE_SCENARIO=happy
    - pr_watcher.watch() with a FakeGitHubClient that stamps a PR
      whose state flips to MERGED
    - auto_merge.run() with AlwaysApproveBackend (force=True to
      bypass the adapter gate) — merges the PR
5. Assert at least one task reached `merged` status.

No real Claude is spawned. No real GitHub is contacted. No path
outside the tmp dir is touched.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from oxi_core import db, planner
from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3 import auto_merge, dispatch, pr_watcher
from oxi_core.v3.critic import AlwaysApproveBackend
from oxi_core.v3.engine_state import EngineState
from oxi_core.v3.github_client import (
    PRCheckStatus,
    PRState,
    PullRequest,
)

# Make tests/fixtures importable so we can reach FakeGitHubClient.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "oxi-core" / "tests" / "fixtures"))

from fake_github import FakeGitHubClient  # noqa: E402

FAKE_CLAUDE = REPO_ROOT / "oxi-core" / "tests" / "fixtures" / "fake_claude.py"
SAMPLE_SRC = REPO_ROOT / "sample-project"


# ---------------------------------------------------------------------------
# Smoke adapter — minimal, points at tmp paths.
# ---------------------------------------------------------------------------


@dataclass
class _SmokeAdapter:
    repo_root_: str
    db_path_: str
    worktree_root_: str

    def naming(self) -> NamingConfig:
        return NamingConfig(instance_name="oxi-smoke")

    def paths(self) -> PathsConfig:
        return PathsConfig(
            repo_root=self.repo_root_,
            db_path=self.db_path_,
        )

    def budget(self) -> BudgetCaps:
        return BudgetCaps(
            daily_soft_warn=1.0, daily_hard_cap=5.0,
            per_task_opus=0.50, per_task_sonnet=0.20,
        )

    def github_repo(self) -> str:
        return "smoke/sample-project"

    def roadmap_location(self) -> str:
        return "roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/",)

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (DispatchHost(
            name="local", ssh_alias=None, max_concurrent=3,
            worktree_root=self.worktree_root_,
        ),)

    def promote_recipe(self) -> None:
        return None

    def plan_tier(self) -> str:
        return "standard"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy(auto_merge=True)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=str(cwd), check=True, capture_output=True)


def _clone_sample_project(dest_root: Path) -> Path:
    """Copy sample-project into dest_root and git-init it.

    Returns the path to the cloned working tree. Creates a bare
    upstream alongside so worktree_provision can fetch from 'origin'.
    """
    upstream = dest_root / "sample-upstream.git"
    upstream.mkdir()
    _run(["git", "init", "--bare", "--initial-branch=main"], cwd=upstream)

    clone = dest_root / "sample-clone"
    clone.mkdir()
    _run(["git", "init", "--initial-branch=main"], cwd=clone)
    _run(["git", "config", "user.email", "smoke@example.com"], cwd=clone)
    _run(["git", "config", "user.name", "smoke"], cwd=clone)

    # Copy sample-project contents (files only, no .git).
    import shutil
    for item in SAMPLE_SRC.iterdir():
        if item.name == ".git":
            continue
        dest = clone / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    _run(["git", "add", "-A"], cwd=clone)
    _run(["git", "commit", "-m", "initial"], cwd=clone)
    _run(["git", "remote", "add", "origin", str(upstream)], cwd=clone)
    _run(["git", "push", "-u", "origin", "main"], cwd=clone)
    return clone


# ---------------------------------------------------------------------------
# The smoke run
# ---------------------------------------------------------------------------


async def _tick_once(
    conn,
    state: EngineState,
    repo_root: Path,
    gh_client: FakeGitHubClient,
    *,
    repo: str,
    branch_prefix: str,
) -> dict:
    """Run one end-to-end tick: dispatch → pr_watcher → auto_merge.

    Returns a dict of what happened for the final assertion.
    """
    # Dispatch: fake claude runs "happy" scenario, exits 0.
    dispatch_result = await dispatch.dispatch_one(
        conn=conn,
        adapter=_SmokeAdapterCache.adapter,
        engine_state=state,
        repo_root=repo_root,
        binary=str(FAKE_CLAUDE),
        extra_env={"FAKE_CLAUDE_SCENARIO": "happy"},
    )

    # After dispatch succeeds, simulate a worker that would have opened
    # a PR: add a fake PR whose branch matches the dispatched task.
    if dispatch_result is not None:
        _add_fake_pr_for_latest_dispatch(conn, gh_client, repo)

    # pr_watcher: discovers the PR and stamps pr_number.
    watch_report = pr_watcher.watch(
        conn, state,
        client=gh_client, repo=repo, branch_prefix=branch_prefix,
    )

    # Promote the newly-observed PR to CI-green state so auto_merge
    # admits it (pr_watcher doesn't change CI state).
    _mark_all_open_prs_success(gh_client, repo)

    # auto_merge: approves and merges via the fake client.
    merge_report = auto_merge.run(
        conn, state, AlwaysApproveBackend(),
        client=gh_client, repo=repo, force=True,
    )

    return {
        "dispatch": dispatch_result,
        "watch": watch_report,
        "merge": merge_report,
    }


class _SmokeAdapterCache:
    """Module-level slot so the tick helper can reach the adapter."""
    adapter: _SmokeAdapter | None = None


def _add_fake_pr_for_latest_dispatch(conn, gh_client, repo: str) -> None:
    """Look at the most recently dispatched task and add a PR for it.

    Simulates a worker session that would have opened the PR from
    inside its dispatched claude.
    """
    row = conn.execute(
        "SELECT identifier FROM task WHERE status = 'dispatched' "
        "AND pr_number IS NULL ORDER BY dispatched_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return
    identifier = row["identifier"]
    # Branch convention: feat/<tag>-<slug(identifier)>
    slug = identifier.lower().replace("_", "-")
    branch = f"feat/oxi-{slug}"
    # Pick a PR number based on task identifier for determinism.
    pr_number = 1000 + abs(hash(identifier)) % 9000
    gh_client.add_pr(repo, PullRequest(
        number=pr_number,
        title=f"ship {identifier}",
        head_branch=branch,
        state=PRState.OPEN,
        check_status=PRCheckStatus.PENDING,
        mergeable=True,
    ))


def _mark_all_open_prs_success(gh_client, repo: str) -> None:
    """Flip every open PR's CI status to SUCCESS.

    In production, CI reaches green on its own schedule. In the smoke
    test, we speed it along by flipping here.
    """
    for pr in list(gh_client.list_open_prs(repo)):
        gh_client.set_check_status(repo, pr.number, PRCheckStatus.SUCCESS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _report(label: str, obj) -> None:
    print(f"  {label}: {obj}")


async def main_async() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="oxi-smoke-"))
    print(f"smoke: tmp dir = {tmp}")

    clear_adapter()
    try:
        repo_root = _clone_sample_project(tmp)
        db_path = tmp / "oxi.db"
        worktree_root = tmp / "worktrees"
        worktree_root.mkdir()

        adapter = _SmokeAdapter(
            repo_root_=str(repo_root),
            db_path_=str(db_path),
            worktree_root_=str(worktree_root),
        )
        _SmokeAdapterCache.adapter = adapter
        register_adapter(adapter)

        handle = db.connect()
        conn = handle.connection

        # Seed tasks.
        seeded = planner.plan(repo_root, conn)
        print(f"smoke: seeded {seeded} tasks from roadmap")
        if seeded == 0:
            print("smoke: FAIL — no tasks seeded", file=sys.stderr)
            return 1

        gh_client = FakeGitHubClient()
        state = EngineState(plan_tier="standard", max_concurrent=3)

        # Run five ticks.
        for i in range(5):
            print(f"smoke: tick {i+1}/5")
            result = await _tick_once(
                conn, state, repo_root, gh_client,
                repo=adapter.github_repo(),
                branch_prefix="feat/",
            )
            _report("dispatch", result["dispatch"] is not None)
            _report("watch", result["watch"])
            _report("merge", result["merge"])

        # Final assertion: at least one task is merged.
        merged_count = conn.execute(
            "SELECT COUNT(*) FROM task WHERE status = 'merged'"
        ).fetchone()[0]
        print(f"\nsmoke: merged tasks = {merged_count}")

        dispatch_events = conn.execute(
            "SELECT COUNT(*) FROM event WHERE kind = 'dispatch_started'"
        ).fetchone()[0]
        pr_events = conn.execute(
            "SELECT COUNT(*) FROM event WHERE kind = 'pr_observed'"
        ).fetchone()[0]
        merge_events = conn.execute(
            "SELECT COUNT(*) FROM event WHERE kind LIKE '%merged%'"
        ).fetchone()[0]

        print(
            f"smoke: events — dispatch_started={dispatch_events} "
            f"pr_observed={pr_events} merged={merge_events}"
        )

        if merged_count >= 1:
            print("smoke: PASS — at least one task reached merged")
            handle.connection.close()
            return 0
        else:
            print("smoke: FAIL — no tasks reached merged", file=sys.stderr)
            handle.connection.close()
            return 1
    finally:
        clear_adapter()
        # Leave tmp dir in place for post-mortem if it failed.


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
