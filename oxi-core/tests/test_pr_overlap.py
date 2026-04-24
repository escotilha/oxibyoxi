"""Tests for oxi_core.v3.pr_overlap."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from oxi_core.v3.github_client import (
    PRCheckStatus,
    PRState,
    PullRequest,
)
from oxi_core.v3.pr_overlap import (
    OverlapChecker,
    OverlapReport,
    check_overlap,
)


@dataclass
class _FakePrFilesClient:
    """FakeGitHubClient-adjacent but simpler — just for pr_files tests."""

    files_by_pr: dict[int, tuple[str, ...]]
    calls: list[tuple[str, int]] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.calls is None:
            object.__setattr__(self, "calls", [])

    def pr_files(self, repo: str, pr_number: int) -> tuple[str, ...]:
        self.calls.append((repo, pr_number))
        return self.files_by_pr.get(pr_number, ())

    # GitHubClient protocol methods — not used by pr_overlap but the
    # type is checkable so we implement the shape.
    def list_open_prs(self, repo, head_prefix=None):
        return ()

    def get_pr(self, repo, pr_number):
        return None

    def merge_pr(self, repo, pr_number, *, method="squash"):
        return False


def _pr(number: int) -> PullRequest:
    return PullRequest(
        number=number, title="", head_branch="feat/x",
        state=PRState.OPEN, check_status=PRCheckStatus.SUCCESS,
        mergeable=True,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_no_overlap_when_planned_files_empty():
    client = _FakePrFilesClient(files_by_pr={1: ("a.py",)})
    checker = OverlapChecker(client=client, repo="owner/r")
    report = check_overlap(
        planned_files=(),
        open_pr_numbers=(1,),
        checker=checker,
    )
    assert report.overlaps is False
    assert report.overlapping_prs == ()


def test_no_overlap_when_no_open_prs():
    client = _FakePrFilesClient(files_by_pr={})
    checker = OverlapChecker(client=client, repo="owner/r")
    report = check_overlap(
        planned_files=("src/a.py", "src/b.py"),
        open_pr_numbers=(),
        checker=checker,
    )
    assert report.overlaps is False


def test_no_overlap_when_files_disjoint():
    client = _FakePrFilesClient(files_by_pr={
        1: ("src/other.py",),
        2: ("docs/thing.md",),
    })
    checker = OverlapChecker(client=client, repo="owner/r")
    report = check_overlap(
        planned_files=("src/a.py", "src/b.py"),
        open_pr_numbers=(1, 2),
        checker=checker,
    )
    assert report.overlaps is False
    assert report.overlapping_prs == ()


# ---------------------------------------------------------------------------
# Overlap detected
# ---------------------------------------------------------------------------


def test_overlap_single_pr():
    client = _FakePrFilesClient(files_by_pr={
        42: ("src/a.py", "src/b.py"),
    })
    checker = OverlapChecker(client=client, repo="owner/r")
    report = check_overlap(
        planned_files=("src/b.py", "src/c.py"),
        open_pr_numbers=(42,),
        checker=checker,
    )
    assert report.overlaps is True
    assert report.overlapping_prs == (42,)
    assert report.overlapping_files == ("src/b.py",)


def test_overlap_multiple_prs():
    client = _FakePrFilesClient(files_by_pr={
        10: ("src/a.py",),
        20: ("src/b.py",),
        30: ("docs/x.md",),
    })
    checker = OverlapChecker(client=client, repo="owner/r")
    report = check_overlap(
        planned_files=("src/a.py", "src/b.py"),
        open_pr_numbers=(10, 20, 30),
        checker=checker,
    )
    assert report.overlaps is True
    assert set(report.overlapping_prs) == {10, 20}
    assert set(report.overlapping_files) == {"src/a.py", "src/b.py"}


def test_overlap_files_are_sorted():
    """Output ordering is deterministic (sorted)."""
    client = _FakePrFilesClient(files_by_pr={
        1: ("z.py", "a.py", "m.py"),
    })
    checker = OverlapChecker(client=client, repo="owner/r")
    report = check_overlap(
        planned_files=("z.py", "a.py", "m.py"),
        open_pr_numbers=(1,),
        checker=checker,
    )
    assert report.overlapping_files == ("a.py", "m.py", "z.py")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_checker_caches_fetch_per_pr():
    client = _FakePrFilesClient(files_by_pr={1: ("a.py",)})
    checker = OverlapChecker(client=client, repo="owner/r")

    # Two calls for the same PR.
    check_overlap(
        planned_files=("a.py",),
        open_pr_numbers=(1,), checker=checker,
    )
    check_overlap(
        planned_files=("a.py",),
        open_pr_numbers=(1,), checker=checker,
    )
    # gh client was called exactly once.
    assert len(client.calls) == 1
    assert client.calls[0] == ("owner/r", 1)


def test_checker_fetches_each_pr_separately():
    client = _FakePrFilesClient(files_by_pr={1: ("a.py",), 2: ("b.py",)})
    checker = OverlapChecker(client=client, repo="owner/r")
    check_overlap(
        planned_files=("a.py",),
        open_pr_numbers=(1, 2), checker=checker,
    )
    pr_nums = [n for _, n in client.calls]
    assert sorted(pr_nums) == [1, 2]


# ---------------------------------------------------------------------------
# Client without pr_files
# ---------------------------------------------------------------------------


def test_client_without_pr_files_returns_no_overlap(caplog):
    """A GitHubClient without pr_files → the gate is fail-open.

    Prefer a missed overlap to an incorrect overlap block; auto_merge
    and the critic catch the actual bad merge.
    """
    @dataclass
    class _BareClient:
        def list_open_prs(self, repo, head_prefix=None): return ()
        def get_pr(self, repo, pr_number): return None
        def merge_pr(self, repo, pr_number, *, method="squash"): return False

    checker = OverlapChecker(client=_BareClient(), repo="owner/r")
    report = check_overlap(
        planned_files=("a.py",),
        open_pr_numbers=(1,),
        checker=checker,
    )
    assert report.overlaps is False


# ---------------------------------------------------------------------------
# OverlapReport dataclass
# ---------------------------------------------------------------------------


def test_report_is_frozen():
    from dataclasses import FrozenInstanceError
    r = OverlapReport(overlaps=False, overlapping_prs=(), overlapping_files=())
    with pytest.raises(FrozenInstanceError):
        r.overlaps = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# GhCliPrFilesClient — just type-level smoke
# ---------------------------------------------------------------------------


def test_gh_cli_pr_files_client_importable():
    """The class exists and extends GhCliClient."""
    from oxi_core.v3.github_client import GhCliClient
    from oxi_core.v3.pr_overlap import GhCliPrFilesClient
    assert issubclass(GhCliPrFilesClient, GhCliClient)
