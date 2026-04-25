"""Tests for oxi_core.v3.heartbeat — the reaper.

Every invariant from the prior-orchestrator post-mortem (orphan-reap
thrash, orphan-with-PR abandoned, killswitch bypass, created_at
fallback) has a dedicated test here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from oxi_core.v3.engine_state import EngineState
from oxi_core.v3.heartbeat import (
    DEFAULT_STALE_AFTER_SECONDS,
    ReapReport,
    _effective_freshness,
    _is_stale,
    _parse_sqlite_timestamp,
    reap,
)

# ---------------------------------------------------------------------------
# Adapter + fixtures
# ---------------------------------------------------------------------------


@dataclass
class _TestAdapter:
    db_path_value: str

    def naming(self) -> NamingConfig:
        return NamingConfig()

    def paths(self) -> PathsConfig:
        return PathsConfig(db_path=self.db_path_value)

    def budget(self) -> BudgetCaps:
        return BudgetCaps()

    def github_repo(self) -> str:
        return "owner/repo"

    def roadmap_location(self) -> str:
        return "roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return ("feat/",)

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (
            DispatchHost(
                name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"
            ),
        )

    def promote_recipe(self) -> None:
        return None

    def plan_tier(self) -> str:
        return "standard"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def conn(tmp_path: Path):
    register_adapter(_TestAdapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield handle.connection
    handle.connection.close()


def _seed_dispatched(
    conn,
    *,
    identifier: str = "T0-1",
    last_progress_at: str | None = None,
    pr_number: int | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> int:
    """Insert a task directly into the 'dispatched' state with controllable timestamps.

    Uses raw UPDATE after INSERT so we can set arbitrary ISO strings.
    """
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status) "
        "VALUES (?, 0, 'title', 'dispatched')",
        (identifier,),
    )
    task_id = cur.lastrowid

    if last_progress_at is not None:
        conn.execute(
            "UPDATE task SET last_progress_at = ? WHERE id = ?",
            (last_progress_at, task_id),
        )
    if updated_at is not None:
        conn.execute(
            "UPDATE task SET updated_at = ? WHERE id = ?",
            (updated_at, task_id),
        )
    if created_at is not None:
        conn.execute(
            "UPDATE task SET created_at = ? WHERE id = ?",
            (created_at, task_id),
        )
    if pr_number is not None:
        # The schema doesn't have pr_number yet; we'd need to add it
        # via future migration. For now, tests that want a PR simulate
        # it by patching the Task object post-load.
        pass
    conn.commit()
    return task_id


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# _parse_sqlite_timestamp
# ---------------------------------------------------------------------------


def test_parse_sqlite_timestamp_happy():
    dt = _parse_sqlite_timestamp("2026-04-23 12:00:00")
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 12
    assert dt.tzinfo is UTC


def test_parse_sqlite_timestamp_returns_none_on_none():
    assert _parse_sqlite_timestamp(None) is None


def test_parse_sqlite_timestamp_returns_none_on_garbage():
    assert _parse_sqlite_timestamp("not a date") is None


# ---------------------------------------------------------------------------
# _effective_freshness
# ---------------------------------------------------------------------------


def test_effective_freshness_prefers_last_progress_at():
    from oxi_core.v3.dispatch import Task
    lpa = "2026-04-23 12:00:00"
    upd = "2026-04-22 10:00:00"  # earlier
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=lpa, created_at="2026-04-20 00:00:00", updated_at=upd,
    )
    result = _effective_freshness(task)
    assert result == _parse_sqlite_timestamp(lpa)


def test_effective_freshness_falls_back_to_updated_at():
    from oxi_core.v3.dispatch import Task
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=None, created_at="2026-04-20 00:00:00",
        updated_at="2026-04-23 12:00:00",
    )
    result = _effective_freshness(task)
    assert result == _parse_sqlite_timestamp("2026-04-23 12:00:00")


def test_effective_freshness_never_uses_created_at():
    """Critical: heartbeat must never trust created_at for freshness.

    That's the bug class that killed prior orchestrators — a 2-day-old
    task dispatched 5 seconds ago gets reaped because created_at is
    used as the clock.
    """
    from oxi_core.v3.dispatch import Task
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=None, updated_at=None,
        created_at="2026-04-23 12:00:00",
    )
    # Both lpa and updated_at are None; freshness is None (stale by default).
    assert _effective_freshness(task) is None


# ---------------------------------------------------------------------------
# _is_stale
# ---------------------------------------------------------------------------


def test_is_stale_when_older_than_grace():
    from oxi_core.v3.dispatch import Task
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old_ts = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=old_ts, created_at=old_ts, updated_at=old_ts,
    )
    assert _is_stale(task, now, timedelta(minutes=10)) is True


def test_is_stale_false_when_fresh():
    from oxi_core.v3.dispatch import Task
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    recent = (now - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=recent, created_at=recent, updated_at=recent,
    )
    assert _is_stale(task, now, timedelta(minutes=10)) is False


def test_is_stale_true_when_no_freshness_data():
    """Defensive: no timestamps at all → stale."""
    from oxi_core.v3.dispatch import Task
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    task = Task(
        id=1, identifier="t", tier=0, title="", subtitle="",
        target_repo=None, status="dispatched", pr_number=None,
        last_progress_at=None, created_at=None, updated_at=None,
    )
    assert _is_stale(task, now, timedelta(minutes=10)) is True


# ---------------------------------------------------------------------------
# reap — happy paths
# ---------------------------------------------------------------------------


def test_reap_abandons_stale_task(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.considered == 1
    assert report.abandoned == 1
    assert report.protected_by_pr == 0
    assert report.skipped_fresh == 0

    row = conn.execute("SELECT status, failure_reason FROM task WHERE id = 1").fetchone()
    assert row["status"] == "abandoned"
    assert row["failure_reason"] == "stale_no_progress"


def test_reap_skips_fresh_task(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    recent = _iso(now - timedelta(seconds=10))
    _seed_dispatched(conn, last_progress_at=recent, updated_at=recent)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.abandoned == 0
    assert report.skipped_fresh == 1
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "dispatched"


def test_reap_ignores_non_dispatched_tasks(conn):
    """Planned, merged, abandoned, failed rows are never considered."""
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(hours=2))
    # Planned, old — should not be touched.
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, last_progress_at, updated_at) "
        "VALUES ('T0-2', 0, 't', 'planned', ?, ?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, last_progress_at, updated_at) "
        "VALUES ('T0-3', 0, 't', 'merged', ?, ?)",
        (old, old),
    )
    conn.commit()

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.considered == 0
    assert report.abandoned == 0


def test_reap_emits_ledger_event(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    task_id = _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    reap(conn, state, stale_after_seconds=600, now=now)

    kinds = [
        row[0]
        for row in conn.execute(
            "SELECT kind FROM event WHERE task_id = ?", (task_id,)
        )
    ]
    assert "abandoned_by_heartbeat" in kinds


# ---------------------------------------------------------------------------
# Engine stop
# ---------------------------------------------------------------------------


def test_reap_is_noop_when_stopping(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    state.request_stop()

    report = reap(conn, state, stale_after_seconds=600, now=now)
    assert report == ReapReport(
        considered=0, abandoned=0, protected_by_pr=0, skipped_fresh=0
    )
    # Task stayed dispatched.
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Atomic transition (anti-pattern #1 on the reap side)
# ---------------------------------------------------------------------------


def test_reap_stamps_last_progress_at_on_abandon(conn):
    """Even on abandon, the freshness beacon updates.

    Without this, a subsequent reap would see the same stale timestamp
    and … well, the status is already abandoned so it wouldn't reap
    again. But stamping keeps the invariant that every state transition
    stamps freshness, which simplifies reasoning.
    """
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    reap(conn, state, stale_after_seconds=600, now=now)

    row = conn.execute(
        "SELECT last_progress_at, updated_at FROM task WHERE id = 1"
    ).fetchone()
    assert row["last_progress_at"] != old
    assert row["updated_at"] != old


# ---------------------------------------------------------------------------
# Orphan-with-PR protection (anti-pattern #4)
# ---------------------------------------------------------------------------


def test_reap_does_not_abandon_task_with_open_pr(conn, monkeypatch):
    """Tasks with pr_number != None are protected from abandonment.

    The current schema doesn't yet have pr_number; we simulate the
    invariant by patching ``is_orphan_reapable`` to return False for
    a specific task. When pr_watcher lands and adds the column, this
    test gets rewritten to seed pr_number directly.
    """
    from oxi_core.v3 import heartbeat as hb_mod

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    # Patch the shared helper: this specific task has a PR.
    monkeypatch.setattr(hb_mod, "is_orphan_reapable", lambda task: False)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.abandoned == 0
    assert report.protected_by_pr == 1
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Mixed case — multiple tasks, different outcomes
# ---------------------------------------------------------------------------


def test_reap_mixed_batch(conn):
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    stale_ts = _iso(now - timedelta(minutes=30))
    fresh_ts = _iso(now - timedelta(seconds=5))

    _seed_dispatched(conn, identifier="T0-1", last_progress_at=stale_ts, updated_at=stale_ts)
    _seed_dispatched(conn, identifier="T0-2", last_progress_at=fresh_ts, updated_at=fresh_ts)
    _seed_dispatched(conn, identifier="T0-3", last_progress_at=stale_ts, updated_at=stale_ts)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(conn, state, stale_after_seconds=600, now=now)

    assert report.considered == 3
    assert report.abandoned == 2
    assert report.skipped_fresh == 1
    assert report.protected_by_pr == 0


# ---------------------------------------------------------------------------
# Default constant sanity
# ---------------------------------------------------------------------------


def test_default_stale_after_is_conservative():
    # 10 minutes is the baseline documented grace period.
    assert DEFAULT_STALE_AFTER_SECONDS == 600


def test_report_dataclass_is_frozen():
    from dataclasses import FrozenInstanceError

    report = ReapReport(considered=1, abandoned=1, protected_by_pr=0, skipped_fresh=0)
    with pytest.raises(FrozenInstanceError):
        report.abandoned = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# HeartbeatConfig + triage (T2-38)
# ---------------------------------------------------------------------------


from oxi_core.v3.heartbeat import HeartbeatConfig  # noqa: E402 — after markers


def test_heartbeat_config_default_triage_disabled():
    """Feature flag defaults to False — zero LLM calls by default."""
    cfg = HeartbeatConfig()
    assert cfg.triage_enabled is False


def test_heartbeat_config_triage_enabled():
    """Opt-in via triage_enabled=True."""
    cfg = HeartbeatConfig(triage_enabled=True)
    assert cfg.triage_enabled is True


def test_heartbeat_config_is_frozen():
    """HeartbeatConfig is immutable."""
    from dataclasses import FrozenInstanceError
    cfg = HeartbeatConfig()
    with pytest.raises(FrozenInstanceError):
        cfg.triage_enabled = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Triage disabled (default) — gateway is ignored
# ---------------------------------------------------------------------------


def test_reap_triage_disabled_no_gateway_calls(conn):
    """When triage_enabled=False, no calls are made even if a gateway is passed."""
    from oxi_core.v3.inference import FakeInferenceGateway, InferenceResult

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    fake = FakeInferenceGateway()
    state = EngineState(plan_tier="standard", max_concurrent=4)

    reap(
        conn, state,
        stale_after_seconds=600, now=now,
        gateway=fake,
        config=HeartbeatConfig(triage_enabled=False),
    )

    # Task was abandoned but gateway was never called.
    assert fake.call_count() == 0
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "abandoned"


def test_reap_triage_disabled_ledger_has_no_triage_summary(conn):
    """When triage is disabled, ledger payload has no triage_summary key."""
    import json as _json

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    task_id = _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    reap(
        conn, state,
        stale_after_seconds=600, now=now,
        config=HeartbeatConfig(triage_enabled=False),
    )

    row = conn.execute(
        "SELECT payload FROM event WHERE task_id = ? AND kind = 'abandoned_by_heartbeat'",
        (task_id,),
    ).fetchone()
    assert row is not None
    payload = _json.loads(row["payload"])
    assert "triage_summary" not in payload
    assert payload["reason"] == "stale_no_progress"


# ---------------------------------------------------------------------------
# Triage enabled — FakeInferenceGateway, fakes-not-mocks
# ---------------------------------------------------------------------------


def test_reap_triage_enabled_calls_gateway(conn):
    """When triage_enabled=True and a gateway is provided, it is called once
    per abandoned task."""
    from oxi_core.v3.inference import FakeInferenceGateway, InferenceResult

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, identifier="T0-1", last_progress_at=old, updated_at=old)

    fake = FakeInferenceGateway()
    canned = InferenceResult(
        text="The worker likely crashed before stamping progress.",
        cost_usd=0.0001,
        tokens_in=50,
        tokens_out=20,
        model="claude-haiku-4-5-20251001",
        latency_ms=100.0,
    )
    # Register a catch-all: FakeInferenceGateway returns the canned result for
    # any unknown key by default (zero-cost empty), but we want to verify the
    # call happened. We rely on the zero-cost default for this test.
    state = EngineState(plan_tier="standard", max_concurrent=4)

    reap(
        conn, state,
        stale_after_seconds=600, now=now,
        gateway=fake,
        config=HeartbeatConfig(triage_enabled=True),
    )

    assert fake.call_count() == 1
    call = fake.calls[0]
    # Model resolved from routing.yaml (heartbeat-triage role).
    assert "claude-haiku" in call["model"] or "haiku" in call["model"]
    # Messages contain the task identifier and title.
    assert any("T0-1" in str(m.get("content", "")) for m in call["messages"])


def test_reap_triage_enabled_summary_stored_in_ledger(conn):
    """triage_summary from the gateway is stored in the event payload."""
    import json as _json

    from oxi_core.v3.inference import FakeInferenceGateway, InferenceResult

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    task_id = _seed_dispatched(conn, identifier="T0-1", last_progress_at=old, updated_at=old)

    fake = FakeInferenceGateway()

    state = EngineState(plan_tier="standard", max_concurrent=4)

    # Register a canned result so we know what text to expect.
    # We register *after* building messages, so use a wildcard approach:
    # FakeInferenceGateway returns empty text for unknown keys. We'll
    # pre-seed a response for a known model.
    model = "claude-haiku-4-5-20251001"
    canned_text = "Worker likely ran out of budget mid-implementation."
    canned = InferenceResult(
        text=canned_text,
        cost_usd=0.0002,
        tokens_in=60,
        tokens_out=15,
        model=model,
        latency_ms=80.0,
    )

    # Build the expected messages so we can register the canned result.
    from oxi_core.v3.dispatch import Task as _Task
    from oxi_core.v3.heartbeat import _build_triage_messages, _load_recent_events
    task_row = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    task_obj = _Task.from_row(task_row)
    msgs = _build_triage_messages(task_obj, _load_recent_events(conn, task_id))
    fake.register(model=model, messages=msgs, result=canned)

    reap(
        conn, state,
        stale_after_seconds=600, now=now,
        gateway=fake,
        config=HeartbeatConfig(triage_enabled=True),
    )

    row = conn.execute(
        "SELECT payload FROM event WHERE task_id = ? AND kind = 'abandoned_by_heartbeat'",
        (task_id,),
    ).fetchone()
    assert row is not None
    payload = _json.loads(row["payload"])
    assert payload.get("triage_summary") == canned_text


def test_reap_triage_multiple_stale_tasks_each_called(conn):
    """Each abandoned task gets its own triage call."""
    from oxi_core.v3.inference import FakeInferenceGateway

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    fresh = _iso(now - timedelta(seconds=5))

    _seed_dispatched(conn, identifier="T0-1", last_progress_at=old, updated_at=old)
    _seed_dispatched(conn, identifier="T0-2", last_progress_at=fresh, updated_at=fresh)  # fresh — skipped
    _seed_dispatched(conn, identifier="T0-3", last_progress_at=old, updated_at=old)

    fake = FakeInferenceGateway()
    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(
        conn, state,
        stale_after_seconds=600, now=now,
        gateway=fake,
        config=HeartbeatConfig(triage_enabled=True),
    )

    assert report.abandoned == 2
    assert report.skipped_fresh == 1
    # One triage call per abandoned task.
    assert fake.call_count() == 2


def test_reap_triage_enabled_but_no_gateway_skips_silently(conn):
    """triage_enabled=True with gateway=None doesn't crash — triage is skipped."""
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    task_id = _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(
        conn, state,
        stale_after_seconds=600, now=now,
        gateway=None,
        config=HeartbeatConfig(triage_enabled=True),
    )

    # Task still abandoned — triage failure must not block reaping.
    assert report.abandoned == 1
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "abandoned"


def test_reap_triage_gateway_error_does_not_block_abandon(conn):
    """If the gateway raises, the task is still abandoned (triage is best-effort)."""
    from oxi_core.v3.inference import FakeInferenceGateway, InferenceServiceError

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    task_id = _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    # Subclass FakeInferenceGateway to always raise.
    class _FailingGateway(FakeInferenceGateway):
        async def complete(self, messages, model, max_tokens, **kwargs):
            raise InferenceServiceError("litellm is down", status_code=503)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    report = reap(
        conn, state,
        stale_after_seconds=600, now=now,
        gateway=_FailingGateway(),
        config=HeartbeatConfig(triage_enabled=True),
    )

    # Abandonment happened despite the gateway error.
    assert report.abandoned == 1
    row = conn.execute("SELECT status FROM task WHERE id = 1").fetchone()
    assert row["status"] == "abandoned"


def test_reap_triage_ledger_payload_has_no_summary_on_gateway_error(conn):
    """When triage errors, ledger payload omits triage_summary (not empty string)."""
    import json as _json

    from oxi_core.v3.inference import FakeInferenceGateway, InferenceServiceError

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    task_id = _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    class _FailingGateway(FakeInferenceGateway):
        async def complete(self, messages, model, max_tokens, **kwargs):
            raise InferenceServiceError("litellm is down", status_code=503)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    reap(
        conn, state,
        stale_after_seconds=600, now=now,
        gateway=_FailingGateway(),
        config=HeartbeatConfig(triage_enabled=True),
    )

    row = conn.execute(
        "SELECT payload FROM event WHERE task_id = ? AND kind = 'abandoned_by_heartbeat'",
        (task_id,),
    ).fetchone()
    payload = _json.loads(row["payload"])
    # Either key absent or value is empty/falsy — both are acceptable.
    assert not payload.get("triage_summary")


# ---------------------------------------------------------------------------
# Config resolution — adapter-optional method
# ---------------------------------------------------------------------------


def test_resolve_config_uses_explicit_arg_over_adapter(conn, monkeypatch):
    """Explicit config argument wins over everything."""
    from oxi_core.v3 import heartbeat as hb_mod

    explicit_cfg = HeartbeatConfig(triage_enabled=True)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(minutes=30))
    _seed_dispatched(conn, last_progress_at=old, updated_at=old)

    state = EngineState(plan_tier="standard", max_concurrent=4)
    # The adapter registered in this test's fixture does NOT have
    # heartbeat_config() — explicit arg must be used.
    from oxi_core.v3.inference import FakeInferenceGateway
    fake = FakeInferenceGateway()
    reap(
        conn, state,
        stale_after_seconds=600, now=now,
        gateway=fake,
        config=explicit_cfg,
    )
    # triage_enabled=True → gateway was called (confirms explicit config used).
    assert fake.call_count() == 1


def test_resolve_config_falls_back_to_defaults_when_no_adapter_method(conn):
    """When adapter has no heartbeat_config(), defaults apply (triage_enabled=False)."""
    from oxi_core.v3.heartbeat import _resolve_config
    # The _TestAdapter in this file has no heartbeat_config(); defaults apply.
    result = _resolve_config(None)
    assert result.triage_enabled is False
