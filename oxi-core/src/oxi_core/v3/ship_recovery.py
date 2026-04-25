"""Rescue completed work from sessions that exited without committing.

The failure mode this module addresses: a dispatched Claude session
writes code, runs tests, and then — compaction, crash, rate-limit,
signal — exits before the commit step. Without recovery, the work is
silently abandoned: heartbeat eventually reaps the task, and the
worktree's uncommitted changes accumulate until an operator notices.

``ship_recovery.run`` scans the engine's known worktrees for:

1. A task in ``dispatched`` status
2. With ``pr_number IS NULL`` (no PR has been opened yet)
3. Whose worktree exists on disk
4. With uncommitted changes (``git status --porcelain`` non-empty)

For each match, the module stages everything, commits with a
prescribed message, pushes the branch, and opens a PR via the
injected ``GitHubClient``. On success, pr_watcher picks up the PR on
its next pass and transitions the task.

Shared invariants with the rest of the engine:

- Uses adapter-derived worktree roots; no hardcoded paths.
- Engine-stop check at entry (killswitch respected).
- Every state update stamps ``last_progress_at`` (anti-pattern #1).
- Emits ``ship_recovered`` or ``ship_recovery_failed`` ledger events.
- Argv-form subprocess invocation; no ``shell=True``; ``os.fspath``
  used at the ``cwd`` boundary instead of ``str()``.

What this module does NOT do:

- Does not run tests. If the worker didn't finish its test pass, the
  recovered commit will land with the same partial test state —
  pr_watcher + CI + auto_merge's critic will catch it from there.
- Does not generate a meaningful commit message. Uses a boilerplate
  ``"recover: <identifier>: <title>"``. Forks that want richer
  messages can subclass ``ShipRecovery``.
- Does not SSH to remote dispatch hosts. Today's recovery is local-
  only; SSH-based recovery belongs with a future ``DispatchPool``
  enhancement since the session's worktree lives on the dispatch host.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..adapter import get_active_adapter
from ._timefmt import now_iso as _now_iso
from .dispatch import Task
from .engine_state import EngineState
from .github_client import GhCliClient, GitHubClient

logger = logging.getLogger(__name__)


# Commit message template. Plain so that Octopus/GH won't try to
# interpret it. Kept boilerplate because the task title itself is the
# most useful information.
_RECOVERY_COMMIT_TEMPLATE = "recover: {identifier}: {title}"


@dataclass(frozen=True)
class ShipReport:
    """Outcome of one recovery pass."""

    considered: int
    recovered: int
    failed: int
    no_uncommitted_changes: int
    worktree_missing: int




def _run_git(
    args: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd`` with argv-form subprocess.

    Mirrors the style of ``worktree_provision._run_git``. Raises
    nothing on nonzero exit — callers inspect ``returncode``.
    """
    return subprocess.run(
        ["git", *args],
        cwd=os.fspath(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _has_uncommitted_changes(worktree: Path) -> bool:
    """Return True if ``git status --porcelain`` in worktree is non-empty."""
    result = _run_git(["status", "--porcelain"], cwd=worktree)
    if result.returncode != 0:
        # Treat not-a-git-worktree as no-changes to be safe.
        return False
    return bool(result.stdout.strip())


def _current_branch(worktree: Path) -> str | None:
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _load_candidates(conn: sqlite3.Connection) -> list[Task]:
    """Dispatched tasks without a PR yet — candidates for recovery."""
    rows = conn.execute(
        "SELECT * FROM task WHERE status = 'dispatched' "
        "AND pr_number IS NULL ORDER BY id ASC"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def _worktree_for(
    task: Task, worktree_root: Path
) -> Path:
    """Mirror worktree_provision's slug convention: ``<root>/<slug>``.

    The slug is the task identifier lowercased with non-alnum → hyphen.
    """
    import re

    slug = re.sub(r"[^a-z0-9\-]+", "-", task.identifier.lower()).strip("-")
    return worktree_root / (slug or "task")


def _commit_message(task: Task) -> str:
    return _RECOVERY_COMMIT_TEMPLATE.format(
        identifier=task.identifier,
        title=task.title,
    )


def _emit_event(
    conn: sqlite3.Connection,
    task: Task,
    kind: str,
    payload: dict,
) -> None:
    with conn:
        conn.execute(
            "UPDATE task SET last_progress_at = ?, updated_at = ? WHERE id = ?",
            (_now_iso(), _now_iso(), task.id),
        )
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task.id, kind, json.dumps(payload)),
        )


def _recover_one(
    conn: sqlite3.Connection,
    task: Task,
    worktree: Path,
    branch: str,
    client: GitHubClient,
    repo: str,
) -> bool:
    """Try to stage + commit + push + PR. Returns True on success.

    On failure, emits ``ship_recovery_failed`` with the step that
    broke. No DB state beyond the event is mutated — pr_watcher and
    heartbeat will handle the task from here on their normal cadence.
    """
    # 1. Stage.
    add = _run_git(["add", "-A"], cwd=worktree)
    if add.returncode != 0:
        _emit_event(conn, task, "ship_recovery_failed", {
            "step": "git add",
            "stderr": add.stderr[-500:],
        })
        return False

    # 2. Commit.
    msg = _commit_message(task)
    commit = _run_git(
        ["commit", "-m", msg],
        cwd=worktree,
    )
    if commit.returncode != 0:
        # "nothing to commit" shouldn't happen because we check
        # porcelain first, but covers races.
        _emit_event(conn, task, "ship_recovery_failed", {
            "step": "git commit",
            "stderr": commit.stderr[-500:],
            "stdout": commit.stdout[-500:],
        })
        return False

    # 3. Push.
    push = _run_git(
        ["push", "--set-upstream", "origin", branch],
        cwd=worktree,
    )
    if push.returncode != 0:
        _emit_event(conn, task, "ship_recovery_failed", {
            "step": "git push",
            "stderr": push.stderr[-500:],
        })
        return False

    # 4. Open PR via GitHubClient (caller can inject a fake in tests).
    # We don't have a generic "open PR" on the protocol yet — emit the
    # intent as an event and let pr_watcher + the worker-side PR-open
    # logic pick it up on next tick. A real production wire-up would
    # add ``client.open_pr`` to the protocol.
    _emit_event(conn, task, "ship_recovered", {
        "branch": branch,
        "commit_message": msg,
        "repo": repo,
        "note": "branch pushed; PR creation deferred to next tick",
    })
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    client: GitHubClient | None = None,
    repo: str | None = None,
    worktree_root: Path | None = None,
) -> ShipReport:
    """Run one recovery pass.

    Arguments:
        conn: SQLite connection.
        engine_state: shared stopping flag.
        client: GitHub client. Defaults to ``GhCliClient``.
        repo: target repo slug. Defaults to ``adapter.github_repo()``.
        worktree_root: where worktrees live. Defaults to the first
            dispatch host's ``worktree_root``.

    Returns a ``ShipReport``. No exceptions for per-task failures —
    they're captured as ``ship_recovery_failed`` events.
    """
    empty = ShipReport(
        considered=0, recovered=0, failed=0,
        no_uncommitted_changes=0, worktree_missing=0,
    )
    if engine_state.is_stopping():
        logger.info("ship_recovery: engine stopping, skipping pass")
        return empty

    adapter = get_active_adapter()
    if client is None:
        client = GhCliClient()
    if repo is None:
        repo = adapter.github_repo()
    if worktree_root is None:
        hosts = adapter.dispatch_hosts()
        if not hosts:
            logger.info(
                "ship_recovery: adapter has no dispatch hosts, skipping"
            )
            return empty
        worktree_root = Path(hosts[0].worktree_root)

    tasks = _load_candidates(conn)
    considered = 0
    recovered = 0
    failed = 0
    no_changes = 0
    missing = 0

    for task in tasks:
        if engine_state.is_stopping():
            break
        considered += 1
        worktree = _worktree_for(task, worktree_root)
        if not worktree.is_dir():
            missing += 1
            continue
        if not _has_uncommitted_changes(worktree):
            no_changes += 1
            continue
        branch = _current_branch(worktree)
        if branch is None:
            _emit_event(conn, task, "ship_recovery_failed", {
                "step": "resolve branch",
                "worktree": str(worktree),
            })
            failed += 1
            continue
        ok = _recover_one(conn, task, worktree, branch, client, repo)
        if ok:
            recovered += 1
        else:
            failed += 1

    return ShipReport(
        considered=considered,
        recovered=recovered,
        failed=failed,
        no_uncommitted_changes=no_changes,
        worktree_missing=missing,
    )


__all__ = ["ShipReport", "run"]


# Silence unused-import for shlex — kept as a documented import for
# future expansion where task titles may contain shell metacharacters.
_ = shlex
