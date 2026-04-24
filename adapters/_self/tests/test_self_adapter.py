"""Tests for the dogfood self-adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

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

from oxi_adapter_self import SelfAdapter


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


def test_self_adapter_conforms_to_protocol(tmp_path: Path):
    assert isinstance(SelfAdapter(repo_root=tmp_path), Adapter)


def test_register_self_adapter_succeeds(tmp_path: Path):
    a = SelfAdapter(repo_root=tmp_path)
    register_adapter(a)
    assert get_active_adapter() is a


def test_naming_is_oxi_dogfood(tmp_path: Path):
    a = SelfAdapter(repo_root=tmp_path)
    naming = a.naming()
    assert isinstance(naming, NamingConfig)
    assert naming.instance_name == "oxi-dogfood"


def test_paths_under_repo_root(tmp_path: Path):
    a = SelfAdapter(repo_root=tmp_path)
    p = a.paths()
    assert isinstance(p, PathsConfig)
    assert p.repo_root == str(tmp_path)
    assert p.db_path == str(tmp_path / ".oxi" / "oxi.db")


def test_budget_matches_pierre_hard_cap(tmp_path: Path):
    """Pierre's explicit caps: soft=$5, hard=$20, opus=$2, sonnet=$0.50.

    Any change to these numbers should be intentional and paired with a
    conversation with Pierre — not edited casually.
    """
    b = SelfAdapter(repo_root=tmp_path).budget()
    assert isinstance(b, BudgetCaps)
    assert b.daily_soft_warn == 5.0
    assert b.daily_hard_cap == 20.0
    assert b.per_task_opus == 2.0
    assert b.per_task_sonnet == 0.50


def test_target_repo_is_oxi(tmp_path: Path):
    a = SelfAdapter(repo_root=tmp_path)
    assert a.github_repo() == "escotilha/oxi"


def test_roadmap_is_docs_roadmap(tmp_path: Path):
    assert SelfAdapter(repo_root=tmp_path).roadmap_location() == "docs/roadmap.md"


def test_dispatch_host_is_serial_local(tmp_path: Path):
    hosts = SelfAdapter(repo_root=tmp_path).dispatch_hosts()
    assert len(hosts) == 1
    host = hosts[0]
    assert isinstance(host, DispatchHost)
    assert host.name == "local"
    assert host.ssh_alias is None
    assert host.max_concurrent == 1, "Dogfood loop MUST stay serial"


def test_plan_tier_is_20x(tmp_path: Path):
    """Pierre is on Max 20x — the engine routes quota accordingly."""
    assert SelfAdapter(repo_root=tmp_path).plan_tier() == "20x"


def test_auto_merge_is_off(tmp_path: Path):
    """Critical guardrail: Pierre reviews every dogfood PR until the
    critic track record is established."""
    p = SelfAdapter(repo_root=tmp_path).policy()
    assert isinstance(p, DispatchPolicy)
    assert p.auto_merge is False


def test_promote_recipe_is_none(tmp_path: Path):
    """Dogfood has no staging/production split — PyPI releases are manual."""
    assert SelfAdapter(repo_root=tmp_path).promote_recipe() is None
