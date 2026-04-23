"""Tests for oxi_core.v3.github_client — PR parsing + status rollup."""

from __future__ import annotations

from oxi_core.v3.github_client import (
    GhCliClient,
    PRCheckStatus,
    PRState,
    PullRequest,
    _rollup_to_status,
)

# ---------------------------------------------------------------------------
# PR state enum
# ---------------------------------------------------------------------------


def test_pr_state_has_three_values():
    assert {s.value for s in PRState} == {"open", "merged", "closed"}


def test_pr_check_status_has_four_values():
    assert {s.value for s in PRCheckStatus} == {
        "pending", "success", "failure", "unknown",
    }


# ---------------------------------------------------------------------------
# _rollup_to_status
# ---------------------------------------------------------------------------


def test_rollup_empty_is_unknown():
    assert _rollup_to_status([]) is PRCheckStatus.UNKNOWN


def test_rollup_all_success():
    rollup = [
        {"conclusion": "success"},
        {"conclusion": "success"},
    ]
    assert _rollup_to_status(rollup) is PRCheckStatus.SUCCESS


def test_rollup_neutral_and_skipped_count_as_success():
    rollup = [
        {"conclusion": "success"},
        {"conclusion": "neutral"},
        {"conclusion": "skipped"},
    ]
    assert _rollup_to_status(rollup) is PRCheckStatus.SUCCESS


def test_rollup_any_failure_wins():
    rollup = [
        {"conclusion": "success"},
        {"conclusion": "failure"},
        {"conclusion": "success"},
    ]
    assert _rollup_to_status(rollup) is PRCheckStatus.FAILURE


def test_rollup_cancelled_is_failure():
    assert _rollup_to_status([{"conclusion": "cancelled"}]) is PRCheckStatus.FAILURE


def test_rollup_timed_out_is_failure():
    assert _rollup_to_status([{"conclusion": "timed_out"}]) is PRCheckStatus.FAILURE


def test_rollup_any_in_progress_is_pending():
    rollup = [
        {"conclusion": "success"},
        {"status": "in_progress"},
    ]
    assert _rollup_to_status(rollup) is PRCheckStatus.PENDING


def test_rollup_queued_is_pending():
    assert _rollup_to_status([{"status": "queued"}]) is PRCheckStatus.PENDING


def test_rollup_state_field_fallback():
    """Commit-status checks use 'state' instead of 'conclusion'."""
    rollup = [
        {"state": "success"},
        {"state": "success"},
    ]
    assert _rollup_to_status(rollup) is PRCheckStatus.SUCCESS


def test_rollup_unknown_verdict_is_pending_conservative():
    """An unrecognized verdict → treat as pending, don't claim success."""
    assert _rollup_to_status([{"conclusion": "some-new-value"}]) is PRCheckStatus.PENDING


def test_rollup_failure_beats_pending():
    rollup = [
        {"status": "in_progress"},
        {"conclusion": "failure"},
    ]
    assert _rollup_to_status(rollup) is PRCheckStatus.FAILURE


# ---------------------------------------------------------------------------
# GhCliClient._parse_pr
# ---------------------------------------------------------------------------


def test_parse_pr_open():
    raw = {
        "number": 42,
        "title": "do the thing",
        "headRefName": "feat/sa-t0-1",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"conclusion": "success"}],
    }
    pr = GhCliClient._parse_pr(raw)
    assert isinstance(pr, PullRequest)
    assert pr.number == 42
    assert pr.state is PRState.OPEN
    assert pr.mergeable is True
    assert pr.check_status is PRCheckStatus.SUCCESS


def test_parse_pr_merged_not_mergeable():
    raw = {
        "number": 1,
        "title": "",
        "headRefName": "feat/sa-t0-1",
        "state": "MERGED",
        "mergeable": "CONFLICTING",
        "statusCheckRollup": [],
    }
    pr = GhCliClient._parse_pr(raw)
    assert pr.state is PRState.MERGED
    assert pr.mergeable is False


def test_parse_pr_mergeable_unknown_maps_to_none():
    raw = {
        "number": 1,
        "title": "",
        "headRefName": "feat/sa-t0-1",
        "state": "OPEN",
        "mergeable": "UNKNOWN",
        "statusCheckRollup": [],
    }
    pr = GhCliClient._parse_pr(raw)
    assert pr.mergeable is None


def test_parse_pr_unknown_state_defaults_to_open():
    """Forward-compatible: an unrecognized gh state label falls back to OPEN.

    That's the conservative choice — we'd rather leave a task in
    'dispatched' than misfire a terminal transition.
    """
    raw = {
        "number": 1,
        "title": "",
        "headRefName": "x",
        "state": "SOMETHING_NEW",
        "mergeable": None,
        "statusCheckRollup": [],
    }
    pr = GhCliClient._parse_pr(raw)
    assert pr.state is PRState.OPEN
