"""Tests for oxi_core.v3.deep_fix — escalation for repeatedly-stuck tasks.

Pattern mirrors test_auto_merge / test_ship_recovery: minimal fake adapter,
real SQLite DB via ``db.connect()``, deterministic time overrides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
from oxi_core.v3.deep_fix import (
    DeepFixReport,
    EscalationConfig,
    EscalationRecipe,
    get_escalation_override,
    run,
)
from oxi_core.v3.engine_state import EngineState

# ---------------------------------------------------------------------------
# Fake adapter
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    db_path_value: str
    _escalation_config: EscalationConfig | None = None

    def naming(self):
        return NamingConfig()

    def paths(self):
        return PathsConfig(db_path=self.db_path_value)

    def budget(self):
        return BudgetCaps()

    def github_repo(self):
        return "owner/repo"

    def roadmap_location(self):
        return "roadmap.md"

    def branch_prefixes(self):
        return ("feat/",)

    def dispatch_hosts(self):
        return (
            DispatchHost(
                name="local", ssh_alias=None,
                max_concurrent=1, worktree_root="/tmp",
            ),
        )

    def promote_recipe(self):
        return None

    def plan_tier(self):
        return "standard"

    def policy(self):
        return DispatchPolicy()

    def escalation_config(self) -> EscalationConfig:
        if self._escalation_config is not None:
            return self._escalation_config
        return EscalationConfig()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def env(tmp_path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "state": EngineState(plan_tier="standard", max_concurrent=4),
    }
    handle.connection.close()


# ---------------------------------------------------------------------------
# DB helpers used in tests
# ---------------------------------------------------------------------------

_T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
_PAST = _T0 - timedelta(hours=2)  # well outside any cooldown


def _seed_failed(conn, identifier: str = "T0-1", title: str = "t") -> int:
    """Insert a task in ``failed`` status; return its id."""
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES (?, 0, ?, 'failed')",
        (identifier, title),
    )
    conn.commit()
    return cur.lastrowid


def _seed_planned(conn, identifier: str = "T0-2") -> int:
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES (?, 0, 't', 'planned')",
        (identifier,),
    )
    conn.commit()
    return cur.lastrowid


def _add_failure_events(
    conn, task_id: int, n: int = 3,
    *,
    kind: str = "dispatch_failed",
    created_at: str = "2024-01-01 10:00:00",
) -> None:
    for _ in range(n):
        conn.execute(
            "INSERT INTO event (task_id, kind, payload, created_at) "
            "VALUES (?, ?, '{}', ?)",
            (task_id, kind, created_at),
        )
    conn.commit()


def _config(
    *,
    threshold: int = 3,
    cooldown_minutes: int = 0,
    recipes: tuple[EscalationRecipe, ...] | None = None,
    enabled: bool = True,
) -> EscalationConfig:
    if recipes is None:
        recipes = (
            EscalationRecipe(
                model="claude-opus-4-5",
                prompt_note="escalation note",
                label="opus-escalation",
            ),
        )
    return EscalationConfig(
        failure_threshold=threshold,
        cooldown_minutes=cooldown_minutes,
        recipes=recipes,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Engine stop
# ---------------------------------------------------------------------------


def test_engine_stopping_is_noop(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(env["conn"], task_id, 5)
    env["state"].request_stop()

    report = run(env["conn"], env["state"], config=_config(), now=_T0)
    assert report == DeepFixReport(
        considered=0, escalated=0, cooldown_skipped=0,
        exhausted=0, below_threshold=0, disabled_skipped=0,
    )


# ---------------------------------------------------------------------------
# Disabled config
# ---------------------------------------------------------------------------


def test_disabled_config_returns_disabled_skipped(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(env["conn"], task_id, 5)

    report = run(env["conn"], env["state"], config=_config(enabled=False), now=_T0)
    assert report.disabled_skipped == 1
    assert report.escalated == 0
    # Task stays failed.
    row = env["conn"].execute(
        "SELECT status FROM task WHERE id = ?", (task_id,)
    ).fetchone()
    assert row[0] == "failed"


# ---------------------------------------------------------------------------
# Below threshold
# ---------------------------------------------------------------------------


def test_below_threshold_is_skipped(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(env["conn"], task_id, 2)  # threshold is 3

    report = run(env["conn"], env["state"], config=_config(threshold=3), now=_T0)
    assert report.below_threshold == 1
    assert report.escalated == 0


def test_exactly_at_threshold_triggers_escalation(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(
        env["conn"], task_id, 3,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )

    report = run(env["conn"], env["state"], config=_config(threshold=3), now=_T0)
    assert report.escalated == 1


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_cooldown_prevents_escalation(env):
    task_id = _seed_failed(env["conn"])
    # Failure events 5 minutes ago — within a 30-minute cooldown.
    recent = (_T0 - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    _add_failure_events(env["conn"], task_id, 3, created_at=recent)

    report = run(
        env["conn"], env["state"],
        config=_config(threshold=3, cooldown_minutes=30),
        now=_T0,
    )
    assert report.cooldown_skipped == 1
    assert report.escalated == 0


def test_after_cooldown_escalation_fires(env):
    task_id = _seed_failed(env["conn"])
    # Failure events 61 minutes ago — outside 60-minute cooldown.
    old = (_T0 - timedelta(minutes=61)).strftime("%Y-%m-%d %H:%M:%S")
    _add_failure_events(env["conn"], task_id, 3, created_at=old)

    report = run(
        env["conn"], env["state"],
        config=_config(threshold=3, cooldown_minutes=60),
        now=_T0,
    )
    assert report.escalated == 1
    assert report.cooldown_skipped == 0


# ---------------------------------------------------------------------------
# Happy path: escalation fires
# ---------------------------------------------------------------------------


def test_escalation_resets_task_to_planned(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(
        env["conn"], task_id, 3,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )

    run(env["conn"], env["state"], config=_config(threshold=3), now=_T0)

    row = env["conn"].execute(
        "SELECT status FROM task WHERE id = ?", (task_id,)
    ).fetchone()
    assert row[0] == "planned"


def test_escalation_emits_deep_fix_escalated_event(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(
        env["conn"], task_id, 3,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )

    run(env["conn"], env["state"], config=_config(threshold=3), now=_T0)

    kinds = [
        r[0] for r in env["conn"].execute(
            "SELECT kind FROM event WHERE task_id = ?", (task_id,)
        )
    ]
    assert "deep_fix_escalated" in kinds


def test_escalation_event_contains_model_and_label(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(
        env["conn"], task_id, 3,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )
    recipe = EscalationRecipe(
        model="claude-opus-4-5", prompt_note="try harder", label="opus-test"
    )

    run(
        env["conn"], env["state"],
        config=_config(threshold=3, recipes=(recipe,)),
        now=_T0,
    )

    row = env["conn"].execute(
        "SELECT payload FROM event WHERE task_id = ? AND kind = 'deep_fix_escalated'",
        (task_id,),
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["model_override"] == "claude-opus-4-5"
    assert payload["recipe_label"] == "opus-test"
    assert payload["failure_count"] == 3
    assert payload["escalation_index"] == 0


def test_escalation_clears_failure_reason(env):
    task_id = _seed_failed(env["conn"])
    env["conn"].execute(
        "UPDATE task SET failure_reason = 'dispatch_failed' WHERE id = ?",
        (task_id,),
    )
    env["conn"].commit()
    _add_failure_events(
        env["conn"], task_id, 3,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )

    run(env["conn"], env["state"], config=_config(threshold=3), now=_T0)

    row = env["conn"].execute(
        "SELECT failure_reason FROM task WHERE id = ?", (task_id,)
    ).fetchone()
    assert row[0] is None


def test_escalation_stamps_last_progress_at(env):
    task_id = _seed_failed(env["conn"])
    env["conn"].execute(
        "UPDATE task SET last_progress_at = '2020-01-01 00:00:00' WHERE id = ?",
        (task_id,),
    )
    env["conn"].commit()
    _add_failure_events(
        env["conn"], task_id, 3,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )

    run(env["conn"], env["state"], config=_config(threshold=3), now=_T0)

    row = env["conn"].execute(
        "SELECT last_progress_at FROM task WHERE id = ?", (task_id,)
    ).fetchone()
    assert row[0] != "2020-01-01 00:00:00"


# ---------------------------------------------------------------------------
# Multiple recipes
# ---------------------------------------------------------------------------


def test_second_escalation_uses_next_recipe(env):
    """After the first escalation, a second failure set triggers recipe[1]."""
    recipe0 = EscalationRecipe(model="claude-sonnet-4-6", label="sonnet-extra")
    recipe1 = EscalationRecipe(model="claude-opus-4-5", label="opus-full")

    task_id = _seed_failed(env["conn"])
    # Three failures to exceed threshold.
    _add_failure_events(
        env["conn"], task_id, 3,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )
    # One prior escalation event (recipe0 already consumed).
    env["conn"].execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, 'deep_fix_escalated', ?)",
        (task_id, json.dumps({"escalation_index": 0, "model_override": "claude-sonnet-4-6",
                               "recipe_label": "sonnet-extra", "prompt_note": ""})),
    )
    env["conn"].commit()

    run(
        env["conn"], env["state"],
        config=_config(
            threshold=3,
            recipes=(recipe0, recipe1),
        ),
        now=_T0,
    )

    rows = env["conn"].execute(
        "SELECT payload FROM event WHERE task_id = ? AND kind = 'deep_fix_escalated' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    payload = json.loads(rows[0])
    assert payload["model_override"] == "claude-opus-4-5"
    assert payload["recipe_label"] == "opus-full"
    assert payload["escalation_index"] == 1


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------


def test_exhausted_task_emits_deep_fix_exhausted_event(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(
        env["conn"], task_id, 5,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )
    # One recipe already consumed.
    env["conn"].execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, 'deep_fix_escalated', '{}')",
        (task_id,),
    )
    env["conn"].commit()

    report = run(
        env["conn"], env["state"],
        config=_config(threshold=3, recipes=(EscalationRecipe(model="m"),)),
        now=_T0,
    )
    assert report.exhausted == 1
    assert report.escalated == 0

    kinds = [
        r[0] for r in env["conn"].execute(
            "SELECT kind FROM event WHERE task_id = ?", (task_id,)
        )
    ]
    assert "deep_fix_exhausted" in kinds


def test_exhausted_event_not_duplicated(env):
    """A second pass does not emit a second ``deep_fix_exhausted`` event."""
    task_id = _seed_failed(env["conn"])
    _add_failure_events(
        env["conn"], task_id, 5,
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )
    env["conn"].execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, 'deep_fix_escalated', '{}')",
        (task_id,),
    )
    env["conn"].commit()

    cfg = _config(threshold=3, recipes=(EscalationRecipe(model="m"),))
    run(env["conn"], env["state"], config=cfg, now=_T0)
    run(env["conn"], env["state"], config=cfg, now=_T0)

    count = env["conn"].execute(
        "SELECT COUNT(*) FROM event WHERE task_id = ? AND kind = 'deep_fix_exhausted'",
        (task_id,),
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Only failed tasks are considered
# ---------------------------------------------------------------------------


def test_planned_tasks_are_ignored(env):
    planned_id = _seed_planned(env["conn"])
    _add_failure_events(env["conn"], planned_id, 5)

    report = run(env["conn"], env["state"], config=_config(threshold=1), now=_T0)
    assert report.considered == 0


def test_dispatched_tasks_are_ignored(env):
    cur = env["conn"].execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES ('T0-99', 0, 't', 'dispatched')"
    )
    env["conn"].commit()
    _add_failure_events(env["conn"], cur.lastrowid, 5)

    report = run(env["conn"], env["state"], config=_config(threshold=1), now=_T0)
    assert report.considered == 0


# ---------------------------------------------------------------------------
# get_escalation_override
# ---------------------------------------------------------------------------


def test_get_escalation_override_returns_none_when_no_event(env):
    task_id = _seed_failed(env["conn"])
    assert get_escalation_override(env["conn"], task_id) is None


def test_get_escalation_override_returns_recipe_from_event(env):
    task_id = _seed_failed(env["conn"])
    payload = {
        "model_override": "claude-opus-4-5",
        "prompt_note": "try harder",
        "recipe_label": "opus-test",
    }
    env["conn"].execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, 'deep_fix_escalated', ?)",
        (task_id, json.dumps(payload)),
    )
    env["conn"].commit()

    recipe = get_escalation_override(env["conn"], task_id)
    assert recipe is not None
    assert recipe.model == "claude-opus-4-5"
    assert recipe.prompt_note == "try harder"
    assert recipe.label == "opus-test"


def test_get_escalation_override_returns_most_recent(env):
    """If multiple escalation events exist, the latest one wins."""
    task_id = _seed_failed(env["conn"])
    for i, model in enumerate(["model-a", "model-b"]):
        payload = json.dumps({
            "model_override": model,
            "prompt_note": "",
            "recipe_label": f"r{i}",
        })
        env["conn"].execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, 'deep_fix_escalated', ?)",
            (task_id, payload),
        )
    env["conn"].commit()

    recipe = get_escalation_override(env["conn"], task_id)
    assert recipe is not None
    assert recipe.model == "model-b"


# ---------------------------------------------------------------------------
# Report dataclass is frozen
# ---------------------------------------------------------------------------


def test_report_is_frozen():
    from dataclasses import FrozenInstanceError
    r = DeepFixReport(
        considered=0, escalated=0, cooldown_skipped=0,
        exhausted=0, below_threshold=0, disabled_skipped=0,
    )
    with pytest.raises(FrozenInstanceError):
        r.escalated = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# auto_merge_rejected events count as failures
# ---------------------------------------------------------------------------


def test_auto_merge_rejected_counts_as_failure(env):
    task_id = _seed_failed(env["conn"])
    _add_failure_events(
        env["conn"], task_id, 3,
        kind="auto_merge_rejected",
        created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
    )

    report = run(env["conn"], env["state"], config=_config(threshold=3), now=_T0)
    assert report.escalated == 1


# ---------------------------------------------------------------------------
# Adapter's escalation_config() is read when config=None
# ---------------------------------------------------------------------------


def test_adapter_escalation_config_is_used_when_config_is_none(tmp_path):
    """When config=None, the module reads adapter.escalation_config()."""
    recipe = EscalationRecipe(model="claude-opus-4-5", label="via-adapter")
    adapter = _Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        _escalation_config=EscalationConfig(
            failure_threshold=2,
            cooldown_minutes=0,
            recipes=(recipe,),
        ),
    )
    register_adapter(adapter)
    handle = db.connect()
    try:
        task_id = handle.connection.execute(
            "INSERT INTO task (identifier, tier, title, status) "
            "VALUES ('T0-1', 0, 't', 'failed')"
        ).lastrowid
        handle.connection.commit()
        _add_failure_events(
            handle.connection, task_id, 2,
            created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
        )

        state = EngineState(plan_tier="standard", max_concurrent=1)
        report = run(handle.connection, state, config=None, now=_T0)
        assert report.escalated == 1
    finally:
        handle.connection.close()


# ---------------------------------------------------------------------------
# Adapter without escalation_config() falls back to defaults
# ---------------------------------------------------------------------------


@dataclass
class _AdapterNoEscalation:
    db_path_value: str

    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig(db_path=self.db_path_value)
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (DispatchHost(name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"),)
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()
    # No escalation_config() method.


def test_adapter_without_escalation_config_uses_defaults(tmp_path):
    """Adapter that doesn't implement escalation_config() falls back to defaults."""
    register_adapter(_AdapterNoEscalation(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    try:
        task_id = handle.connection.execute(
            "INSERT INTO task (identifier, tier, title, status) "
            "VALUES ('T0-1', 0, 't', 'failed')"
        ).lastrowid
        handle.connection.commit()
        # Default threshold is 3 — add 1 failure; should be below threshold.
        _add_failure_events(
            handle.connection, task_id, 1,
            created_at=_PAST.strftime("%Y-%m-%d %H:%M:%S"),
        )
        state = EngineState(plan_tier="standard", max_concurrent=1)
        report = run(handle.connection, state, config=None, now=_T0)
        # Default threshold=3, so 1 failure is below threshold.
        assert report.below_threshold == 1
    finally:
        handle.connection.close()
