"""Tests for oxi_core.v3.pr_watcher — reconciliation against a fake GH client."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# Make tests/fixtures importable.
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from fake_github import FakeGitHubClient  # noqa: E402

from oxi_core import db  # noqa: E402
from oxi_core.adapter import (  # noqa: E402
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3.engine_state import EngineState  # noqa: E402
from oxi_core.v3.github_client import (  # noqa: E402
    PRCheckStatus,
    PRState,
    PullRequest,
)
from oxi_core.v3.pr_watcher import (  # noqa: E402
    WatchReport,
    _branch_to_identifier_slug,
    _identifier_to_slug,
    watch,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    db_path_value: str

    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig(db_path=self.db_path_value)
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (DispatchHost(name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"),)
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def env(tmp_path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "state": EngineState(plan_tier="standard", max_concurrent=4),
        "client": FakeGitHubClient(),
        "repo": "owner/repo",
    }
    handle.connection.close()


def _seed_dispatched(conn, identifier: str, *, pr_number: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number) "
        "VALUES (?, 0, 't', 'dispatched', ?)",
        (identifier, pr_number),
    )
    conn.commit()
    return cur.lastrowid


def _pr(
    number: int,
    head: str,
    *,
    state: PRState = PRState.OPEN,
    check: PRCheckStatus = PRCheckStatus.PENDING,
    mergeable: bool | None = True,
) -> PullRequest:
    return PullRequest(
        number=number, title=f"pr {number}", head_branch=head,
        state=state, check_status=check, mergeable=mergeable,
    )


# ---------------------------------------------------------------------------
# Branch ↔ identifier mapping
# ---------------------------------------------------------------------------


def test_branch_to_identifier_slug_happy():
    assert _branch_to_identifier_slug("feat/sa-t0-1") == "t0-1"


def test_branch_to_identifier_slug_ignores_non_matching():
    assert _branch_to_identifier_slug("random-human-branch") is None
    assert _branch_to_identifier_slug("main") is None


def test_identifier_to_slug_mirrors_worktree():
    assert _identifier_to_slug("T0-1") == "t0-1"
    assert _identifier_to_slug("T2-17") == "t2-17"


# ---------------------------------------------------------------------------
# Phase 1: discovery (first observation of a PR for a task)
# ---------------------------------------------------------------------------


def test_watch_stamps_pr_number_on_discovery(env):
    _seed_dispatched(env["conn"], identifier="T0-1")
    env["client"].add_pr("owner/repo", _pr(42, "feat/sa-t0-1"))

    report = watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    assert report.pr_numbers_stamped == 1
    row = env["conn"].execute(
        "SELECT pr_number, status FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["pr_number"] == 42
    # Still dispatched (CI pending, pr_watcher doesn't move past merge/close).
    assert row["status"] == "dispatched"


def test_watch_ignores_unmatched_prs(env):
    env["client"].add_pr("owner/repo", _pr(99, "random-human-branch"))
    report = watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    assert report.pr_numbers_stamped == 0
    # head_prefix filter keeps it out of the list anyway, so unmatched isn't
    # tripped by non-feat/ branches — verify open_prs_observed instead.
    assert report.open_prs_observed == 0


def test_watch_matches_only_to_dispatched_without_pr(env):
    # Task already has a pr_number — discovery should not re-stamp.
    _seed_dispatched(env["conn"], identifier="T0-1", pr_number=10)
    # Different PR for same branch (shouldn't happen in reality).
    env["client"].add_pr("owner/repo", _pr(99, "feat/sa-t0-1"))

    report = watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    assert report.pr_numbers_stamped == 0


def test_watch_emits_pr_observed_event(env):
    task_id = _seed_dispatched(env["conn"], identifier="T0-1")
    env["client"].add_pr("owner/repo", _pr(42, "feat/sa-t0-1"))

    watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    kinds = [
        r[0] for r in env["conn"].execute(
            "SELECT kind FROM event WHERE task_id = ?", (task_id,)
        )
    ]
    assert "pr_observed" in kinds


# ---------------------------------------------------------------------------
# Phase 2: terminal transitions
# ---------------------------------------------------------------------------


def test_merged_pr_transitions_task_to_merged(env):
    _seed_dispatched(env["conn"], identifier="T0-1", pr_number=42)
    env["client"].add_pr(
        "owner/repo",
        _pr(42, "feat/sa-t0-1", state=PRState.MERGED,
            check=PRCheckStatus.SUCCESS),
    )

    report = watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    assert report.tasks_transitioned_merged == 1
    row = env["conn"].execute(
        "SELECT status, merged_at FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "merged"
    assert row["merged_at"] is not None


def test_closed_unmerged_pr_transitions_task_to_failed(env):
    _seed_dispatched(env["conn"], identifier="T0-1", pr_number=42)
    env["client"].add_pr(
        "owner/repo",
        _pr(42, "feat/sa-t0-1", state=PRState.CLOSED,
            check=PRCheckStatus.FAILURE),
    )

    report = watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    assert report.tasks_transitioned_failed == 1
    row = env["conn"].execute(
        "SELECT status, failure_reason FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["failure_reason"] == "pr_closed_unmerged"


def test_open_pr_stays_dispatched_and_stamps_freshness(env):
    task_id = _seed_dispatched(env["conn"], identifier="T0-1", pr_number=42)
    old_row = env["conn"].execute(
        "SELECT last_progress_at FROM task WHERE id=?", (task_id,)
    ).fetchone()
    # Simulate an old last_progress_at.
    env["conn"].execute(
        "UPDATE task SET last_progress_at = '2020-01-01 00:00:00' WHERE id=?",
        (task_id,),
    )
    env["conn"].commit()

    env["client"].add_pr(
        "owner/repo",
        _pr(42, "feat/sa-t0-1", state=PRState.OPEN,
            check=PRCheckStatus.PENDING),
    )

    watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    row = env["conn"].execute(
        "SELECT status, last_progress_at FROM task WHERE id=?", (task_id,)
    ).fetchone()
    assert row["status"] == "dispatched"
    # last_progress_at got bumped (heartbeat won't reap).
    assert row["last_progress_at"] != "2020-01-01 00:00:00"
    assert row["last_progress_at"] != old_row["last_progress_at"] or \
        row["last_progress_at"] is not None


def test_missing_pr_does_not_crash(env):
    """If gh returns None for a pr_number we're tracking, keep the task alive."""
    _seed_dispatched(env["conn"], identifier="T0-1", pr_number=42)
    # No PR added to the fake — get_pr returns None.

    report = watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    # No transitions.
    assert report.tasks_transitioned_merged == 0
    assert report.tasks_transitioned_failed == 0
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Engine stop
# ---------------------------------------------------------------------------


def test_watch_is_noop_when_stopping(env):
    _seed_dispatched(env["conn"], identifier="T0-1", pr_number=42)
    env["client"].add_pr(
        "owner/repo",
        _pr(42, "feat/sa-t0-1", state=PRState.MERGED),
    )
    env["state"].request_stop()

    report = watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    assert report == WatchReport(
        pr_numbers_stamped=0,
        tasks_transitioned_merged=0,
        tasks_transitioned_failed=0,
        open_prs_observed=0,
        unmatched_prs=0,
    )
    row = env["conn"].execute(
        "SELECT status FROM task WHERE identifier='T0-1'"
    ).fetchone()
    assert row["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Mixed batch
# ---------------------------------------------------------------------------


def test_watch_handles_mixed_batch(env):
    _seed_dispatched(env["conn"], identifier="T0-1")  # discovery
    _seed_dispatched(env["conn"], identifier="T0-2", pr_number=52)  # merged
    _seed_dispatched(env["conn"], identifier="T0-3", pr_number=53)  # still open

    env["client"].add_pr("owner/repo", _pr(51, "feat/sa-t0-1"))
    env["client"].add_pr("owner/repo", _pr(52, "feat/sa-t0-2", state=PRState.MERGED))
    env["client"].add_pr("owner/repo", _pr(53, "feat/sa-t0-3", state=PRState.OPEN,
                                             check=PRCheckStatus.SUCCESS))

    report = watch(
        env["conn"], env["state"],
        client=env["client"], repo=env["repo"], branch_prefix="feat/",
    )
    assert report.pr_numbers_stamped == 1
    assert report.tasks_transitioned_merged == 1
    # open_prs_observed counts all open PRs the client returned. T0-2 is merged so not open.
    assert report.open_prs_observed == 2


# ---------------------------------------------------------------------------
# Adapter-default pathways
# ---------------------------------------------------------------------------


def test_watch_defaults_repo_and_prefix_from_adapter(env):
    """Omitting repo + branch_prefix reads them from the active adapter."""
    _seed_dispatched(env["conn"], identifier="T0-1")
    env["client"].add_pr("owner/repo", _pr(42, "feat/sa-t0-1"))

    # No repo / branch_prefix passed — should pull from adapter.
    report = watch(
        env["conn"], env["state"], client=env["client"],
    )
    assert report.pr_numbers_stamped == 1


def test_watch_raises_when_adapter_has_no_branch_prefixes(env, monkeypatch):
    _seed_dispatched(env["conn"], identifier="T0-1")

    from oxi_core import adapter as adapter_mod

    class _BadAdapter(_Adapter):
        def branch_prefixes(self): return ()

    adapter_mod.clear_adapter()
    adapter_mod.register_adapter(
        _BadAdapter(db_path_value="unused")
    )
    with pytest.raises(ValueError, match="branch_prefixes"):
        watch(env["conn"], env["state"], client=env["client"])
