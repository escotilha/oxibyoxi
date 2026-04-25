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
    """Pierre's explicit caps: soft=$25, hard=$100, opus=$20, sonnet=$0.50.

    Hard-cap raised from $20 to $100 on 2026-04-25 once parallel
    dispatch + auto_merge + 22-item plan ingest landed. per_task_opus
    raised 2.0 → 20.0 on 2026-04-25 PM after T1-B9 validation showed
    Opus 4.7 hitting the $2 cap mid-implementation. Any change to
    these numbers should be intentional and paired with a conversation
    with Pierre — not edited casually.
    """
    b = SelfAdapter(repo_root=tmp_path).budget()
    assert isinstance(b, BudgetCaps)
    assert b.daily_soft_warn == 25.0
    assert b.daily_hard_cap == 100.0
    assert b.per_task_opus == 20.0
    assert b.per_task_sonnet == 0.50


def test_target_repo_is_oxi(tmp_path: Path):
    a = SelfAdapter(repo_root=tmp_path)
    assert a.github_repo() == "escotilha/oxi"


def test_roadmap_is_docs_roadmap(tmp_path: Path):
    assert SelfAdapter(repo_root=tmp_path).roadmap_location() == "docs/roadmap.md"


def test_dispatch_host_is_local_ram_probed(tmp_path: Path):
    hosts = SelfAdapter(repo_root=tmp_path).dispatch_hosts()
    assert len(hosts) == 1
    host = hosts[0]
    assert isinstance(host, DispatchHost)
    assert host.name == "local"
    assert host.ssh_alias is None
    # Concurrency is probed from free RAM at call time; cap is at
    # HARDWARE_CONCURRENCY_CEILING=20 (raised from 10 once parallel
    # dispatch landed). Floor of 1 means even on a sandboxed/probe-
    # failed host the engine still ticks one at a time.
    assert 1 <= host.max_concurrent <= 20, \
        f"concurrency outside expected range: {host.max_concurrent}"


def test_dispatch_host_respects_oxi_max_concurrent_env(
    tmp_path: Path, monkeypatch
):
    """OXI_MAX_CONCURRENT bypasses the RAM probe."""
    monkeypatch.setenv("OXI_MAX_CONCURRENT", "3")
    host = SelfAdapter(repo_root=tmp_path).dispatch_hosts()[0]
    assert host.max_concurrent == 3


def test_dispatch_host_ignores_invalid_oxi_max_concurrent(
    tmp_path: Path, monkeypatch
):
    """A bogus env var falls back to the probe, not crashes."""
    monkeypatch.setenv("OXI_MAX_CONCURRENT", "not-a-number")
    host = SelfAdapter(repo_root=tmp_path).dispatch_hosts()[0]
    assert 1 <= host.max_concurrent <= 20


def test_plan_tier_is_20x(tmp_path: Path):
    """Pierre is on Max 20x — the engine routes quota accordingly."""
    assert SelfAdapter(repo_root=tmp_path).plan_tier() == "20x"


def test_auto_merge_is_on(tmp_path: Path):
    """Engine PRs auto-merge after the critic passes. Flipped 2026-04-25
    once the critic + CI track record was established across 90+ dogfood
    dispatches."""
    p = SelfAdapter(repo_root=tmp_path).policy()
    assert isinstance(p, DispatchPolicy)
    assert p.auto_merge is True


def test_promote_recipe_is_none(tmp_path: Path):
    """Dogfood has no staging/production split — PyPI releases are manual."""
    assert SelfAdapter(repo_root=tmp_path).promote_recipe() is None
