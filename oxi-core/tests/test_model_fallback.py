"""Tests for the model-fallback chain in dispatch.

Covers:
  - _pick_model walks the right chain per plan tier
  - fallback_steps clamps at chain boundaries
  - _count_recent_rate_limit_failures returns the right strike count
  - DispatchResult.rate_limit_exhausted flag round-trips
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from oxi_core import db
from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3.dispatch import (
    _MODEL_CHAIN_OPUS_FIRST,
    _MODEL_CHAIN_SONNET_FIRST,
    _count_recent_rate_limit_failures,
    _pick_model,
)
from oxi_core.v3.dispatch_invoke import Classification, DispatchResult


@dataclass
class _StubAdapter:
    """Minimal adapter for tier-only tests."""

    plan_tier_value: str

    def naming(self) -> NamingConfig: return NamingConfig()
    def paths(self) -> PathsConfig: return PathsConfig(db_path="/tmp/oxi.db")
    def budget(self) -> BudgetCaps: return BudgetCaps()
    def github_repo(self) -> str: return "owner/repo"
    def roadmap_location(self) -> str: return "roadmap.md"
    def branch_prefixes(self) -> tuple[str, ...]: return ("feat/",)
    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (DispatchHost(name="local", ssh_alias=None,
                             max_concurrent=1, worktree_root="/tmp"),)
    def promote_recipe(self) -> None: return None
    def plan_tier(self) -> str: return self.plan_tier_value
    def policy(self) -> DispatchPolicy: return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def conn(tmp_path: Path):
    register_adapter(_StubAdapter(plan_tier_value="standard"))
    register_adapter._test_db_path = str(tmp_path / "oxi.db")  # noqa: SLF001
    handle = db.connect(Path(tmp_path / "oxi.db"))
    yield handle.connection
    handle.connection.close()


# ---------------------------------------------------------------------------
# _pick_model — chain walk
# ---------------------------------------------------------------------------


def test_pick_model_max_tier_starts_at_opus():
    a = _StubAdapter(plan_tier_value="max_20x")
    assert _pick_model(a, fallback_steps=0) == "claude-opus-4-7"
    assert _pick_model(a, fallback_steps=1) == "claude-sonnet-4-6"
    assert _pick_model(a, fallback_steps=2) == "claude-haiku-4-5-20251001"


def test_pick_model_max_tier_clamps_at_haiku():
    """Out-of-bounds steps clamp at the last (cheapest) model."""
    a = _StubAdapter(plan_tier_value="max_5x")
    assert _pick_model(a, fallback_steps=99) == "claude-haiku-4-5-20251001"


def test_pick_model_standard_tier_skips_opus():
    """Standard plan can't afford Opus — chain starts at Sonnet."""
    a = _StubAdapter(plan_tier_value="standard")
    assert _pick_model(a, fallback_steps=0) == "claude-sonnet-4-6"
    assert _pick_model(a, fallback_steps=1) == "claude-haiku-4-5-20251001"


def test_pick_model_standard_tier_clamps_at_haiku():
    a = _StubAdapter(plan_tier_value="standard")
    assert _pick_model(a, fallback_steps=99) == "claude-haiku-4-5-20251001"


def test_pick_model_unknown_tier_treated_as_standard():
    """Unknown plan strings fall through to the standard chain."""
    a = _StubAdapter(plan_tier_value="weirdo_plan")
    assert _pick_model(a, fallback_steps=0) == "claude-sonnet-4-6"


def test_pick_model_default_fallback_steps_is_zero():
    """Calling without fallback_steps returns the preferred model."""
    a = _StubAdapter(plan_tier_value="max_20x")
    assert _pick_model(a) == "claude-opus-4-7"


def test_pick_model_negative_steps_clamps_to_zero():
    """A bug somewhere shouldn't accidentally pick a wrong-direction model."""
    a = _StubAdapter(plan_tier_value="max_20x")
    assert _pick_model(a, fallback_steps=-5) == "claude-opus-4-7"


def test_chains_are_distinct():
    """Sanity: max chain has Opus, standard chain doesn't."""
    assert "claude-opus-4-7" in _MODEL_CHAIN_OPUS_FIRST
    assert "claude-opus-4-7" not in _MODEL_CHAIN_SONNET_FIRST


# ---------------------------------------------------------------------------
# _count_recent_rate_limit_failures
# ---------------------------------------------------------------------------


def _seed_task(conn, identifier: str = "T0-1") -> int:
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title) VALUES (?, 0, 'do thing')",
        (identifier,),
    )
    conn.commit()
    return cur.lastrowid


def test_count_returns_zero_for_task_with_no_history(conn):
    task_id = _seed_task(conn)
    assert _count_recent_rate_limit_failures(conn, task_id) == 0


def test_count_increments_per_flagged_event(conn):
    task_id = _seed_task(conn)
    for _ in range(2):
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) "
            "VALUES (?, 'dispatch_retryable', ?)",
            (task_id, json.dumps({"rate_limit_exhausted": True})),
        )
    conn.commit()
    assert _count_recent_rate_limit_failures(conn, task_id) == 2


def test_count_ignores_unflagged_retryables(conn):
    """Generic transient retries (SIGTERM, etc.) don't trigger fallback."""
    task_id = _seed_task(conn)
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) "
        "VALUES (?, 'dispatch_retryable', ?)",
        (task_id, json.dumps({"reason": "sigterm"})),
    )
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) "
        "VALUES (?, 'dispatch_retryable', ?)",
        (task_id, json.dumps({})),
    )
    conn.commit()
    assert _count_recent_rate_limit_failures(conn, task_id) == 0


def test_count_ignores_other_event_kinds(conn):
    """dispatch_failed and dispatch_succeeded shouldn't increment count."""
    task_id = _seed_task(conn)
    for kind in ("dispatch_failed", "dispatch_succeeded", "pr_observed"):
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, kind, json.dumps({"rate_limit_exhausted": True})),
        )
    conn.commit()
    assert _count_recent_rate_limit_failures(conn, task_id) == 0


def test_count_window_excludes_old_events(conn):
    task_id = _seed_task(conn)
    conn.execute(
        "INSERT INTO event (task_id, kind, payload, created_at) "
        "VALUES (?, 'dispatch_retryable', ?, datetime('now', '-10 hours'))",
        (task_id, json.dumps({"rate_limit_exhausted": True})),
    )
    conn.commit()
    # Default window is 6 hours.
    assert _count_recent_rate_limit_failures(conn, task_id) == 0
    # But within a wider window, it counts.
    assert _count_recent_rate_limit_failures(
        conn, task_id, window_hours=24
    ) == 1


def test_count_handles_malformed_json_payload(conn):
    """Bad JSON shouldn't crash — just skip the row."""
    task_id = _seed_task(conn)
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) "
        "VALUES (?, 'dispatch_retryable', 'not json')",
        (task_id,),
    )
    conn.commit()
    assert _count_recent_rate_limit_failures(conn, task_id) == 0


# ---------------------------------------------------------------------------
# DispatchResult.rate_limit_exhausted flag
# ---------------------------------------------------------------------------


def test_dispatch_result_flag_defaults_false():
    """Existing call sites that don't set the flag get False."""
    result = DispatchResult(
        classification=Classification.SUCCESS,
        exit_code=0,
        session_id="abc",
    )
    assert result.rate_limit_exhausted is False


def test_dispatch_result_flag_round_trips():
    result = DispatchResult(
        classification=Classification.RETRYABLE_TRANSIENT,
        exit_code=1,
        session_id="abc",
        rate_limit_exhausted=True,
    )
    assert result.rate_limit_exhausted is True
