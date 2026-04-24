"""Tests for oxi_core.v3.kill — killswitch file handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3 import kill


@dataclass
class _Adapter:
    release_lock: str | None = None
    oxi_dir_: str = ".oxi"
    repo_root_: str | None = None

    def naming(self): return NamingConfig()
    def paths(self):
        return PathsConfig(
            release_lock_path=self.release_lock,
            oxi_dir=self.oxi_dir_,
            repo_root=self.repo_root_,
        )
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (DispatchHost(name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"),)
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


def test_path_reads_release_lock_from_adapter(tmp_path: Path):
    ksw = tmp_path / "KILLSWITCH"
    register_adapter(_Adapter(release_lock=str(ksw)))
    assert kill.path() == ksw


def test_path_falls_back_to_repo_root_oxi_dir(tmp_path: Path):
    register_adapter(
        _Adapter(release_lock=None, repo_root_=str(tmp_path), oxi_dir_=".oxi")
    )
    assert kill.path() == tmp_path / ".oxi" / "KILLSWITCH"


def test_is_set_false_when_absent(tmp_path: Path):
    register_adapter(_Adapter(release_lock=str(tmp_path / "KS")))
    assert kill.is_set() is False


def test_create_and_is_set(tmp_path: Path):
    ksw = tmp_path / "KILLSWITCH"
    register_adapter(_Adapter(release_lock=str(ksw)))
    kill.create(reason="testing")
    assert kill.is_set() is True
    assert "testing" in ksw.read_text()


def test_create_parents_dirs(tmp_path: Path):
    ksw = tmp_path / "nested" / "dir" / "KILLSWITCH"
    register_adapter(_Adapter(release_lock=str(ksw)))
    kill.create(reason="")
    assert ksw.exists()


def test_remove_returns_true_when_removed(tmp_path: Path):
    ksw = tmp_path / "KS"
    register_adapter(_Adapter(release_lock=str(ksw)))
    kill.create(reason="x")
    assert kill.remove() is True
    assert kill.is_set() is False


def test_remove_returns_false_when_not_present(tmp_path: Path):
    ksw = tmp_path / "KS"
    register_adapter(_Adapter(release_lock=str(ksw)))
    assert kill.remove() is False


def test_explicit_path_overrides_adapter(tmp_path: Path):
    register_adapter(_Adapter(release_lock=str(tmp_path / "ignored")))
    explicit = tmp_path / "explicit"
    kill.create(killswitch_path=explicit)
    assert explicit.exists()
    assert not (tmp_path / "ignored").exists()
    assert kill.is_set(killswitch_path=explicit) is True
