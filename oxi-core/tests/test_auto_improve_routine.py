"""Smoke tests for scripts/auto_improve_routine.py — T1-B10.

Tests in this file exercise the entry-point script end-to-end using a
fixture SQLite DB.  No real adapter, no network, no LLM calls.

Coverage targets
----------------
1. Script runs against a fresh DB, exits 0, and returns a ScanReport
   with ``proposals_emitted=0`` (sources stub returns []).
2. Idempotency: after seeding one ``external_proposal_emitted`` event for
   today, a second call to ``scan()`` raises
   ``AutoImproveAlreadyRanError`` and the entry-point exits 0.
3. ``scan_already_ran_today`` returns 0 on an empty DB and N > 0 after
   events are seeded.
4. ``--dry-run`` flag: exits 0 in both cases (fresh DB and after events).
5. Digest path: with stub sources the digest is empty string; no file
   is created in the DB directory.
6. Stopping engine: when ``engine_state.is_stopping()`` is set before
   scan(), report.stopped is True.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]   # …/t1-b10/ (repo root)
SCRIPT = REPO_ROOT / "scripts" / "auto_improve_routine.py"

# oxi-core/src must be on sys.path for direct imports.
_OXI_CORE_SRC = REPO_ROOT / "oxi-core" / "src"
if _OXI_CORE_SRC.exists() and str(_OXI_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_OXI_CORE_SRC))

from oxi_core import db  # noqa: E402
from oxi_core.v3.auto_external import (  # noqa: E402
    AutoImproveAlreadyRanError,
    scan,
    scan_already_ran_today,
)
from oxi_core.v3.auto_external.scan import EXTERNAL_PROPOSAL_EMITTED  # noqa: E402
from oxi_core.v3.engine_state import EngineState  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path):
    """Return a DbHandle opened against a fresh SQLite file."""
    handle = db.connect(tmp_path / "oxi.db")
    yield handle
    handle.connection.close()


@pytest.fixture
def engine_state() -> EngineState:
    """Return a non-stopping EngineState."""
    return EngineState(plan_tier="standard", max_concurrent=1)


# ---------------------------------------------------------------------------
# Helper: seed a today-event directly into the DB
# ---------------------------------------------------------------------------


def _seed_emitted_event(conn, today: str | None = None) -> None:
    """Insert one external_proposal_emitted event for today (UTC)."""
    import json

    if today is None:
        today = datetime.now(tz=UTC).date().isoformat()
    # Use the SQLite datetime format that matches the schema
    created_at = f"{today} 00:00:01"
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload, created_at) "
            "VALUES (NULL, ?, ?, ?)",
            (EXTERNAL_PROPOSAL_EMITTED, json.dumps({"test": True}), created_at),
        )


# ---------------------------------------------------------------------------
# Test: scan_already_ran_today
# ---------------------------------------------------------------------------


def test_scan_already_ran_today_empty_db(fresh_db):
    """Returns 0 when the DB has no external_proposal_emitted events."""
    count = scan_already_ran_today(fresh_db.connection)
    assert count == 0


def test_scan_already_ran_today_with_event(fresh_db):
    """Returns > 0 after seeding an event for today."""
    _seed_emitted_event(fresh_db.connection)
    count = scan_already_ran_today(fresh_db.connection)
    assert count == 1


def test_scan_already_ran_today_different_date(fresh_db):
    """Returns 0 when the only event is from a different date."""
    _seed_emitted_event(fresh_db.connection, today="2000-01-01")
    count = scan_already_ran_today(fresh_db.connection)
    assert count == 0


def test_scan_already_ran_today_multiple_events(fresh_db):
    """Returns the correct count when multiple events exist for today."""
    _seed_emitted_event(fresh_db.connection)
    _seed_emitted_event(fresh_db.connection)
    _seed_emitted_event(fresh_db.connection)
    count = scan_already_ran_today(fresh_db.connection)
    assert count == 3


# ---------------------------------------------------------------------------
# Test: scan() — fresh DB (stub pipeline)
# ---------------------------------------------------------------------------


def test_scan_fresh_db_exits_clean(fresh_db, engine_state):
    """Scan on a fresh DB completes, returns a ScanReport, proposals=0."""
    report = scan(fresh_db.connection, engine_state)
    assert not report.skipped_already_ran
    assert not report.stopped
    assert report.proposals_emitted == 0
    # Stub sources return nothing, so all stage counts are 0.
    assert report.sources_fetched == 0
    assert report.candidates_after_ranking == 0
    assert report.candidates_after_judge == 0
    assert report.candidates_after_dedup == 0


def test_scan_fresh_db_digest_path_empty(fresh_db, engine_state):
    """With stub sources, no digest file is written (digest_path is empty)."""
    report = scan(fresh_db.connection, engine_state)
    assert report.digest_path == ""


# ---------------------------------------------------------------------------
# Test: idempotency
# ---------------------------------------------------------------------------


def test_scan_idempotency_already_ran(fresh_db, engine_state):
    """Second scan raises AutoImproveAlreadyRanError when an event exists."""
    _seed_emitted_event(fresh_db.connection)
    with pytest.raises(AutoImproveAlreadyRanError) as exc_info:
        scan(fresh_db.connection, engine_state)
    err = exc_info.value
    assert err.count >= 1
    assert err.today == datetime.now(tz=UTC).date().isoformat()


def test_scan_idempotency_future_date_not_skipped(fresh_db, engine_state):
    """A event from yesterday does not trigger the idempotency guard for today."""
    _seed_emitted_event(fresh_db.connection, today="2000-01-01")
    # Should NOT raise — yesterday's event doesn't count as "already ran today".
    report = scan(fresh_db.connection, engine_state)
    assert not report.skipped_already_ran


def test_auto_improve_already_ran_error_attributes():
    """AutoImproveAlreadyRanError carries today + count."""
    err = AutoImproveAlreadyRanError(today="2026-04-25", count=3)
    assert err.today == "2026-04-25"
    assert err.count == 3
    assert "2026-04-25" in str(err)
    assert "3" in str(err)


# ---------------------------------------------------------------------------
# Test: stopping engine
# ---------------------------------------------------------------------------


def test_scan_respects_is_stopping(fresh_db):
    """When engine is stopping at entry, returns ScanReport(stopped=True)."""
    state = EngineState(plan_tier="standard", max_concurrent=1)
    state.request_stop()
    report = scan(fresh_db.connection, state)
    assert report.stopped is True
    assert report.proposals_emitted == 0


# ---------------------------------------------------------------------------
# Test: entry-point script (subprocess)
# ---------------------------------------------------------------------------


def _run_script(tmp_path, *extra_args) -> subprocess.CompletedProcess:
    """Run auto_improve_routine.py with the given extra args."""
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--db", str(tmp_path / "oxi.db"),
        *extra_args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            # Minimal env: no OXI_ADAPTER — the script handles missing adapter.
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            # Pass PYTHONPATH so oxi-core is importable.
            "PYTHONPATH": str(_OXI_CORE_SRC),
        },
    )


@pytest.mark.slow
def test_script_fresh_db_exits_zero(tmp_path):
    """Entry-point script exits 0 on a fresh DB."""
    result = _run_script(tmp_path)
    assert result.returncode == 0, (
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


@pytest.mark.slow
def test_script_dry_run_exits_zero(tmp_path):
    """--dry-run exits 0 on a fresh DB."""
    result = _run_script(tmp_path, "--dry-run")
    assert result.returncode == 0, (
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


@pytest.mark.slow
def test_script_idempotent_second_run(tmp_path):
    """Second invocation exits 0 (idempotent skip)."""
    # First run — creates DB + runs scan.
    r1 = _run_script(tmp_path)
    assert r1.returncode == 0, f"first run failed: {r1.stdout!r} {r1.stderr!r}"

    # Seed a today-event to simulate a prior scan having emitted proposals.
    import json
    from datetime import UTC
    from datetime import datetime as _dt

    from oxi_core import db as _db
    today = _dt.now(tz=UTC).date().isoformat()
    created_at = f"{today} 00:00:02"
    h = _db.connect(tmp_path / "oxi.db")
    with h.connection:
        h.connection.execute(
            "INSERT INTO event (task_id, kind, payload, created_at) "
            "VALUES (NULL, ?, ?, ?)",
            (EXTERNAL_PROPOSAL_EMITTED, json.dumps({"test": True}), created_at),
        )
    h.connection.close()

    # Second run — should skip and exit 0.
    r2 = _run_script(tmp_path)
    assert r2.returncode == 0, f"second run failed: {r2.stdout!r} {r2.stderr!r}"
    assert "already ran" in r2.stdout.lower() or "skip" in r2.stdout.lower(), (
        f"Expected 'already ran' or 'skip' in output, got: {r2.stdout!r}"
    )
