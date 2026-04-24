"""Tests for oxi_core.v3.query_helpers.

Covers every public function with:
- Happy-path correctness (row returned / updated / inserted)
- Boundary cases (not-found, empty status list, pr_number filters)
- Guard invariants (guard_status prevents double-transitions)
- extra_cols validation in atomic_status_update
- count_events_by_kind for both single-kind and frozenset variants
- No behaviour change regression: helpers produce the same DB state as
  the inline SQL they replace.

All tests use a real in-memory/tmp SQLite DB (via db.connect) with the
full migration stack applied, so helper correctness is validated against
the actual schema.
"""

from __future__ import annotations

import json
import sqlite3
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
from oxi_core.v3.query_helpers import (
    atomic_status_update,
    count_events_by_kind,
    insert_engine_event,
    insert_event,
    select_task_by_id,
    select_tasks_by_status,
    stamp_progress,
)

# ---------------------------------------------------------------------------
# Minimal adapter & fixtures
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


# ---------------------------------------------------------------------------
# Seed helpers (raw SQL — intentionally avoid using the helpers under test)
# ---------------------------------------------------------------------------


def _insert_task(
    conn: sqlite3.Connection,
    *,
    identifier: str = "T0-1",
    tier: int = 0,
    title: str = "Test Task",
    status: str = "planned",
    pr_number: int | None = None,
) -> int:
    """Insert a minimal task row; return its rowid."""
    cur = conn.execute(
        "INSERT INTO task (identifier, tier, title, status) VALUES (?, ?, ?, ?)",
        (identifier, tier, title, status),
    )
    task_id = cur.lastrowid
    conn.commit()
    if pr_number is not None:
        conn.execute(
            "UPDATE task SET pr_number = ? WHERE id = ?",
            (pr_number, task_id),
        )
        conn.commit()
    return task_id


def _insert_raw_event(
    conn: sqlite3.Connection,
    *,
    task_id: int | None,
    kind: str,
    payload: dict,
) -> None:
    conn.execute(
        "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
        (task_id, kind, json.dumps(payload)),
    )
    conn.commit()


# ===========================================================================
# select_task_by_id
# ===========================================================================


class TestSelectTaskById:
    def test_returns_task_when_found(self, conn):
        task_id = _insert_task(conn, identifier="T0-1", status="planned")
        task = select_task_by_id(conn, task_id)
        assert task is not None
        assert task.id == task_id
        assert task.identifier == "T0-1"
        assert task.status == "planned"

    def test_returns_none_for_missing_id(self, conn):
        result = select_task_by_id(conn, 99999)
        assert result is None

    def test_hydrates_all_standard_fields(self, conn):
        task_id = _insert_task(
            conn, identifier="T1-7", tier=1, title="My Task", status="dispatched"
        )
        task = select_task_by_id(conn, task_id)
        assert task is not None
        assert task.tier == 1
        assert task.title == "My Task"
        assert task.status == "dispatched"

    def test_hydrates_pr_number(self, conn):
        task_id = _insert_task(conn, identifier="T0-5", pr_number=42)
        task = select_task_by_id(conn, task_id)
        assert task is not None
        assert task.pr_number == 42

    def test_pr_number_is_none_when_not_set(self, conn):
        task_id = _insert_task(conn, identifier="T0-6")
        task = select_task_by_id(conn, task_id)
        assert task is not None
        assert task.pr_number is None


# ===========================================================================
# select_tasks_by_status
# ===========================================================================


class TestSelectTasksByStatus:
    def test_returns_empty_list_when_no_matches(self, conn):
        _insert_task(conn, identifier="T0-1", status="planned")
        result = select_tasks_by_status(conn, "dispatched")
        assert result == []

    def test_returns_all_matching_tasks(self, conn):
        _insert_task(conn, identifier="T0-1", status="planned")
        _insert_task(conn, identifier="T0-2", status="planned")
        _insert_task(conn, identifier="T0-3", status="dispatched")
        result = select_tasks_by_status(conn, "planned")
        assert len(result) == 2
        identifiers = {t.identifier for t in result}
        assert identifiers == {"T0-1", "T0-2"}

    def test_default_order_is_id_asc(self, conn):
        id1 = _insert_task(conn, identifier="T0-2", status="planned")
        id2 = _insert_task(conn, identifier="T0-1", status="planned")
        result = select_tasks_by_status(conn, "planned")
        assert result[0].id == id1
        assert result[1].id == id2

    def test_order_by_override(self, conn):
        _insert_task(conn, identifier="T1-1", tier=1, status="planned")
        _insert_task(conn, identifier="T0-1", tier=0, status="planned")
        result = select_tasks_by_status(
            conn, "planned", order_by="tier ASC, identifier ASC"
        )
        assert result[0].identifier == "T0-1"
        assert result[1].identifier == "T1-1"

    def test_limit_parameter(self, conn):
        for i in range(5):
            _insert_task(conn, identifier=f"T0-{i}", status="planned")
        result = select_tasks_by_status(conn, "planned", limit=3)
        assert len(result) == 3

    def test_pr_number_filter_is_null(self, conn):
        _insert_task(conn, identifier="T0-1", status="dispatched", pr_number=None)
        _insert_task(conn, identifier="T0-2", status="dispatched", pr_number=99)
        result = select_tasks_by_status(
            conn, "dispatched", pr_number_filter="is_null"
        )
        assert len(result) == 1
        assert result[0].identifier == "T0-1"

    def test_pr_number_filter_not_null(self, conn):
        _insert_task(conn, identifier="T0-1", status="dispatched", pr_number=None)
        _insert_task(conn, identifier="T0-2", status="dispatched", pr_number=99)
        result = select_tasks_by_status(
            conn, "dispatched", pr_number_filter="not_null"
        )
        assert len(result) == 1
        assert result[0].identifier == "T0-2"

    def test_invalid_pr_number_filter_raises(self, conn):
        with pytest.raises(ValueError, match="pr_number_filter"):
            select_tasks_by_status(conn, "planned", pr_number_filter="bad_value")

    def test_no_pr_number_filter_returns_all(self, conn):
        _insert_task(conn, identifier="T0-1", status="dispatched", pr_number=None)
        _insert_task(conn, identifier="T0-2", status="dispatched", pr_number=99)
        result = select_tasks_by_status(conn, "dispatched")
        assert len(result) == 2

    def test_returns_task_objects_not_rows(self, conn):
        _insert_task(conn, identifier="T0-1", status="planned")
        from oxi_core.v3.dispatch import Task
        result = select_tasks_by_status(conn, "planned")
        assert all(isinstance(t, Task) for t in result)


# ===========================================================================
# insert_event
# ===========================================================================


class TestInsertEvent:
    def test_inserts_row_with_correct_fields(self, conn):
        task_id = _insert_task(conn, identifier="T0-1")
        insert_event(
            conn,
            task_id=task_id,
            kind="test_event",
            payload={"key": "value"},
        )
        row = conn.execute(
            "SELECT task_id, kind, payload FROM event WHERE kind = 'test_event'"
        ).fetchone()
        assert row is not None
        assert row["task_id"] == task_id
        assert row["kind"] == "test_event"
        assert json.loads(row["payload"]) == {"key": "value"}

    def test_inserts_event_with_empty_payload(self, conn):
        task_id = _insert_task(conn, identifier="T0-2")
        insert_event(conn, task_id=task_id, kind="empty_event", payload={})
        row = conn.execute(
            "SELECT payload FROM event WHERE kind = 'empty_event'"
        ).fetchone()
        assert json.loads(row["payload"]) == {}

    def test_multiple_events_for_same_task(self, conn):
        task_id = _insert_task(conn, identifier="T0-3")
        for i in range(3):
            insert_event(conn, task_id=task_id, kind=f"event_{i}", payload={"i": i})
        count = conn.execute(
            "SELECT COUNT(*) FROM event WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        assert count == 3

    def test_created_at_is_populated(self, conn):
        task_id = _insert_task(conn, identifier="T0-4")
        insert_event(conn, task_id=task_id, kind="ts_event", payload={})
        row = conn.execute(
            "SELECT created_at FROM event WHERE kind = 'ts_event'"
        ).fetchone()
        assert row["created_at"] is not None

    def test_raises_on_bad_payload(self, conn):
        task_id = _insert_task(conn, identifier="T0-5")
        with pytest.raises((TypeError, ValueError)):
            insert_event(
                conn,
                task_id=task_id,
                kind="bad",
                payload={"unserializable": object()},  # type: ignore[dict-item]
            )


# ===========================================================================
# insert_engine_event
# ===========================================================================


class TestInsertEngineEvent:
    def test_inserts_row_with_null_task_id(self, conn):
        insert_engine_event(
            conn,
            kind="engine_test_event",
            payload={"source": "engine"},
        )
        row = conn.execute(
            "SELECT task_id, kind, payload FROM event WHERE kind = 'engine_test_event'"
        ).fetchone()
        assert row is not None
        assert row["task_id"] is None
        assert row["kind"] == "engine_test_event"
        assert json.loads(row["payload"])["source"] == "engine"

    def test_separate_from_task_events(self, conn):
        task_id = _insert_task(conn, identifier="T0-1")
        insert_event(conn, task_id=task_id, kind="task_ev", payload={})
        insert_engine_event(conn, kind="engine_ev", payload={})

        task_events = conn.execute(
            "SELECT task_id FROM event WHERE kind = 'task_ev'"
        ).fetchone()
        engine_events = conn.execute(
            "SELECT task_id FROM event WHERE kind = 'engine_ev'"
        ).fetchone()

        assert task_events["task_id"] == task_id
        assert engine_events["task_id"] is None


# ===========================================================================
# stamp_progress
# ===========================================================================


class TestStampProgress:
    def test_updates_last_progress_at_and_updated_at(self, conn):
        task_id = _insert_task(conn, identifier="T0-1")
        ts = stamp_progress(conn, task_id=task_id)

        row = conn.execute(
            "SELECT last_progress_at, updated_at, status FROM task WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row["last_progress_at"] == ts
        assert row["updated_at"] == ts

    def test_does_not_change_status(self, conn):
        task_id = _insert_task(conn, identifier="T0-2", status="dispatched")
        stamp_progress(conn, task_id=task_id)

        row = conn.execute(
            "SELECT status FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        assert row["status"] == "dispatched"

    def test_both_columns_get_same_timestamp(self, conn):
        task_id = _insert_task(conn, identifier="T0-3")
        stamp_progress(conn, task_id=task_id)

        row = conn.execute(
            "SELECT last_progress_at, updated_at FROM task WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row["last_progress_at"] == row["updated_at"]

    def test_returns_the_written_timestamp(self, conn):
        task_id = _insert_task(conn, identifier="T0-4")
        ts = stamp_progress(conn, task_id=task_id)

        assert isinstance(ts, str)
        assert len(ts) == 19  # "YYYY-MM-DD HH:MM:SS"

    def test_second_call_overwrites_first(self, conn):
        task_id = _insert_task(conn, identifier="T0-5")
        stamp_progress(conn, task_id=task_id)
        ts2 = stamp_progress(conn, task_id=task_id)
        # ts2 >= ts1 (may be equal if within the same second)
        row = conn.execute(
            "SELECT last_progress_at FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        assert row["last_progress_at"] == ts2


# ===========================================================================
# atomic_status_update
# ===========================================================================


class TestAtomicStatusUpdate:
    def test_updates_status_and_timestamps(self, conn):
        task_id = _insert_task(conn, identifier="T0-1", status="planned")
        ts = atomic_status_update(
            conn, task_id=task_id, new_status="dispatched"
        )
        row = conn.execute(
            "SELECT status, last_progress_at, updated_at FROM task WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row["status"] == "dispatched"
        assert row["last_progress_at"] == ts
        assert row["updated_at"] == ts

    def test_both_timestamps_identical(self, conn):
        task_id = _insert_task(conn, identifier="T0-2", status="planned")
        ts = atomic_status_update(
            conn, task_id=task_id, new_status="dispatched"
        )
        row = conn.execute(
            "SELECT last_progress_at, updated_at FROM task WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row["last_progress_at"] == row["updated_at"] == ts

    def test_returns_timestamp_string(self, conn):
        task_id = _insert_task(conn, identifier="T0-3")
        ts = atomic_status_update(conn, task_id=task_id, new_status="dispatched")
        assert isinstance(ts, str)
        assert len(ts) == 19

    def test_guard_status_prevents_update_when_mismatch(self, conn):
        task_id = _insert_task(conn, identifier="T0-4", status="dispatched")
        # Guard requires "planned"; row is "dispatched" — should NOT update.
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="merged",
            guard_status="planned",
        )
        row = conn.execute(
            "SELECT status FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        # Status unchanged because guard did not match.
        assert row["status"] == "dispatched"

    def test_guard_status_allows_update_when_matching(self, conn):
        task_id = _insert_task(conn, identifier="T0-5", status="dispatched")
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="merged",
            guard_status="dispatched",
        )
        row = conn.execute(
            "SELECT status FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        assert row["status"] == "merged"

    def test_extra_col_merged_at(self, conn):
        task_id = _insert_task(conn, identifier="T0-6", status="dispatched")
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="merged",
            merged_at="2026-04-24 12:00:00",
        )
        row = conn.execute(
            "SELECT merged_at FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        assert row["merged_at"] == "2026-04-24 12:00:00"

    def test_extra_col_failed_at_and_failure_reason(self, conn):
        task_id = _insert_task(conn, identifier="T0-7", status="dispatched")
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="failed",
            guard_status="dispatched",
            failed_at="2026-04-24 12:00:00",
            failure_reason="critic_rejected",
        )
        row = conn.execute(
            "SELECT status, failed_at, failure_reason FROM task WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["failed_at"] == "2026-04-24 12:00:00"
        assert row["failure_reason"] == "critic_rejected"

    def test_extra_col_dispatched_at(self, conn):
        task_id = _insert_task(conn, identifier="T0-8", status="planned")
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="dispatched",
            dispatched_at="2026-04-24 10:00:00",
        )
        row = conn.execute(
            "SELECT dispatched_at FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        assert row["dispatched_at"] == "2026-04-24 10:00:00"

    def test_unknown_extra_col_raises_value_error(self, conn):
        task_id = _insert_task(conn, identifier="T0-9")
        with pytest.raises(ValueError, match="unknown extra column"):
            atomic_status_update(
                conn,
                task_id=task_id,
                new_status="planned",
                nonexistent_column="oops",
            )

    def test_no_guard_updates_regardless_of_current_status(self, conn):
        task_id = _insert_task(conn, identifier="T0-10", status="failed")
        atomic_status_update(conn, task_id=task_id, new_status="planned")
        row = conn.execute(
            "SELECT status FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        assert row["status"] == "planned"

    def test_extra_col_pr_number(self, conn):
        task_id = _insert_task(conn, identifier="T0-11", status="dispatched")
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="planned",
            pr_number=None,
        )
        row = conn.execute(
            "SELECT pr_number FROM task WHERE id = ?", (task_id,)
        ).fetchone()
        assert row["pr_number"] is None


# ===========================================================================
# count_events_by_kind
# ===========================================================================


class TestCountEventsByKind:
    def test_zero_when_no_events(self, conn):
        task_id = _insert_task(conn, identifier="T0-1")
        assert count_events_by_kind(conn, task_id, "no_such_event") == 0

    def test_single_kind_count(self, conn):
        task_id = _insert_task(conn, identifier="T0-2")
        _insert_raw_event(conn, task_id=task_id, kind="my_event", payload={})
        _insert_raw_event(conn, task_id=task_id, kind="my_event", payload={})
        _insert_raw_event(conn, task_id=task_id, kind="other_event", payload={})
        assert count_events_by_kind(conn, task_id, "my_event") == 2

    def test_frozenset_kind_count(self, conn):
        task_id = _insert_task(conn, identifier="T0-3")
        _insert_raw_event(conn, task_id=task_id, kind="dispatch_failed", payload={})
        _insert_raw_event(conn, task_id=task_id, kind="dispatch_timeout", payload={})
        _insert_raw_event(conn, task_id=task_id, kind="dispatch_succeeded", payload={})
        kinds = frozenset({"dispatch_failed", "dispatch_timeout"})
        assert count_events_by_kind(conn, task_id, kinds) == 2

    def test_empty_frozenset_returns_zero(self, conn):
        task_id = _insert_task(conn, identifier="T0-4")
        _insert_raw_event(conn, task_id=task_id, kind="whatever", payload={})
        assert count_events_by_kind(conn, task_id, frozenset()) == 0

    def test_does_not_count_events_from_other_tasks(self, conn):
        id1 = _insert_task(conn, identifier="T0-5")
        id2 = _insert_task(conn, identifier="T0-6")
        _insert_raw_event(conn, task_id=id1, kind="shared_event", payload={})
        _insert_raw_event(conn, task_id=id1, kind="shared_event", payload={})
        _insert_raw_event(conn, task_id=id2, kind="shared_event", payload={})
        assert count_events_by_kind(conn, id1, "shared_event") == 2
        assert count_events_by_kind(conn, id2, "shared_event") == 1

    def test_frozenset_with_single_kind_matches_string_variant(self, conn):
        task_id = _insert_task(conn, identifier="T0-7")
        _insert_raw_event(conn, task_id=task_id, kind="ping", payload={})
        _insert_raw_event(conn, task_id=task_id, kind="ping", payload={})
        assert count_events_by_kind(conn, task_id, "ping") == 2
        assert count_events_by_kind(conn, task_id, frozenset({"ping"})) == 2

    def test_counts_only_matching_kind_from_mixed_events(self, conn):
        task_id = _insert_task(conn, identifier="T0-8")
        for kind in ["a", "b", "b", "c", "c", "c"]:
            _insert_raw_event(conn, task_id=task_id, kind=kind, payload={})
        assert count_events_by_kind(conn, task_id, "a") == 1
        assert count_events_by_kind(conn, task_id, "b") == 2
        assert count_events_by_kind(conn, task_id, "c") == 3
        assert count_events_by_kind(conn, task_id, frozenset({"a", "b"})) == 3


# ===========================================================================
# Integration: combinations that mirror real caller patterns
# ===========================================================================


class TestIntegrationPatterns:
    """Each test mirrors a pattern from a real v3 module."""

    def test_dispatch_transition_pattern(self, conn):
        """Mirrors dispatch._transition_to_dispatched: status update + event."""
        task_id = _insert_task(conn, identifier="T0-1", status="planned")
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="dispatched",
            guard_status="planned",
            dispatched_at="2026-04-24 10:00:00",
        )
        insert_event(
            conn,
            task_id=task_id,
            kind="dispatch_started",
            payload={"session_id": "s123"},
        )
        task = select_task_by_id(conn, task_id)
        assert task is not None
        assert task.status == "dispatched"
        event_count = count_events_by_kind(conn, task_id, "dispatch_started")
        assert event_count == 1

    def test_pr_watcher_stamp_pr_number_pattern(self, conn):
        """Mirrors pr_watcher._stamp_pr_number: partial update + event."""
        task_id = _insert_task(conn, identifier="T0-2", status="dispatched")
        stamp_progress(conn, task_id=task_id)
        # Set pr_number separately (stamp_progress doesn't do this, the
        # caller also does an atomic_status_update with pr_number kwarg)
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="dispatched",  # status unchanged
            pr_number=99,
        )
        insert_event(
            conn,
            task_id=task_id,
            kind="pr_observed",
            payload={"pr_number": 99},
        )
        task = select_task_by_id(conn, task_id)
        assert task is not None
        assert task.pr_number == 99
        assert task.status == "dispatched"

    def test_auto_recover_retry_count_pattern(self, conn):
        """Mirrors auto_recover._retry_count: count events by kind."""
        task_id = _insert_task(conn, identifier="T0-3", status="failed")
        for i in range(2):
            _insert_raw_event(
                conn, task_id=task_id, kind="auto_recover_attempted", payload={"n": i}
            )
        assert count_events_by_kind(conn, task_id, "auto_recover_attempted") == 2

    def test_heartbeat_abandon_pattern(self, conn):
        """Mirrors heartbeat._abandon: atomic status + event."""
        task_id = _insert_task(conn, identifier="T0-4", status="dispatched")
        atomic_status_update(
            conn,
            task_id=task_id,
            new_status="abandoned",
            guard_status="dispatched",
            failure_reason="stale_no_progress",
        )
        insert_event(
            conn,
            task_id=task_id,
            kind="abandoned_by_heartbeat",
            payload={"reason": "stale_no_progress", "identifier": "T0-4"},
        )
        task = select_task_by_id(conn, task_id)
        assert task is not None
        assert task.status == "abandoned"

    def test_budget_engine_event_pattern(self, conn):
        """Mirrors budget._emit_event: task-less engine event."""
        insert_engine_event(
            conn,
            kind="budget_hard_stop",
            payload={"today_spend_usd": 10.0, "daily_hard_cap_usd": 5.0},
        )
        row = conn.execute(
            "SELECT task_id, payload FROM event WHERE kind = 'budget_hard_stop'"
        ).fetchone()
        assert row["task_id"] is None
        data = json.loads(row["payload"])
        assert data["today_spend_usd"] == 10.0

    def test_select_tasks_matches_inline_query_planned(self, conn):
        """select_tasks_by_status returns the same set as the inline query it replaces."""
        _insert_task(conn, identifier="T1-1", tier=1, status="planned")
        _insert_task(conn, identifier="T0-1", tier=0, status="planned")
        _insert_task(conn, identifier="T0-2", tier=0, status="dispatched")

        # Helper:
        helper_result = select_tasks_by_status(
            conn, "planned", order_by="tier ASC, identifier ASC", limit=1
        )
        # Inline equivalent (mirroring dispatch._pick_next_planned):
        row = conn.execute(
            "SELECT * FROM task WHERE status = 'planned' "
            "ORDER BY tier ASC, identifier ASC LIMIT 1"
        ).fetchone()

        assert len(helper_result) == 1
        assert helper_result[0].identifier == row["identifier"]

    def test_select_tasks_dispatched_without_pr_matches_inline(self, conn):
        """Mirrors pr_watcher._load_dispatched_without_pr."""
        _insert_task(conn, identifier="T0-1", status="dispatched", pr_number=None)
        _insert_task(conn, identifier="T0-2", status="dispatched", pr_number=7)
        _insert_task(conn, identifier="T0-3", status="planned")

        helper_result = select_tasks_by_status(
            conn, "dispatched", pr_number_filter="is_null"
        )
        # Inline equivalent:
        rows = conn.execute(
            "SELECT * FROM task WHERE status = 'dispatched' AND pr_number IS NULL"
        ).fetchall()

        assert len(helper_result) == len(rows) == 1
        assert helper_result[0].identifier == rows[0]["identifier"]

    def test_select_tasks_dispatched_with_pr_matches_inline(self, conn):
        """Mirrors pr_watcher._load_dispatched_with_pr and ci_issue_filer."""
        _insert_task(conn, identifier="T0-1", status="dispatched", pr_number=None)
        _insert_task(conn, identifier="T0-2", status="dispatched", pr_number=7)

        helper_result = select_tasks_by_status(
            conn, "dispatched", pr_number_filter="not_null"
        )
        rows = conn.execute(
            "SELECT * FROM task WHERE status = 'dispatched' AND pr_number IS NOT NULL"
        ).fetchall()

        assert len(helper_result) == len(rows) == 1
        assert helper_result[0].pr_number == 7
