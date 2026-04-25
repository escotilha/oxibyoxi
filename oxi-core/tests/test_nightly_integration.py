"""Pytest harness for the nightly integration probe.

These tests call the real GitHub API via ``gh`` and are therefore
opt-in: they are only executed when the runner satisfies *all* of:

  - ``gh`` is on PATH
  - ``gh auth status`` exits 0 (i.e. a token is configured)
  - The ``OXI_RUN_INTEGRATION`` env var is set to ``"1"``

In CI the nightly workflow calls ``scripts/nightly-integration.py``
directly (faster start, no pytest overhead). This module exists so
developers can run the same probes locally from the familiar pytest
entry point and get structured failure output when something regresses.

How to run::

    OXI_RUN_INTEGRATION=1 pytest -m integration -v
    # or just:
    OXI_RUN_INTEGRATION=1 pytest tests/test_nightly_integration.py -v

Without ``OXI_RUN_INTEGRATION=1`` every test skips immediately — the
normal ``pytest -q`` suite is unaffected even if ``gh`` is available.

Read-only by design — no PR writes, no branches created.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from oxi_core.v3.github_client import (
    CheckRun,
    GhCliClient,
    PullRequest,
)

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

_OPT_IN = os.environ.get("OXI_RUN_INTEGRATION", "") == "1"
_GH_MISSING = shutil.which("gh") is None
_GH_UNAUTHED: bool = False

if _OPT_IN and not _GH_MISSING:
    _result = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False
    )
    _GH_UNAUTHED = _result.returncode != 0

pytestmark = pytest.mark.integration

# Target repo — use $OXI_NIGHTLY_REPO so the same env var that the
# nightly CI workflow sets works here too.
_REPO = os.environ.get("OXI_NIGHTLY_REPO", "escotilha/oxi")


def _require_live_gh() -> None:
    """Skip unless OXI_RUN_INTEGRATION=1 and gh is available + authenticated."""
    if not _OPT_IN:
        pytest.skip(
            "live GitHub probes disabled; set OXI_RUN_INTEGRATION=1 to enable"
        )
    if _GH_MISSING:
        pytest.skip("gh not on PATH")
    if _GH_UNAUTHED:
        pytest.skip("gh not authenticated (run `gh auth login`)")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_list_open_prs_returns_sequence() -> None:
    """``list_open_prs`` returns a tuple of ``PullRequest`` objects (may be empty).

    Validates the API shape — no breakage from a ``gh`` CLI upgrade or
    a GitHub response-schema change.
    """
    _require_live_gh()
    client = GhCliClient()
    prs = client.list_open_prs(_REPO)
    assert isinstance(prs, tuple), f"expected tuple, got {type(prs).__name__}"
    for pr in prs:
        assert isinstance(pr, PullRequest), (
            f"list member is {type(pr).__name__}, not PullRequest"
        )


def test_get_pr_roundtrips_to_dataclass() -> None:
    """``get_pr`` for the highest-numbered open PR returns a populated PullRequest.

    Checks that the PR fields (number, title, head_branch) survive the
    JSON → dataclass parse without coercion errors.
    """
    _require_live_gh()
    client = GhCliClient()
    prs = client.list_open_prs(_REPO)
    if not prs:
        pytest.skip(f"no open PRs in {_REPO}; cannot exercise get_pr")
    target = max(prs, key=lambda p: p.number)
    pr = client.get_pr(_REPO, target.number)
    assert isinstance(pr, PullRequest), (
        f"get_pr returned {type(pr).__name__}, not PullRequest"
    )
    assert pr.number == target.number
    assert isinstance(pr.title, str)
    assert isinstance(pr.head_branch, str)


def test_list_check_runs_returns_tuple_of_check_runs() -> None:
    """``list_check_runs`` returns a tuple of ``CheckRun`` objects (may be empty).

    Validates the check-run shape — name, status, and conclusion are
    strings (conclusion is empty when the check has not yet completed).
    """
    _require_live_gh()
    client = GhCliClient()
    prs = client.list_open_prs(_REPO)
    if not prs:
        pytest.skip(f"no open PRs in {_REPO}; cannot exercise list_check_runs")
    target = max(prs, key=lambda p: p.number)
    runs = client.list_check_runs(_REPO, target.number)
    assert isinstance(runs, tuple), (
        f"list_check_runs returned {type(runs).__name__}, not tuple"
    )
    for run in runs:
        assert isinstance(run, CheckRun), (
            f"tuple member is {type(run).__name__}, not CheckRun"
        )
        assert isinstance(run.status, str)
        assert isinstance(run.conclusion, str)
