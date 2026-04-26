"""scan — top-level orchestrator for the auto-external daily run.

:func:`scan` is the single public entry point called by
``scripts/auto_improve_routine.py``.  It wires the full pipeline:

    sources → ranking → judge → dedup → emit → health

Each stage is implemented in a sibling module (B2-B9).  Until those PRs
land, each stage is a no-op stub that returns an empty result, so the
scaffolding is executable from day one.

Idempotency guard
-----------------
If any ``external_proposal_emitted`` event exists in the ledger whose
``created_at`` date (UTC) matches today, :func:`scan` raises
:exc:`AutoImproveAlreadyRanError` and exits immediately.  This prevents
duplicate runs when the Routine fires twice (e.g. a retry) or when the
B11 GHA fallback also fires on the same day.

The guard checks the ``created_at`` column of the ``event`` table with a
``DATE()`` comparison that SQLite evaluates in UTC (consistent with
``datetime('now')``).

Engine-state contract
---------------------
``engine_state.is_stopping()`` is checked at entry and between every
major stage.  A stopping engine causes :func:`scan` to return a report
with ``stopped=True``; it does NOT raise.

Design notes
------------
- Synchronous: mirrors ``auto_observe``, ``auto_recover``, ``heartbeat``.
- No subprocess at this level; sources do their own subprocess isolation.
- ``conn`` is the caller's SQLite connection — the routine opens it.
- ``now`` is injectable for tests; production callers omit it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..engine_state import EngineState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Ledger event kind written by the emit stage (B8).
#: Referenced here for the idempotency guard; the full write path is in emit.py.
EXTERNAL_PROPOSAL_EMITTED = "external_proposal_emitted"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AutoImproveAlreadyRanError(Exception):
    """Raised when a scan has already run today and the caller should skip.

    The entry-point script catches this and exits 0 with a log message.
    External orchestrators (Routines, GHA) should treat exit 0 as success.
    """

    def __init__(self, today: str, count: int) -> None:
        self.today = today
        self.count = count
        super().__init__(
            f"auto_external scan already ran today ({today}): "
            f"{count} EXTERNAL_PROPOSAL_EMITTED event(s) found — skipping"
        )


# ---------------------------------------------------------------------------
# Idempotency helper
# ---------------------------------------------------------------------------


def scan_already_ran_today(
    conn: sqlite3.Connection,
    *,
    today: str | None = None,
) -> int:
    """Return the count of ``external_proposal_emitted`` events for today (UTC).

    Arguments:
        conn: SQLite connection.  ``row_factory`` is not required.
        today: override for tests (``"YYYY-MM-DD"`` format).  When None,
            uses ``datetime.now(tz=UTC).date().isoformat()``.

    Returns the event count (0 → scan has not run today).
    """
    date_str = today if today is not None else datetime.now(tz=UTC).date().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM event "
        "WHERE kind = ? AND DATE(created_at) = ?",
        (EXTERNAL_PROPOSAL_EMITTED, date_str),
    ).fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanReport:
    """Summary of one :func:`scan` invocation.

    Attributes:
        skipped_already_ran: True when the idempotency guard fired.
        stopped: True when ``engine_state.is_stopping()`` was set at entry.
        sources_fetched: number of raw items collected across all sources.
        candidates_after_ranking: items surviving the ranking stage.
        candidates_after_judge: items surviving the LLM judge.
        candidates_after_dedup: items surviving the three-layer dedup.
        proposals_emitted: ``EXTERNAL_PROPOSAL_EMITTED`` events written.
        digest_path: path to the Markdown digest written, or empty string.
        errors: per-stage errors logged (does not include source failures
            which are recorded in the ledger separately).
    """

    skipped_already_ran: bool = False
    stopped: bool = False
    sources_fetched: int = 0
    candidates_after_ranking: int = 0
    candidates_after_judge: int = 0
    candidates_after_dedup: int = 0
    proposals_emitted: int = 0
    digest_path: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public API — scan
# ---------------------------------------------------------------------------


def scan(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    now: datetime | None = None,
) -> ScanReport:
    """Run one auto-external daily scan.

    Opens the pipeline: sources → ranking → judge → dedup → emit.  Each
    stage is a stub until its implementing PR lands.

    Arguments:
        conn: SQLite connection with foreign keys enabled.
        engine_state: shared engine state; the pass is a no-op if stopping.
        now: wall-clock override for tests.

    Returns a :class:`ScanReport` describing what happened.

    Raises:
        :exc:`AutoImproveAlreadyRanError`: when the idempotency guard
            detects that a scan already ran today.  Callers (the routine
            script) should catch this and exit 0.
    """
    now_dt = now if now is not None else datetime.now(tz=UTC)
    today = now_dt.date().isoformat()

    # -- Idempotency guard --------------------------------------------------
    count = scan_already_ran_today(conn, today=today)
    if count > 0:
        logger.info(
            "auto_external: scan already ran today (%s): %d event(s) — skipping",
            today,
            count,
        )
        raise AutoImproveAlreadyRanError(today=today, count=count)

    # -- Stopping check ------------------------------------------------------
    if engine_state.is_stopping():
        logger.info("auto_external: engine is stopping, skipping scan")
        return ScanReport(stopped=True)

    logger.info("auto_external: starting daily scan for %s", today)

    # -- Resolve adapter config ---------------------------------------------
    config = _resolve_config()

    # -- Stage: sources (B2, B3, B4) ----------------------------------------
    if engine_state.is_stopping():
        return ScanReport(stopped=True)

    raw_items = _run_sources(conn, config, now_dt)
    logger.info("auto_external: sources fetched %d raw items", len(raw_items))

    if engine_state.is_stopping():
        return ScanReport(stopped=True, sources_fetched=len(raw_items))

    # -- Stage: ranking (B5) ------------------------------------------------
    ranked = _run_ranking(raw_items, conn, config)
    logger.info("auto_external: %d candidates after ranking", len(ranked))

    if engine_state.is_stopping():
        return ScanReport(
            stopped=True,
            sources_fetched=len(raw_items),
            candidates_after_ranking=len(ranked),
        )

    # -- Stage: judge (B6) --------------------------------------------------
    judged = _run_judge(ranked, conn, engine_state, config)
    logger.info("auto_external: %d candidates after judge", len(judged))

    if engine_state.is_stopping():
        return ScanReport(
            stopped=True,
            sources_fetched=len(raw_items),
            candidates_after_ranking=len(ranked),
            candidates_after_judge=len(judged),
        )

    # -- Stage: dedup (B7) --------------------------------------------------
    deduped = _run_dedup(judged, conn, config)
    logger.info("auto_external: %d candidates after dedup", len(deduped))

    if engine_state.is_stopping():
        return ScanReport(
            stopped=True,
            sources_fetched=len(raw_items),
            candidates_after_ranking=len(ranked),
            candidates_after_judge=len(judged),
            candidates_after_dedup=len(deduped),
        )

    # -- Stage: emit (B8) ---------------------------------------------------
    proposals_emitted, digest_path = _run_emit(deduped, conn, config, today)
    logger.info(
        "auto_external: emitted %d proposal(s), digest at %s",
        proposals_emitted,
        digest_path or "(none)",
    )

    # -- Stage: health check (B9) -------------------------------------------
    _run_health(conn, config)

    return ScanReport(
        skipped_already_ran=False,
        stopped=False,
        sources_fetched=len(raw_items),
        candidates_after_ranking=len(ranked),
        candidates_after_judge=len(judged),
        candidates_after_dedup=len(deduped),
        proposals_emitted=proposals_emitted,
        digest_path=digest_path,
    )


# ---------------------------------------------------------------------------
# Stage stubs — replaced by B2-B9 PRs
# ---------------------------------------------------------------------------


def _resolve_config() -> object:
    """Return the active adapter's ``auto_improve_config()``, or a sentinel.

    Returns ``None`` when no adapter is registered or the adapter does not
    implement ``auto_improve_config()``.  Stage stubs treat ``None`` as
    "disabled/unconfigured" and return empty results.
    """
    try:
        from ...adapter import get_active_adapter

        adapter = get_active_adapter()
        if hasattr(adapter, "auto_improve_config"):
            return adapter.auto_improve_config()
    except Exception:  # noqa: BLE001  # no adapter or method absent — acceptable
        pass
    return None


def _run_sources(
    conn: sqlite3.Connection,
    config: object,
    now: datetime,
) -> list[dict]:
    """Fetch raw items from all configured sources.

    Stub until B2-B4.  Returns an empty list so the pipeline short-circuits
    cleanly.  The full implementation iterates GitHub, Newsletter, and X
    sources, each wrapped in try/except that emits an
    ``auto_improve_source_failed`` event on failure.
    """
    _ = conn, config, now  # noqa: F841 — will be used by B2-B4
    logger.debug("auto_external: sources stub — returning []")
    return []


def _run_ranking(
    raw_items: list[dict],
    conn: sqlite3.Connection,
    config: object,
) -> list[dict]:
    """Rank and filter raw items.

    Stub until B5.  Returns ``raw_items`` unchanged so the pipeline
    remains exercisable end-to-end with synthetic test data.
    """
    _ = conn, config  # noqa: F841
    return raw_items


def _run_judge(
    candidates: list[dict],
    conn: sqlite3.Connection,
    engine_state: EngineState,
    config: object,
) -> list[dict]:
    """Apply the LLM judge rubric.

    Stub until B6.  Passes all candidates through without LLM calls.
    """
    _ = conn, engine_state, config  # noqa: F841
    return candidates


def _run_dedup(
    candidates: list[dict],
    conn: sqlite3.Connection,
    config: object,
) -> list[dict]:
    """Apply three-layer deduplication (B7 implementation).

    Delegates to :func:`oxi_core.v3.auto_external.dedup.run_dedup` which
    applies identifier → semantic → temporal layers in order.  Candidates
    that are dropped by any layer generate an
    ``EXTERNAL_PROPOSAL_DEDUP_SKIPPED`` ledger event.

    ``config`` is accepted but unused at this stage; dedup thresholds use
    module-level defaults (``SEMANTIC_THRESHOLD=0.85``,
    ``TEMPORAL_WINDOW_DAYS=14``).  A future B7-extension PR may thread
    adapter-configurable thresholds through here.
    """
    _ = config  # noqa: F841
    from .dedup import run_dedup

    return run_dedup(candidates, conn)


def _run_emit(
    proposals: list[dict],
    conn: sqlite3.Connection,
    config: object,
    today: str,
) -> tuple[int, str]:
    """Write ledger events and the Markdown digest.

    Stub until B8.  When ``proposals`` is empty (the stub sources return
    nothing), writes a minimal "no proposals" digest and emits zero events.

    Returns ``(proposals_emitted, digest_path)``.
    """
    _ = config  # noqa: F841
    if not proposals:
        logger.debug("auto_external: emit stub — no proposals to emit")
        return 0, ""

    # Write placeholder EXTERNAL_PROPOSAL_EMITTED events so that real
    # proposals (injected by tests via the conn directly) can be counted
    # by the idempotency guard in subsequent runs.
    emitted = 0
    for proposal in proposals:
        payload = json.dumps({"stub": True, **proposal})
        with conn:
            conn.execute(
                "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
                (EXTERNAL_PROPOSAL_EMITTED, payload),
            )
        emitted += 1

    return emitted, ""


def _run_health(
    conn: sqlite3.Connection,
    config: object,
) -> None:
    """Update the auto-improve health tracker.

    Stub until B9.  No-op.
    """
    _ = conn, config  # noqa: F841


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "EXTERNAL_PROPOSAL_EMITTED",
    "AutoImproveAlreadyRanError",
    "ScanReport",
    "scan",
    "scan_already_ran_today",
]
