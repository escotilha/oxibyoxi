"""Tests for oxi_core.v3.auto_external.dedup — T1-B7.

Three-layer dedup: identifier (counter), semantic (cosine similarity),
temporal (cooldown window).

Test strategy
-------------
- Pure in-process: no subprocess, no real adapter, no network.
- All DB interactions use an in-memory SQLite with the minimal schema
  needed by each layer (event, front, task tables).
- Fixed embeddings / text injected directly — no sentence-transformer.
- Ledger events are asserted after each dedup call.
- ``now`` is pinned to a fixed datetime in all temporal tests.

Coverage matrix
---------------
dedup_identifier:
  - fresh DB returns counter=1
  - after 1 EXTERNAL_PROPOSAL_EMITTED event returns counter=2
  - after N events returns max+1
  - counter field missing in payload → treated as 0 → counter=1
  - malformed JSON payload → skipped gracefully → counter=1
  - counter monotonicity: 3 events with counters 1,2,3 → next is 4

dedup_semantic:
  - empty corpus → never a duplicate
  - candidate text empty → never a duplicate
  - very similar title to a front row → duplicate (sim ≥ 0.85)
  - dissimilar title → not duplicate (sim < 0.85)
  - very similar title to a recent task → duplicate
  - old task (outside 30-day window) → not considered
  - threshold parameter respected

dedup_temporal:
  - fresh DB → not a temporal duplicate
  - same (signal_kind, target_identifier) within 14 days → duplicate
  - same signal_kind but different target → not duplicate
  - different signal_kind same target → not duplicate
  - event exists but outside window (> 14 days ago) → not duplicate
  - window_days parameter respected

run_dedup:
  - empty candidates → returns []
  - candidate passing all layers → survives, gets _proposal_counter
  - candidate failing semantic → dropped, DEDUP_SKIPPED event written
  - candidate failing temporal → dropped, DEDUP_SKIPPED event written
  - multiple candidates: some pass some fail, counters monotonic
  - DEDUP_SKIPPED event payload has correct fields

scan._run_dedup integration:
  - _run_dedup with real conn passes candidates through run_dedup
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from oxi_core.v3.auto_external.dedup import (
    SEMANTIC_THRESHOLD,
    dedup_identifier,
    dedup_semantic,
    dedup_temporal,
    run_dedup,
)
from oxi_core.v3.ledger_events import LedgerEvent

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_NOW_STR = "2026-04-25 12:00:00"
_14_DAYS_AGO = (_NOW - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
_13_DAYS_AGO = (_NOW - timedelta(days=13)).strftime("%Y-%m-%d %H:%M:%S")
_15_DAYS_AGO = (_NOW - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
_31_DAYS_AGO = (_NOW - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
_29_DAYS_AGO = (_NOW - timedelta(days=29)).strftime("%Y-%m-%d %H:%M:%S")


def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with the full oxi schema subset."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS event (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    INTEGER,
            kind       TEXT NOT NULL,
            payload    TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS front (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            weight      REAL NOT NULL DEFAULT 1.0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            identifier  TEXT,
            tier        INTEGER NOT NULL DEFAULT 99,
            title       TEXT NOT NULL DEFAULT '',
            subtitle    TEXT NOT NULL DEFAULT '',
            last_seeded_at TEXT,
            cooldown_hours REAL NOT NULL DEFAULT 24.0
        );

        CREATE TABLE IF NOT EXISTS task (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier        TEXT NOT NULL UNIQUE,
            tier              INTEGER NOT NULL,
            title             TEXT NOT NULL,
            subtitle          TEXT NOT NULL DEFAULT '',
            target_repo       TEXT,
            status            TEXT NOT NULL DEFAULT 'planned',
            files_touched     TEXT NOT NULL DEFAULT '[]',
            last_progress_at  TEXT,
            dispatched_at     TEXT,
            merged_at         TEXT,
            failed_at         TEXT,
            failure_reason    TEXT,
            cost_usd          REAL NOT NULL DEFAULT 0.0,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    return conn


def _insert_emitted_event(
    conn: sqlite3.Connection,
    *,
    signal_kind: str = "github_release",
    target_identifier: str = "anthropic/sdk",
    counter: int | None = None,
    created_at: str = _NOW_STR,
) -> None:
    """Insert one EXTERNAL_PROPOSAL_EMITTED event into the ledger."""
    payload: dict = {
        "signal_kind": signal_kind,
        "target_identifier": target_identifier,
    }
    if counter is not None:
        payload["counter"] = counter
    conn.execute(
        "INSERT INTO event (task_id, kind, payload, created_at) VALUES (NULL, ?, ?, ?)",
        (LedgerEvent.EXTERNAL_PROPOSAL_EMITTED, json.dumps(payload), created_at),
    )
    conn.commit()


def _insert_front(
    conn: sqlite3.Connection,
    *,
    name: str = "T1-1",
    title: str = "",
    subtitle: str = "",
    description: str = "",
    active: int = 1,
) -> None:
    """Insert one row into the front table."""
    conn.execute(
        "INSERT INTO front (name, description, title, subtitle, active) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, description, title, subtitle, active),
    )
    conn.commit()


def _insert_task(
    conn: sqlite3.Connection,
    *,
    identifier: str = "T1-1",
    title: str = "",
    subtitle: str = "",
    updated_at: str = _NOW_STR,
) -> None:
    """Insert one row into the task table."""
    conn.execute(
        "INSERT INTO task (identifier, tier, title, subtitle, updated_at) "
        "VALUES (?, 1, ?, ?, ?)",
        (identifier, title, subtitle, updated_at),
    )
    conn.commit()


def _dedup_events(conn: sqlite3.Connection) -> list[dict]:
    """Return all EXTERNAL_PROPOSAL_DEDUP_SKIPPED events as payload dicts."""
    rows = conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (LedgerEvent.EXTERNAL_PROPOSAL_DEDUP_SKIPPED,),
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _make_candidate(
    *,
    signal_kind: str = "github_release",
    target_identifier: str = "anthropic/sdk",
    title: str = "feat: add streaming support to SDK",
    subtitle: str = "SDK now streams responses for lower latency",
) -> dict:
    return {
        "signal_kind": signal_kind,
        "target_identifier": target_identifier,
        "title": title,
        "subtitle": subtitle,
    }


# ---------------------------------------------------------------------------
# Layer 1: dedup_identifier
# ---------------------------------------------------------------------------


class TestDedupIdentifier:
    """Tests for the identifier / counter layer."""

    def test_fresh_db_returns_one(self):
        conn = _make_db()
        assert dedup_identifier(conn) == 1

    def test_after_one_event_returns_two(self):
        conn = _make_db()
        _insert_emitted_event(conn, counter=1)
        assert dedup_identifier(conn) == 2

    def test_after_multiple_events_returns_max_plus_one(self):
        conn = _make_db()
        for i in (1, 3, 5):
            _insert_emitted_event(conn, counter=i, target_identifier=f"repo/{i}")
        assert dedup_identifier(conn) == 6

    def test_counter_field_missing_treated_as_zero(self):
        """Event with no 'counter' key in payload → treated as 0 → next is 1."""
        conn = _make_db()
        _insert_emitted_event(conn)  # no counter= set
        assert dedup_identifier(conn) == 1

    def test_malformed_json_skipped_gracefully(self):
        """Malformed payload is skipped; counter starts at 1."""
        conn = _make_db()
        conn.execute(
            "INSERT INTO event (task_id, kind, payload, created_at) VALUES (NULL, ?, ?, ?)",
            (LedgerEvent.EXTERNAL_PROPOSAL_EMITTED, "not-json", _NOW_STR),
        )
        conn.commit()
        assert dedup_identifier(conn) == 1

    def test_counter_monotonicity(self):
        """Counters 1,2,3 → next should be 4."""
        conn = _make_db()
        for i in (1, 2, 3):
            _insert_emitted_event(conn, counter=i, target_identifier=f"repo/{i}")
        assert dedup_identifier(conn) == 4

    def test_out_of_order_counters(self):
        """Counter 7 inserted before 1,2 — max+1 must be 8."""
        conn = _make_db()
        for i in (7, 1, 2):
            _insert_emitted_event(conn, counter=i, target_identifier=f"repo/{i}")
        assert dedup_identifier(conn) == 8


# ---------------------------------------------------------------------------
# Layer 2: dedup_semantic
# ---------------------------------------------------------------------------


class TestDedupSemantic:
    """Tests for the cosine-similarity layer."""

    def test_empty_corpus_not_duplicate(self):
        """When there are no fronts/tasks, nothing is a semantic duplicate."""
        conn = _make_db()
        candidate = _make_candidate(title="some novel proposal", subtitle="")
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert not is_dup
        assert sim == 0.0

    def test_empty_candidate_text_not_duplicate(self):
        """Candidate with empty title+subtitle can't be a semantic duplicate."""
        conn = _make_db()
        _insert_front(conn, title="add streaming support SDK responses latency")
        candidate = _make_candidate(title="", subtitle="")
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert not is_dup
        assert sim == 0.0

    def test_identical_title_is_duplicate(self):
        """Title identical to a front row → sim=1.0 → duplicate."""
        conn = _make_db()
        text = "add streaming support to anthropic SDK responses latency"
        _insert_front(conn, title=text)
        candidate = _make_candidate(title=text, subtitle="")
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert is_dup
        assert sim >= SEMANTIC_THRESHOLD

    def test_very_similar_title_is_duplicate(self):
        """Near-identical title → duplicate."""
        conn = _make_db()
        _insert_front(
            conn,
            title="feat: add streaming support to the Anthropic Python SDK",
            subtitle="lower latency for responses via stream mode",
        )
        candidate = _make_candidate(
            title="feat: add streaming support to Anthropic Python SDK",
            subtitle="lower latency via stream mode in responses",
        )
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert is_dup, f"expected duplicate but got sim={sim:.3f}"

    def test_dissimilar_title_not_duplicate(self):
        """Completely different topic → not a duplicate."""
        conn = _make_db()
        _insert_front(
            conn,
            title="fix: retry logic in HTTP client",
            subtitle="exponential backoff for transient errors",
        )
        candidate = _make_candidate(
            title="feat: integrate Grok vision model",
            subtitle="multimodal image analysis via xAI Grok",
        )
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert not is_dup, f"expected not-duplicate but got sim={sim:.3f}"
        assert sim < SEMANTIC_THRESHOLD

    def test_similar_to_recent_task_is_duplicate(self):
        """Candidate very similar to a task updated within 30 days → duplicate."""
        conn = _make_db()
        _insert_task(
            conn,
            identifier="T1-42",
            title="feat: add streaming support to anthropic SDK responses latency",
            subtitle="",
            updated_at=_29_DAYS_AGO,
        )
        candidate = _make_candidate(
            title="feat: add streaming support to anthropic SDK responses latency",
            subtitle="",
        )
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert is_dup, f"expected duplicate but sim={sim:.3f}"

    def test_old_task_outside_window_ignored(self):
        """Task updated 31 days ago is outside the 30-day window → not corpus."""
        conn = _make_db()
        _insert_task(
            conn,
            identifier="T1-99",
            title="feat: add streaming support to anthropic SDK responses latency",
            subtitle="",
            updated_at=_31_DAYS_AGO,
        )
        candidate = _make_candidate(
            title="feat: add streaming support to anthropic SDK responses latency",
            subtitle="",
        )
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert not is_dup, f"old task should not be in corpus but sim={sim:.3f}"

    def test_inactive_front_ignored(self):
        """Inactive front row (active=0) is not in the corpus."""
        conn = _make_db()
        _insert_front(
            conn,
            title="feat: add streaming support to anthropic SDK responses latency",
            active=0,
        )
        candidate = _make_candidate(
            title="feat: add streaming support to anthropic SDK responses latency",
        )
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert not is_dup, f"inactive front should not be in corpus but sim={sim:.3f}"

    def test_threshold_parameter_respected(self):
        """Custom threshold of 0.1 causes even mildly-similar text to be flagged."""
        conn = _make_db()
        _insert_front(conn, title="streaming sdk anthropic")
        candidate = _make_candidate(title="streaming api latency sdk")
        is_dup_default, _ = dedup_semantic(candidate, conn, now=_NOW)
        is_dup_low, sim = dedup_semantic(
            candidate, conn, threshold=0.10, now=_NOW
        )
        # With 0.10 threshold, shared words "streaming sdk" should trigger.
        assert is_dup_low, (
            f"expected duplicate at threshold=0.10 but sim={sim:.3f}"
        )
        # With default 0.85, the same pair may not be a duplicate.
        _ = is_dup_default  # result depends on exact text; just check no crash

    def test_returns_max_similarity(self):
        """dedup_semantic returns the highest similarity across corpus."""
        conn = _make_db()
        _insert_front(conn, name="F1", title="completely unrelated topic nothing matching")
        _insert_front(
            conn,
            name="F2",
            title="feat: add streaming support to anthropic SDK responses latency",
        )
        candidate = _make_candidate(
            title="feat: add streaming support to anthropic SDK responses latency"
        )
        is_dup, sim = dedup_semantic(candidate, conn, now=_NOW)
        assert is_dup
        assert sim >= SEMANTIC_THRESHOLD


# ---------------------------------------------------------------------------
# Layer 3: dedup_temporal
# ---------------------------------------------------------------------------


class TestDedupTemporal:
    """Tests for the 14-day cooldown window layer."""

    def test_fresh_db_not_temporal_duplicate(self):
        conn = _make_db()
        candidate = _make_candidate()
        assert not dedup_temporal(candidate, conn, now=_NOW)

    def test_same_pair_within_window_is_duplicate(self):
        """Same (signal_kind, target_identifier) within 14 days → duplicate."""
        conn = _make_db()
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_13_DAYS_AGO,
        )
        candidate = _make_candidate(
            signal_kind="github_release", target_identifier="anthropic/sdk"
        )
        assert dedup_temporal(candidate, conn, now=_NOW)

    def test_same_signal_different_target_not_duplicate(self):
        """Different target_identifier → not a temporal duplicate."""
        conn = _make_db()
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_13_DAYS_AGO,
        )
        candidate = _make_candidate(
            signal_kind="github_release", target_identifier="openai/openai-python"
        )
        assert not dedup_temporal(candidate, conn, now=_NOW)

    def test_different_signal_same_target_not_duplicate(self):
        """Different signal_kind → not a temporal duplicate."""
        conn = _make_db()
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_13_DAYS_AGO,
        )
        candidate = _make_candidate(
            signal_kind="newsletter_mention", target_identifier="anthropic/sdk"
        )
        assert not dedup_temporal(candidate, conn, now=_NOW)

    def test_event_outside_window_not_duplicate(self):
        """Event from 15 days ago is outside the 14-day window → not a duplicate."""
        conn = _make_db()
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_15_DAYS_AGO,
        )
        candidate = _make_candidate(
            signal_kind="github_release", target_identifier="anthropic/sdk"
        )
        assert not dedup_temporal(candidate, conn, now=_NOW)

    def test_event_exactly_at_boundary(self):
        """Event created exactly 14 days ago: boundary is inclusive (>= cutoff)."""
        conn = _make_db()
        # The cutoff is now - 14 days; created_at == cutoff should be >= cutoff.
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_14_DAYS_AGO,
        )
        candidate = _make_candidate(
            signal_kind="github_release", target_identifier="anthropic/sdk"
        )
        # At the exact boundary: should be considered within window.
        # The SQL uses >=, so this event qualifies.
        result = dedup_temporal(candidate, conn, now=_NOW)
        assert result  # boundary is inclusive

    def test_window_days_parameter_respected(self):
        """window_days=7: event from 8 days ago is outside → not duplicate."""
        conn = _make_db()
        eight_days_ago = (_NOW - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=eight_days_ago,
        )
        candidate = _make_candidate(
            signal_kind="github_release", target_identifier="anthropic/sdk"
        )
        # 7-day window: 8 days ago is outside.
        assert not dedup_temporal(candidate, conn, window_days=7, now=_NOW)
        # Default 14-day window: 8 days ago is inside.
        assert dedup_temporal(candidate, conn, now=_NOW)

    def test_no_signal_kind_or_target_not_duplicate(self):
        """Candidate with neither signal_kind nor target → not flagged."""
        conn = _make_db()
        _insert_emitted_event(conn, created_at=_13_DAYS_AGO)
        candidate = {"title": "something", "subtitle": ""}
        assert not dedup_temporal(candidate, conn, now=_NOW)

    def test_multiple_events_one_matching(self):
        """Multiple events in window; only one matches → is duplicate."""
        conn = _make_db()
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="other/repo",
            created_at=_13_DAYS_AGO,
        )
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_13_DAYS_AGO,
        )
        candidate = _make_candidate(
            signal_kind="github_release", target_identifier="anthropic/sdk"
        )
        assert dedup_temporal(candidate, conn, now=_NOW)


# ---------------------------------------------------------------------------
# run_dedup — combined pipeline
# ---------------------------------------------------------------------------


class TestRunDedup:
    """Tests for the combined run_dedup() entry point."""

    def test_empty_candidates_returns_empty(self):
        conn = _make_db()
        result = run_dedup([], conn, now=_NOW)
        assert result == []

    def test_candidate_passing_all_layers_survives(self):
        """Candidate with no conflicts passes all layers and gets a counter."""
        conn = _make_db()
        candidate = _make_candidate()
        result = run_dedup([candidate], conn, now=_NOW)
        assert len(result) == 1
        assert result[0]["signal_kind"] == "github_release"
        assert result[0]["_proposal_counter"] == 1
        # No DEDUP_SKIPPED events.
        assert _dedup_events(conn) == []

    def test_candidate_failing_semantic_is_dropped(self):
        """Candidate semantically identical to a front row is dropped."""
        conn = _make_db()
        title = "feat: add streaming support to anthropic SDK responses latency"
        _insert_front(conn, title=title)
        candidate = _make_candidate(title=title)
        result = run_dedup([candidate], conn, now=_NOW)
        assert result == []
        events = _dedup_events(conn)
        assert len(events) == 1
        assert events[0]["layer"] == "semantic"
        assert events[0]["signal_kind"] == "github_release"
        assert events[0]["target_identifier"] == "anthropic/sdk"
        assert "similarity" in events[0]

    def test_candidate_failing_temporal_is_dropped(self):
        """Candidate whose (signal_kind, target) was emitted within 14 days is dropped."""
        conn = _make_db()
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_13_DAYS_AGO,
        )
        candidate = _make_candidate()
        result = run_dedup([candidate], conn, now=_NOW)
        assert result == []
        events = _dedup_events(conn)
        assert len(events) == 1
        assert events[0]["layer"] == "temporal"

    def test_multiple_candidates_mixed_results(self):
        """Some candidates pass, some fail; counters are monotonic for passing."""
        conn = _make_db()

        # Candidate A: passes all layers.
        cand_a = _make_candidate(
            signal_kind="github_release",
            target_identifier="repo-a",
            title="completely novel idea about async runtime optimisation",
            subtitle="reduces scheduling latency by 40 percent",
        )

        # Candidate B: fails temporal (was recently emitted).
        _insert_emitted_event(
            conn,
            signal_kind="newsletter",
            target_identifier="repo-b",
            created_at=_13_DAYS_AGO,
        )
        cand_b = _make_candidate(
            signal_kind="newsletter",
            target_identifier="repo-b",
            title="completely novel but temporal duplicate",
            subtitle="",
        )

        # Candidate C: passes all layers.
        cand_c = _make_candidate(
            signal_kind="github_release",
            target_identifier="repo-c",
            title="implement parallel test runner for python packages",
            subtitle="cuts test wall time by 60 percent via process pool",
        )

        result = run_dedup([cand_a, cand_b, cand_c], conn, now=_NOW)

        # B should be dropped.
        surviving_targets = [r["target_identifier"] for r in result]
        assert "repo-a" in surviving_targets
        assert "repo-b" not in surviving_targets
        assert "repo-c" in surviving_targets

        # Counters are assigned monotonically to survivors.
        counters = [r["_proposal_counter"] for r in result]
        assert counters == sorted(counters)
        assert len(set(counters)) == len(counters), "counters must be unique"

        # Exactly one DEDUP_SKIPPED event (for B).
        assert len(_dedup_events(conn)) == 1

    def test_dedup_skipped_event_payload_shape(self):
        """EXTERNAL_PROPOSAL_DEDUP_SKIPPED payload has all required fields."""
        conn = _make_db()
        title = "feat: add streaming support to anthropic SDK responses latency"
        _insert_front(conn, title=title)
        candidate = _make_candidate(title=title)
        run_dedup([candidate], conn, now=_NOW)

        events = _dedup_events(conn)
        assert len(events) == 1
        ev = events[0]
        assert ev["layer"] in ("identifier", "semantic", "temporal")
        assert isinstance(ev["signal_kind"], str)
        assert isinstance(ev["target_identifier"], str)
        assert isinstance(ev["reason"], str)
        # Semantic layer adds similarity.
        if ev["layer"] == "semantic":
            assert "similarity" in ev
            assert isinstance(ev["similarity"], float)

    def test_counter_increments_across_survivors(self):
        """Three surviving candidates get counters 1, 2, 3."""
        conn = _make_db()
        candidates = [
            _make_candidate(
                target_identifier=f"repo-{i}",
                title=f"unique proposal {i} for a totally different area of work",
                subtitle=f"improves a different module {i}",
            )
            for i in range(3)
        ]
        result = run_dedup(candidates, conn, now=_NOW)
        assert len(result) == 3
        counters = [r["_proposal_counter"] for r in result]
        assert counters == [1, 2, 3]

    def test_counter_starts_after_existing_events(self):
        """When 2 events exist with counters 1,2 → first new survivor gets 3."""
        conn = _make_db()
        for i in (1, 2):
            _insert_emitted_event(conn, counter=i, target_identifier=f"old-repo-{i}")
        candidate = _make_candidate(
            target_identifier="brand-new-repo",
            title="novel idea not seen before anywhere in the corpus",
            subtitle="",
        )
        result = run_dedup([candidate], conn, now=_NOW)
        assert len(result) == 1
        assert result[0]["_proposal_counter"] == 3

    def test_original_candidate_fields_preserved(self):
        """Extra fields on a candidate are preserved through dedup."""
        conn = _make_db()
        candidate = _make_candidate()
        candidate["judge_score"] = 8.5
        candidate["source_kind"] = "org_release"
        result = run_dedup([candidate], conn, now=_NOW)
        assert len(result) == 1
        assert result[0]["judge_score"] == 8.5
        assert result[0]["source_kind"] == "org_release"

    def test_semantic_before_temporal(self):
        """Semantic layer fires before temporal; DEDUP_SKIPPED layer is 'semantic'."""
        conn = _make_db()
        title = "feat: add streaming support to anthropic SDK responses latency"
        # Plant both a corpus match (semantic) and a temporal event.
        _insert_front(conn, title=title)
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_13_DAYS_AGO,
        )
        candidate = _make_candidate(title=title)
        result = run_dedup([candidate], conn, now=_NOW)
        assert result == []
        events = _dedup_events(conn)
        assert len(events) == 1
        assert events[0]["layer"] == "semantic"


# ---------------------------------------------------------------------------
# scan._run_dedup integration
# ---------------------------------------------------------------------------


class TestScanRunDedupIntegration:
    """Verify that scan._run_dedup delegates to run_dedup correctly."""

    def test_scan_run_dedup_passes_candidates_through(self):
        """_run_dedup returns the candidates that survive dedup (here: all)."""
        from oxi_core.v3.auto_external.scan import _run_dedup

        conn = _make_db()
        candidate = _make_candidate(
            title="integration test unique proposal nobody duplicates",
        )
        result = _run_dedup([candidate], conn, config=None)
        # On a fresh DB, candidate should pass.
        assert len(result) == 1
        assert "_proposal_counter" in result[0]

    def test_scan_run_dedup_drops_temporal_duplicate(self):
        """_run_dedup correctly drops a temporal duplicate."""
        from oxi_core.v3.auto_external.scan import _run_dedup

        conn = _make_db()
        _insert_emitted_event(
            conn,
            signal_kind="github_release",
            target_identifier="anthropic/sdk",
            created_at=_13_DAYS_AGO,
        )
        candidate = _make_candidate()
        result = _run_dedup([candidate], conn, config=None)
        assert result == []

    def test_scan_run_dedup_empty_input(self):
        from oxi_core.v3.auto_external.scan import _run_dedup

        conn = _make_db()
        assert _run_dedup([], conn, config=None) == []
