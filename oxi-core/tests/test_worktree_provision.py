"""Tests for oxi_core.v3.worktree_provision — real git, tmp repos.

These tests create actual git repositories in pytest's tmp_path and
operate on them. That's slower than mocking subprocess, but it's the
only way to catch the class of bug where git's behavior diverges from
our mental model (every past worktree-related incident has been one of
those).

Tests that assert on `origin` fetch behavior use a bare repo as origin
and a working clone as the caller — the same shape production uses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from oxi_core.v3.worktree_provision import (
    WorktreeError,
    WorktreeHandle,
    compose_branch_name,
    is_provisioned,
    provision,
    reap,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(args: list[str], cwd: Path) -> None:
    """Run a git/command and assert success. Raises on failure."""
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


def _make_upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare upstream repo + a clone. Return (upstream_dir, clone_dir).

    The clone has one commit on `main` with a single file.
    """
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _run(["git", "init", "--bare", "--initial-branch=main"], cwd=upstream)

    seed = tmp_path / "seed"
    seed.mkdir()
    _run(["git", "init", "--initial-branch=main"], cwd=seed)
    _run(["git", "config", "user.email", "test@example.com"], cwd=seed)
    _run(["git", "config", "user.name", "Test"], cwd=seed)
    (seed / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=seed)
    _run(["git", "commit", "-m", "initial"], cwd=seed)
    _run(["git", "remote", "add", "origin", str(upstream)], cwd=seed)
    _run(["git", "push", "-u", "origin", "main"], cwd=seed)

    clone = tmp_path / "clone"
    _run(["git", "clone", str(upstream), str(clone)], cwd=tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=clone)
    _run(["git", "config", "user.name", "Test"], cwd=clone)
    return upstream, clone


# ---------------------------------------------------------------------------
# compose_branch_name
# ---------------------------------------------------------------------------


def test_compose_branch_name_basic():
    assert compose_branch_name("sa", "T0-1") == "feat/sa-t0-1"


def test_compose_branch_name_respects_prefix():
    assert compose_branch_name("sb", "T0-1", prefix="fix/") == "fix/sb-t0-1"


def test_compose_branch_name_slugifies_identifier():
    assert compose_branch_name("sa", "T2-17 weird title") == "feat/sa-t2-17-weird-title"


def test_compose_branch_name_rejects_empty_session_tag():
    with pytest.raises(ValueError):
        compose_branch_name("", "T0-1")


def test_compose_branch_name_rejects_non_alphanumeric_session_tag():
    with pytest.raises(ValueError):
        compose_branch_name("s a", "T0-1")


def test_compose_branch_name_lowercases_tag():
    assert compose_branch_name("SA", "T0-1") == "feat/sa-t0-1"


# ---------------------------------------------------------------------------
# provision — happy paths
# ---------------------------------------------------------------------------


def test_provision_creates_worktree_on_fresh_branch(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"

    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )

    assert isinstance(handle, WorktreeHandle)
    assert handle.branch == "feat/sa-t0-1"
    assert handle.session_tag == "sa"
    assert handle.path.is_dir()
    # Worktree has README from the upstream.
    assert (handle.path / "README.md").exists()
    # Branch is checked out in the worktree.
    proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(handle.path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "feat/sa-t0-1"


def test_provision_returns_existing_handle_when_worktree_exists(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"

    first = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    second = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )

    assert first.path == second.path
    assert first.branch == second.branch


def test_provision_attaches_to_existing_branch_without_worktree(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"

    # Create the branch but don't attach a worktree.
    _run(["git", "branch", "feat/sa-t0-1"], cwd=clone)

    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    assert handle.branch == "feat/sa-t0-1"
    assert handle.path.is_dir()


# ---------------------------------------------------------------------------
# provision — error paths
# ---------------------------------------------------------------------------


def test_provision_raises_when_repo_root_missing(tmp_path: Path):
    with pytest.raises(WorktreeError, match="is not a directory"):
        provision(
            repo_root=tmp_path / "does-not-exist",
            worktree_root=tmp_path / "wt",
            task_identifier="T0-1",
            session_tag="sa",
        )


def test_provision_refuses_to_clobber_nonempty_target(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    conflict = worktree_root / "t0-1"
    conflict.mkdir(parents=True)
    (conflict / "random.txt").write_text("surprise\n")

    with pytest.raises(WorktreeError, match="refusing to clobber"):
        provision(
            repo_root=clone,
            worktree_root=worktree_root,
            task_identifier="T0-1",
            session_tag="sa",
        )


def test_provision_detects_branch_checked_out_at_wrong_path(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"

    # Provision at the expected path.
    provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    # Move the existing worktree to a different place, then try again.
    other = tmp_path / "elsewhere"
    _run(
        ["git", "worktree", "move", str(worktree_root / "t0-1"), str(other)],
        cwd=clone,
    )

    with pytest.raises(WorktreeError, match="already checked out"):
        provision(
            repo_root=clone,
            worktree_root=worktree_root,
            task_identifier="T0-1",
            session_tag="sa",
        )


# ---------------------------------------------------------------------------
# is_provisioned
# ---------------------------------------------------------------------------


def test_is_provisioned_true_after_provision(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    assert is_provisioned(clone, handle) is True


def test_is_provisioned_false_for_bogus_handle(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    handle = WorktreeHandle(
        path=tmp_path / "nonexistent",
        branch="feat/sa-never-created",
        session_tag="sa",
    )
    assert is_provisioned(clone, handle) is False


# ---------------------------------------------------------------------------
# reap
# ---------------------------------------------------------------------------


def test_reap_removes_worktree(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    assert handle.path.exists()

    reap(clone, handle)
    assert not handle.path.exists()


def test_reap_with_delete_branch_removes_branch_ref(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    reap(clone, handle, delete_branch=True)

    proc = subprocess.run(
        ["git", "branch", "--list", handle.branch],
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == ""


def test_reap_is_idempotent(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    reap(clone, handle)
    # Second reap should not raise.
    reap(clone, handle)


def test_reap_handles_manually_deleted_directory(tmp_path: Path):
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )

    # Simulate operator-manual cleanup: rm -rf on the worktree dir.
    import shutil as _shutil
    _shutil.rmtree(handle.path)

    # reap should clean up git's record of the orphaned worktree without raising.
    reap(clone, handle)


# ---------------------------------------------------------------------------
# Drift repair (T0-11)
# ---------------------------------------------------------------------------


def test_provision_repairs_worktree_drifted_to_unrelated_branch(tmp_path: Path):
    """Target dir exists as a worktree on an unrelated branch; provision should nuke & re-provision.

    Note: git does not allow a second worktree to check out a branch that
    is already checked out in the primary clone (e.g. ``main`` when the
    clone itself is on ``main``).  We simulate the equivalent scenario —
    the target slot is occupied by a clearly-wrong branch — using a
    dedicated branch name rather than ``main``.
    """
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    target = worktree_root / "t0-1"

    # Create a branch that represents a "wrong" occupant at the target path.
    _run(["git", "branch", "fix/unrelated"], cwd=clone)
    worktree_root.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", str(target), "fix/unrelated"], cwd=clone)
    assert (target / ".git").exists()

    # Now provision for T0-1 — the target slot is on a wrong branch.
    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )

    assert handle.branch == "feat/sa-t0-1"
    assert handle.path.is_dir()

    # Verify the worktree is now on the expected branch.
    proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(handle.path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "feat/sa-t0-1"


def test_provision_repairs_worktree_drifted_to_sibling_branch(tmp_path: Path):
    """Target dir exists as a worktree on a stale sibling task's branch; should repair."""
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    target = worktree_root / "t0-1"

    # Create a stale sibling branch and check it out at the target path.
    _run(["git", "branch", "feat/sa-t0-9"], cwd=clone)
    worktree_root.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", str(target), "feat/sa-t0-9"], cwd=clone)

    # Provision for T0-1; drift repair should kick in.
    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )

    assert handle.branch == "feat/sa-t0-1"
    proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(handle.path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "feat/sa-t0-1"


def test_provision_repairs_detached_head_worktree(tmp_path: Path):
    """Target dir exists as a worktree in detached-HEAD state; should repair."""
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"
    target = worktree_root / "t0-1"

    # Check out the worktree in detached-HEAD state.
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    worktree_root.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(target), head_sha], cwd=clone)

    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )

    assert handle.branch == "feat/sa-t0-1"
    proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(handle.path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "feat/sa-t0-1"


def test_provision_prunes_stale_records_before_add(tmp_path: Path):
    """Stale worktree admin records (dir deleted without git cleanup) don't block re-provision."""
    _, clone = _make_upstream_and_clone(tmp_path)
    worktree_root = tmp_path / "worktrees"

    # Provision then manually rm -rf the worktree dir without calling reap.
    handle = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    import shutil as _shutil
    _shutil.rmtree(handle.path)

    # At this point git's worktree list still has a stale record for the deleted dir.
    # Re-provisioning the same task should succeed because provision() prunes first.
    handle2 = provision(
        repo_root=clone,
        worktree_root=worktree_root,
        task_identifier="T0-1",
        session_tag="sa",
    )
    assert handle2.branch == "feat/sa-t0-1"
    assert handle2.path.is_dir()
