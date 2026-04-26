"""FakeGitHubClient — deterministic stub for pr_watcher/auto_merge/ci_issue_filer tests.

Lives under tests/fixtures so it's test-only infrastructure. The fake
lets tests pre-configure PR state (open, merged, closed, CI status,
individual check runs) and then observe what the production code does
against that state.
"""

from __future__ import annotations

from oxi_core.v3.github_client import (
    CheckRun,
    GitHubClient,
    MergedPR,
    PRCheckStatus,
    PRState,
    PullRequest,
)


class FakeGitHubClient:
    """In-memory GitHub. Supports the ``GitHubClient`` protocol."""

    def __init__(self) -> None:
        # repo → {pr_number: PullRequest}
        self._prs: dict[str, dict[int, PullRequest]] = {}
        self._merge_calls: list[tuple[str, int, str]] = []
        # repo → {pr_number: tuple[CheckRun, ...]}
        self._check_runs: dict[str, dict[int, tuple[CheckRun, ...]]] = {}
        # repo → list of MergedPR (for list_merged_prs stub)
        self._merged_prs: dict[str, list[MergedPR]] = {}

    # ---- Test setup helpers ------------------------------------------

    def add_pr(self, repo: str, pr: PullRequest) -> None:
        self._prs.setdefault(repo, {})[pr.number] = pr

    def set_pr_state(self, repo: str, pr_number: int, state: PRState) -> None:
        old = self._prs[repo][pr_number]
        self._prs[repo][pr_number] = PullRequest(
            number=old.number, title=old.title, head_branch=old.head_branch,
            state=state, check_status=old.check_status, mergeable=old.mergeable,
        )

    def set_check_status(
        self, repo: str, pr_number: int, status: PRCheckStatus
    ) -> None:
        old = self._prs[repo][pr_number]
        self._prs[repo][pr_number] = PullRequest(
            number=old.number, title=old.title, head_branch=old.head_branch,
            state=old.state, check_status=status, mergeable=old.mergeable,
        )

    def set_check_runs(
        self, repo: str, pr_number: int, runs: tuple[CheckRun, ...]
    ) -> None:
        """Pre-configure individual check runs for a PR.

        Used by ci_issue_filer tests to exercise per-check-run logic
        without touching real GitHub.
        """
        self._check_runs.setdefault(repo, {})[pr_number] = runs

    def add_merged_pr(self, repo: str, pr: MergedPR) -> None:
        """Pre-configure a merged PR for ``list_merged_prs`` to return."""
        self._merged_prs.setdefault(repo, []).append(pr)

    @property
    def merge_calls(self) -> tuple[tuple[str, int, str], ...]:
        """History of merge_pr invocations for test assertions."""
        return tuple(self._merge_calls)

    # ---- GitHubClient protocol ---------------------------------------

    def list_open_prs(
        self, repo: str, head_prefix: str | None = None
    ) -> tuple[PullRequest, ...]:
        by_num = self._prs.get(repo, {})
        open_prs = [p for p in by_num.values() if p.state is PRState.OPEN]
        if head_prefix is not None:
            open_prs = [p for p in open_prs if p.head_branch.startswith(head_prefix)]
        return tuple(open_prs)

    def get_pr(self, repo: str, pr_number: int) -> PullRequest | None:
        return self._prs.get(repo, {}).get(pr_number)

    def merge_pr(
        self, repo: str, pr_number: int, *, method: str = "squash"
    ) -> bool:
        self._merge_calls.append((repo, pr_number, method))
        pr = self._prs.get(repo, {}).get(pr_number)
        if pr is None:
            return False
        # Flip to merged.
        self.set_pr_state(repo, pr_number, PRState.MERGED)
        return True

    def list_check_runs(self, repo: str, pr_number: int) -> tuple[CheckRun, ...]:
        """Return pre-configured check runs, or an empty tuple if none set."""
        return self._check_runs.get(repo, {}).get(pr_number, ())

    def list_merged_prs(
        self,
        repo: str,
        *,
        since: str,
        limit: int = 100,
    ) -> tuple[MergedPR, ...]:
        """Return pre-configured merged PRs, filtered by ``since``.

        Only PRs whose ``merged_at`` >= ``since`` are returned, ordered
        newest-first.  The ``since`` comparison is lexicographic (ISO
        strings sort correctly).
        """
        all_prs = self._merged_prs.get(repo, [])
        filtered = [p for p in all_prs if p.merged_at >= since]
        filtered.sort(key=lambda p: p.merged_at, reverse=True)
        return tuple(filtered[:limit])


# Type-check that FakeGitHubClient satisfies the protocol.
_: GitHubClient = FakeGitHubClient()
