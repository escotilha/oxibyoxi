"""Git worktree lifecycle.

Every dispatched task runs in its own worktree off the target repo.
Worktrees are identified by task identifier + a session tag; the
branch name follows the adapter's `branch_prefixes` convention.

Responsibilities:

- Provision: create a worktree for a task, on a fresh branch off the
  default branch, at a predictable path under
  `<dispatch_host.worktree_root>/<task_identifier>`.
- Reap: remove a worktree when a task reaches a terminal state
  (merged, abandoned, failed).
- Validate: tell the caller whether a worktree exists and whether
  its checked-out branch matches what we expect.

Nothing here spawns Claude. The worktree is set up and handed off.

This module is intentionally thin. Git commands are shelled out via
`subprocess.run`; no GitPython dependency. The trade-off: less
friendly error handling vs. one fewer package in the dep tree. For an
orchestrator that ships 20 parallel workers, startup time matters more
than the convenience of GitPython.

Branch-name collisions are detected before the worktree is created; if
a worktree for the same task already exists on disk, `provision()`
returns the existing handle rather than duplicating.

Drift repair (T0-11):

If the target directory exists but the branch checked out there does
not match the expected feature branch — because it's on ``main``, in a
detached-HEAD state, or on a stale sibling task's branch — the old
code surfaced a ``WorktreeError`` and the dispatch died.  The repaired
path instead:

1. Runs ``git worktree prune`` before every ``worktree add`` to evict
   stale administrative records left by previous manual or crash
   cleanups.
2. Inspects HEAD inside the target directory with
   ``git rev-parse --abbrev-ref HEAD``.
3. If the branch has drifted, forces the worktree out via
   ``git worktree remove --force`` (falling back to ``shutil.rmtree``
   if that also fails), then re-provisions from scratch on the correct
   branch.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BRANCH_PREFIX = "feat/"


class WorktreeError(RuntimeError):
    """Raised when a worktree operation fails in a way that isn't recoverable."""


@dataclass(frozen=True)
class WorktreeHandle:
    """A provisioned worktree ready for dispatch to run in.

    `path` is the worktree directory on disk. `branch` is the branch
    checked out there. `session_tag` is the two-letter concurrent-session
    identifier embedded in the branch name.
    """

    path: Path
    branch: str
    session_tag: str


# ---------------------------------------------------------------------------
# Branch-name composition
# ---------------------------------------------------------------------------


_SAFE_SLUG = re.compile(r"[^a-z0-9\-]+")


def _slugify(text: str) -> str:
    """Turn arbitrary text into a branch-safe slug.

    Lowercase, hyphens only, collapse runs of non-alphanumerics to one
    hyphen, strip leading/trailing hyphens. Empty result falls back to
    'task'.
    """
    slug = _SAFE_SLUG.sub("-", text.lower()).strip("-")
    return slug or "task"


def compose_branch_name(
    session_tag: str, task_identifier: str, *, prefix: str = DEFAULT_BRANCH_PREFIX
) -> str:
    """Compose a branch name following the `<prefix><tag>-<slug>` convention.

    Example: \\`feat/sa-t0-1\\` for session 'sa' and task 'T0-1'.
    """
    tag = session_tag.strip().lower()
    if not tag or not tag.isalnum():
        raise ValueError(f"session tag must be non-empty alphanumeric, got {session_tag!r}")
    slug = _slugify(task_identifier)
    return f"{prefix}{tag}-{slug}"


# ---------------------------------------------------------------------------
# Git subprocess shims (testable)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` and return the completed process.

    Raises `WorktreeError` with stderr on nonzero exit.
    """
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed or not on PATH") from exc

    if proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def _default_branch(repo_root: Path) -> str:
    """Resolve the default branch of the target repo.

    Tries `origin/HEAD` first, falls back to `main`, then `master`.
    """
    try:
        proc = _run_git(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo_root
        )
        # e.g. `origin/main` — strip the remote prefix.
        ref = proc.stdout.strip()
        if "/" in ref:
            return ref.split("/", 1)[1]
        return ref
    except WorktreeError:
        pass

    for candidate in ("main", "master"):
        try:
            _run_git(["rev-parse", "--verify", candidate], cwd=repo_root)
            return candidate
        except WorktreeError:
            continue

    raise WorktreeError(
        f"could not determine default branch in {repo_root} (no origin/HEAD, no main, no master)"
    )


# ---------------------------------------------------------------------------
# Existing-worktree detection
# ---------------------------------------------------------------------------


def _existing_worktree_for_branch(
    repo_root: Path, branch: str
) -> Path | None:
    """Return the path to an existing worktree for `branch`, or None."""
    proc = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)

    current_path: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ").strip())
        elif line.startswith("branch "):
            ref = line.removeprefix("branch ").strip()
            # Porcelain format reports full refs like `refs/heads/feat/sa-t0-1`.
            if ref == f"refs/heads/{branch}" and current_path is not None:
                return current_path
    return None


def _head_branch(worktree_path: Path) -> str:
    """Return the branch name checked out in ``worktree_path``.

    Returns ``HEAD`` when in detached-HEAD state (``rev-parse
    --abbrev-ref`` prints ``HEAD`` for detached checkouts).  Returns
    an empty string if the path is not a git worktree at all.
    """
    try:
        proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path)
        return proc.stdout.strip()
    except WorktreeError:
        return ""


def _force_remove_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Forcibly remove a worktree directory and prune its git record.

    Tries ``git worktree remove --force`` first (clean removal that
    updates git's internal state).  Falls back to ``shutil.rmtree``
    when git can't locate the worktree in its records (e.g. it was
    already pruned or the record is corrupt), then runs ``worktree
    prune`` to flush any lingering administrative state.
    """
    try:
        _run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=repo_root)
    except WorktreeError:
        shutil.rmtree(worktree_path, ignore_errors=True)

    # Always prune so git's administrative view matches disk.
    try:
        _run_git(["worktree", "prune"], cwd=repo_root)
    except WorktreeError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def provision(
    *,
    repo_root: Path,
    worktree_root: Path,
    task_identifier: str,
    session_tag: str,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> WorktreeHandle:
    """Create (or return) a worktree for the given task.

    Behavior:

    - Branch name is \\`<prefix><session_tag>-<slug(task_identifier)>\\`.
    - Worktree directory is \\`<worktree_root>/<task_identifier>\\`.
    - If the branch already exists **and** a worktree is checked out
      for it, returns the existing handle unchanged.
    - If the branch exists but no worktree is attached, attaches one
      to the existing branch.
    - If neither exists, creates both (new branch off \\`origin/<default>\\`).
    - The target directory is \\`mkdir -p\\`ed; any existing non-git
      content under that path raises \\`WorktreeError\\` rather than being
      clobbered.
    - **Drift repair**: if the target directory already exists as a git
      worktree but is checked out on the wrong branch (main, detached
      HEAD, or a stale sibling branch), the drifted worktree is nuked
      and re-provisioned on the correct branch rather than raising
      ``WorktreeError``.
    - \\`git worktree prune\\` is run before every \\`worktree add\\` to
      evict stale administrative records left by crashes or manual
      cleanups.
    """
    if not repo_root.is_dir():
        raise WorktreeError(f"repo_root {repo_root} is not a directory")

    branch = compose_branch_name(
        session_tag, task_identifier, prefix=branch_prefix
    )
    target = worktree_root / _slugify(task_identifier)
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Prune stale worktree records up front so that subsequent checks
    # (`worktree list`, `worktree add`) operate on a consistent view.
    # A stale record — e.g. left by a crash or manual rm -rf — would
    # otherwise make `_existing_worktree_for_branch` believe the branch
    # is still attached, returning a path that no longer exists on disk.
    try:
        _run_git(["worktree", "prune"], cwd=repo_root)
    except WorktreeError:
        pass

    # If a worktree for this branch already exists, reuse it.
    existing = _existing_worktree_for_branch(repo_root, branch)
    if existing is not None:
        if existing.resolve() != target.resolve():
            raise WorktreeError(
                f"branch {branch} is already checked out at {existing}, "
                f"not at expected path {target}"
            )
        return WorktreeHandle(path=existing, branch=branch, session_tag=session_tag)

    # Drift-repair: if the target directory already exists as a git
    # worktree but is on a different branch, nuke and re-provision.
    if target.exists() and (target / ".git").exists():
        actual_branch = _head_branch(target)
        if actual_branch != branch:
            logger.warning(
                "worktree drift detected at %s: expected branch %r, found %r — "
                "nuking and re-provisioning",
                target,
                branch,
                actual_branch,
            )
            _force_remove_worktree(repo_root, target)

    # If target path exists and is non-empty non-worktree content, refuse.
    if target.exists() and any(target.iterdir()) and not (target / ".git").exists():
        raise WorktreeError(
            f"target worktree path {target} exists and is not empty; refusing to clobber"
        )

    # Fresh fetch so we branch from the latest origin tip.
    _run_git(["fetch", "origin"], cwd=repo_root)
    default = _default_branch(repo_root)

    # If the branch ref already exists locally, attach a worktree to it.
    branch_exists = True
    try:
        _run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo_root)
    except WorktreeError:
        branch_exists = False

    if branch_exists:
        _run_git(
            ["worktree", "add", str(target), branch],
            cwd=repo_root,
        )
    else:
        _run_git(
            ["worktree", "add", "-b", branch, str(target), f"origin/{default}"],
            cwd=repo_root,
        )

    return WorktreeHandle(path=target, branch=branch, session_tag=session_tag)


def reap(repo_root: Path, handle: WorktreeHandle, *, delete_branch: bool = False) -> None:
    """Remove the worktree. Optionally delete the branch ref.

    This is the terminal-state cleanup. Caller decides whether the
    branch survives (e.g. merged branches should NOT be deleted by
    oxi; GitHub's auto-branch-delete setting handles that upstream).
    """
    # `git worktree remove` is the supported way to detach + delete the dir.
    # --force because the worker may have left stale changes behind.
    if handle.path.exists():
        try:
            _run_git(
                ["worktree", "remove", "--force", str(handle.path)], cwd=repo_root
            )
        except WorktreeError:
            # Filesystem path may already be gone (operator cleanup, crash).
            # Fall through to prune.
            if handle.path.exists():
                shutil.rmtree(handle.path, ignore_errors=True)

    # Prune stale worktree records.
    _run_git(["worktree", "prune"], cwd=repo_root)

    if delete_branch:
        try:
            _run_git(["branch", "-D", handle.branch], cwd=repo_root)
        except WorktreeError:
            # Branch may have been deleted already by an earlier reap or by GH.
            pass


def is_provisioned(repo_root: Path, handle: WorktreeHandle) -> bool:
    """Return True if the worktree exists on disk and points at the expected branch."""
    if not handle.path.is_dir():
        return False
    existing = _existing_worktree_for_branch(repo_root, handle.branch)
    return existing is not None and existing.resolve() == handle.path.resolve()


__all__ = [
    "DEFAULT_BRANCH_PREFIX",
    "WorktreeError",
    "WorktreeHandle",
    "compose_branch_name",
    "is_provisioned",
    "provision",
    "reap",
]
