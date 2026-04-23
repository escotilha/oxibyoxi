"""Tests for the reference adapter.

Covers: protocol conformance, every method returns a real value,
registration works end-to-end against oxi_core, and driving the
planner against the real sample-project roadmap yields tasks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from oxi_core import db, planner
from oxi_core.adapter import (
    Adapter,
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    get_active_adapter,
    register_adapter,
)

from oxi_adapter_reference import ReferenceAdapter


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_reference_adapter_conforms_to_protocol(tmp_path: Path):
    assert isinstance(ReferenceAdapter(repo_root=tmp_path), Adapter)


def test_register_reference_adapter_succeeds(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    register_adapter(a)
    assert get_active_adapter() is a


# ---------------------------------------------------------------------------
# Method return values
# ---------------------------------------------------------------------------


def test_naming_returns_reference_instance_name(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    assert isinstance(a.naming(), NamingConfig)
    assert a.naming().instance_name == "reference"


def test_paths_derived_from_repo_root(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    p = a.paths()
    assert isinstance(p, PathsConfig)
    assert p.repo_root == str(tmp_path)
    assert p.db_path == str(tmp_path / ".oxi" / "oxi.db")
    assert p.brief_path == str(tmp_path / ".oxi" / "brief.md")


def test_budget_is_conservative(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    b = a.budget()
    assert isinstance(b, BudgetCaps)
    # These must be small — this adapter should never cost real money.
    assert b.daily_hard_cap <= 10.0
    assert b.per_task_opus <= 1.0


def test_github_repo_is_placeholder(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    assert a.github_repo() == "owner/sample-project"


def test_roadmap_location_is_roadmap_md(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    assert a.roadmap_location() == "roadmap.md"


def test_branch_prefixes_are_conventional(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    prefixes = a.branch_prefixes()
    assert "feat/" in prefixes
    assert "fix/" in prefixes


def test_dispatch_hosts_is_single_local(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    hosts = a.dispatch_hosts()
    assert len(hosts) == 1
    host = hosts[0]
    assert isinstance(host, DispatchHost)
    assert host.name == "local"
    assert host.ssh_alias is None
    assert host.max_concurrent == 1


def test_promote_recipe_is_none(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    assert a.promote_recipe() is None


def test_plan_tier_is_explicit_standard(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    assert a.plan_tier() == "standard"


def test_policy_defaults_conservative(tmp_path: Path):
    a = ReferenceAdapter(repo_root=tmp_path)
    p = a.policy()
    assert isinstance(p, DispatchPolicy)
    # auto_merge off by default — the reference adapter is for demos,
    # not for shipping to a real repo.
    assert p.auto_merge is False
    assert p.tier_zero_absorb is True


# ---------------------------------------------------------------------------
# Registration does NOT happen at import
# ---------------------------------------------------------------------------


def test_importing_package_does_not_register_adapter():
    """Anti-pattern guard: importing a package must never register an adapter.

    If it did, any transitive dependency could silently take over the
    engine's configuration.
    """
    clear_adapter()
    # Re-import the package fresh.
    for mod_name in [m for m in list(sys.modules) if m.startswith("oxi_adapter_reference")]:
        del sys.modules[mod_name]
    import oxi_adapter_reference  # noqa: F401

    from oxi_core.adapter import AdapterNotRegisteredError

    with pytest.raises(AdapterNotRegisteredError):
        get_active_adapter()


# ---------------------------------------------------------------------------
# End-to-end against the real sample-project
# ---------------------------------------------------------------------------


def _find_sample_project_root() -> Path | None:
    """Walk up from this file to find the repo root, then sample-project/."""
    here = Path(__file__).resolve()
    for ancestor in [here, *here.parents]:
        candidate = ancestor / "sample-project"
        if (candidate / "roadmap.md").is_file():
            return candidate
    return None


def test_end_to_end_drives_planner_against_sample_project(tmp_path: Path):
    """Register the adapter, run the planner, assert tasks land in the DB."""
    sample_root = _find_sample_project_root()
    if sample_root is None:
        pytest.skip("sample-project/roadmap.md not found near this test")

    # Point db at tmp to avoid writing into the repo.
    class _ShimAdapter(ReferenceAdapter):
        """Override db_path to live in tmp so tests don't leak DBs into the repo."""

        def paths(self) -> PathsConfig:
            p = super().paths()
            return PathsConfig(
                repo_root=p.repo_root,
                oxi_dir=p.oxi_dir,
                db_path=str(tmp_path / "oxi.db"),
                brief_path=p.brief_path,
                dashboard_path=p.dashboard_path,
                release_lock_path=p.release_lock_path,
                daily_recap_path=p.daily_recap_path,
            )

    register_adapter(_ShimAdapter(repo_root=sample_root))
    handle = db.connect()
    try:
        n = planner.plan(sample_root, handle.connection)
        assert n >= 1
        ids = {
            row[0]
            for row in handle.connection.execute("SELECT identifier FROM task")
        }
        # Sample-project roadmap lists at least T0-1.
        assert "T0-1" in ids
    finally:
        handle.connection.close()
