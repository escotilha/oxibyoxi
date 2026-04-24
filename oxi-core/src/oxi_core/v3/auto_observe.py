"""auto_observe — engine reads its own ledger and proposes roadmap items.

The engine already auto-writes fixes via dogfood dispatch, but it doesn't
yet auto-identify *what* to fix.  ``auto_observe`` closes that loop: it
watches the ledger for recurring signal patterns and emits a proposed
roadmap entry when a pattern crosses a configurable threshold.

Signal patterns detected
------------------------
1. **Dispatch-failure cluster** — the same task accumulates
   ``dispatch_failed`` / ``dispatch_timeout`` events ≥ N times within
   a rolling time window.  Indicates a code-path that keeps breaking the
   engine's own workers.

2. **Critic-rejection cluster** — ``auto_merge_rejected`` events cluster
   on the same task ≥ N times in the window.  Indicates a quality problem
   the current worker keeps reproducing.

3. **Worktree-drift cluster** — ``abandoned_by_heartbeat`` events
   accumulate across *any* tasks above a count threshold in the window.
   Indicates systemic stale-worker pathology (network, resource pressure,
   or worktree-provision bugs).

When a pattern crosses threshold, ``scan()`` writes an
``observe_proposal`` event to the ledger (task_id=NULL; it's an
engine-level observation, not task-scoped).  The payload contains a
machine-readable summary the operator can inspect with
``oxi v3 observe`` and accept with ``oxi v3 observe --accept <id>``.

Acceptance
----------
``accept(conn, proposal_id)`` writes an ``observe_accepted`` event and
optionally injects a new row into the ``front`` table so the next
``seed_from_roadmap`` pass promotes it to a real task.  It does NOT
directly write a task row — the normal seed → plan → dispatch pipeline
handles that.

Idempotency
-----------
``scan()`` checks whether an identical pattern + target has already
generated an un-accepted proposal in the recent window before emitting a
new one.  A proposal is "identical" if its ``signal_kind`` and
``target_identifier`` fields match an existing pending proposal.  This
prevents ledger spam on repeated ticks.

Design notes
------------
- Pure DB work: no subprocesses, no adapter dependency at import time.
- Adapter is read inside ``scan()`` (for the roadmap tier label only).
- Synchronous: mirrors ``auto_recover``, ``heartbeat``, ``deep_fix``.
- Killswitch respected at entry via ``engine_state.is_stopping()``.
- All DB mutations stamp ``updated_at`` consistently.
- No new DB columns: proposals live entirely in the ``event`` table with
  ``task_id=NULL`` (the existing nullable FK allows this).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .engine_state import EngineState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default rolling window for pattern detection (hours).
DEFAULT_WINDOW_HOURS: int = 48

#: Default event count that triggers a proposal.
DEFAULT_THRESHOLD: int = 3

#: Event kind written to the ledger when a pattern is detected.
_PROPOSAL_KIND = "observe_proposal"

#: Event kind written when an operator accepts a proposal.
_ACCEPTED_KIND = "observe_accepted"

#: Signal kinds that the scanner recognises.
SIGNAL_DISPATCH_FAILURE = "dispatch_failure_cluster"
SIGNAL_CRITIC_REJECTION = "critic_rejection_cluster"
SIGNAL_WORKTREE_DRIFT = "worktree_drift_cluster"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObserveConfig:
    """Adapter-configurable observation policy.

    Attributes:
        window_hours: rolling time window for event counting.
        failure_threshold: minimum event count for dispatch / critic signals.
        drift_threshold: minimum event count for the worktree-drift signal
            (counted across ALL tasks, not per-task).
        enabled: global toggle; set ``False`` to disable entirely.
        auto_inject_front: when True, accepted proposals are immediately
            injected into the ``front`` table so the seed loop picks them
            up.  When False, acceptance is recorded in the event ledger
            only — the operator handles front insertion manually.
    """

    window_hours: int = DEFAULT_WINDOW_HOURS
    failure_threshold: int = DEFAULT_THRESHOLD
    drift_threshold: int = DEFAULT_THRESHOLD
    enabled: bool = True
    auto_inject_front: bool = True


# ---------------------------------------------------------------------------
# Report / proposal dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObserveProposal:
    """A single observation proposal as returned to callers.

    Attributes:
        event_id: the ``id`` of the ``observe_proposal`` event row.
        signal_kind: one of the ``SIGNAL_*`` constants.
        target_identifier: task identifier that triggered the signal, or
            ``"*"`` for system-wide signals (worktree drift).
        title: suggested roadmap item title.
        subtitle: suggested roadmap item subtitle (optional).
        count: number of signal events that crossed the threshold.
        window_hours: the detection window that was used.
        created_at: ISO timestamp when the proposal was written.
        accepted: True if an ``observe_accepted`` event exists for this
            proposal.
    """

    event_id: int
    signal_kind: str
    target_identifier: str
    title: str
    subtitle: str
    count: int
    window_hours: int
    created_at: str
    accepted: bool = False


@dataclass(frozen=True)
class ObserveScanReport:
    """Summary of one auto_observe scan pass.

    Attributes:
        proposals_emitted: new ``observe_proposal`` events written.
        proposals_skipped_duplicate: patterns that matched an existing
            pending proposal (idempotency guard fired).
        signals_evaluated: total signal candidates examined.
        disabled_skipped: 1 if the scan was skipped due to
            ``ObserveConfig.enabled=False``, else 0.
    """

    proposals_emitted: int
    proposals_skipped_duplicate: int
    signals_evaluated: int
    disabled_skipped: int


# ---------------------------------------------------------------------------
# DB helpers — reading signals from the ledger
# ---------------------------------------------------------------------------


def _window_cutoff(window_hours: int, now: datetime) -> str:
    """Return the ISO cutoff timestamp for the rolling window."""
    cutoff = now - timedelta(hours=window_hours)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def _dispatch_failure_counts(
    conn: sqlite3.Connection,
    cutoff: str,
) -> list[tuple[int, str, int]]:
    """Return (task_id, identifier, count) for dispatch failures in window.

    Counts ``dispatch_failed`` and ``dispatch_timeout`` events per task,
    ordered by count descending.
    """
    rows = conn.execute(
        "SELECT e.task_id, t.identifier, COUNT(*) AS n "
        "FROM event e "
        "JOIN task t ON t.id = e.task_id "
        "WHERE e.kind IN ('dispatch_failed', 'dispatch_timeout') "
        "AND e.created_at >= ? "
        "GROUP BY e.task_id "
        "ORDER BY n DESC",
        (cutoff,),
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def _critic_rejection_counts(
    conn: sqlite3.Connection,
    cutoff: str,
) -> list[tuple[int, str, int]]:
    """Return (task_id, identifier, count) for critic rejections in window."""
    rows = conn.execute(
        "SELECT e.task_id, t.identifier, COUNT(*) AS n "
        "FROM event e "
        "JOIN task t ON t.id = e.task_id "
        "WHERE e.kind = 'auto_merge_rejected' "
        "AND e.created_at >= ? "
        "GROUP BY e.task_id "
        "ORDER BY n DESC",
        (cutoff,),
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def _worktree_drift_count(
    conn: sqlite3.Connection,
    cutoff: str,
) -> int:
    """Return the total ``abandoned_by_heartbeat`` event count in window.

    This is a system-wide metric — we count across all tasks.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM event "
        "WHERE kind = 'abandoned_by_heartbeat' AND created_at >= ?",
        (cutoff,),
    ).fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# Idempotency guard — pending proposals
# ---------------------------------------------------------------------------


def _pending_proposal_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Return (signal_kind, target_identifier) pairs for un-accepted proposals.

    A proposal is "pending" if no ``observe_accepted`` event references its
    event_id.  We read all proposals, then subtract those that have an
    acceptance event.
    """
    proposal_rows = conn.execute(
        "SELECT id, payload FROM event WHERE kind = ?",
        (_PROPOSAL_KIND,),
    ).fetchall()

    if not proposal_rows:
        return set()

    # Build set of accepted proposal IDs.
    accepted_ids: set[int] = set()
    acceptance_rows = conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (_ACCEPTED_KIND,),
    ).fetchall()
    for row in acceptance_rows:
        try:
            payload = json.loads(row[0] or "{}")
            pid = payload.get("proposal_event_id")
            if pid is not None:
                accepted_ids.add(int(pid))
        except (TypeError, ValueError, KeyError):
            pass

    pending: set[tuple[str, str]] = set()
    for row in proposal_rows:
        if row[0] in accepted_ids:
            continue
        try:
            payload = json.loads(row[1] or "{}")
            sk = payload.get("signal_kind", "")
            ti = payload.get("target_identifier", "")
            if sk and ti:
                pending.add((sk, ti))
        except (TypeError, ValueError):
            pass

    return pending


# ---------------------------------------------------------------------------
# Proposal emission
# ---------------------------------------------------------------------------


def _emit_proposal(
    conn: sqlite3.Connection,
    *,
    signal_kind: str,
    target_identifier: str,
    title: str,
    subtitle: str,
    count: int,
    window_hours: int,
) -> int:
    """Write an ``observe_proposal`` event to the ledger.

    Returns the new event row's ``id``.
    """
    payload = {
        "signal_kind": signal_kind,
        "target_identifier": target_identifier,
        "title": title,
        "subtitle": subtitle,
        "count": count,
        "window_hours": window_hours,
    }
    with conn:
        cur = conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (_PROPOSAL_KIND, json.dumps(payload)),
        )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Proposal synthesis helpers
# ---------------------------------------------------------------------------


def _failure_proposal(identifier: str, count: int, window_hours: int) -> dict[str, str]:
    return {
        "title": f"fix: recurring dispatch failures on {identifier}",
        "subtitle": (
            f"Dispatch failed or timed out {count}× in {window_hours}h on "
            f"{identifier}. Investigate the worker code path and add "
            "defensive handling or a targeted test."
        ),
    }


def _rejection_proposal(identifier: str, count: int, window_hours: int) -> dict[str, str]:
    return {
        "title": f"fix: critic keeps rejecting {identifier}",
        "subtitle": (
            f"auto_merge critic rejected {count}× in {window_hours}h for "
            f"{identifier}. Identify the quality gap and either update the "
            "dispatch prompt or add a critic-bypass condition."
        ),
    }


def _drift_proposal(count: int, window_hours: int) -> dict[str, str]:
    return {
        "title": "fix: systemic worktree-drift — workers abandoned repeatedly",
        "subtitle": (
            f"{count} tasks abandoned by heartbeat in {window_hours}h. "
            "Investigate worktree provisioning latency, SSH connectivity, "
            "and the heartbeat reap timeout configuration."
        ),
    }


# ---------------------------------------------------------------------------
# Public API — scan
# ---------------------------------------------------------------------------


def scan(
    conn: sqlite3.Connection,
    engine_state: EngineState,
    *,
    config: ObserveConfig | None = None,
    now: datetime | None = None,
) -> ObserveScanReport:
    """Run one auto-observation pass.

    Reads the ledger for recurring failure patterns and emits
    ``observe_proposal`` events for any that cross the configured
    threshold and don't already have a pending (un-accepted) proposal.

    Arguments:
        conn: SQLite connection with ``row_factory = sqlite3.Row``.
        engine_state: shared stopping flag; the pass is a no-op when
            ``engine_state.is_stopping()`` is True.
        config: observation policy.  If None, tries
            ``adapter.observe_config()`` if the method exists; otherwise
            falls back to ``ObserveConfig()`` defaults.
        now: wall-clock override for tests.

    Returns an :class:`ObserveScanReport` summarising the pass.
    """
    empty = ObserveScanReport(
        proposals_emitted=0,
        proposals_skipped_duplicate=0,
        signals_evaluated=0,
        disabled_skipped=0,
    )

    if engine_state.is_stopping():
        logger.info("auto_observe: engine stopping, skipping pass")
        return empty

    if config is None:
        try:
            from ..adapter import get_active_adapter
            adapter = get_active_adapter()
            if hasattr(adapter, "observe_config"):
                config = adapter.observe_config()
            else:
                config = ObserveConfig()
        except Exception:  # noqa: BLE001
            config = ObserveConfig()

    if not config.enabled:
        logger.info("auto_observe: disabled via ObserveConfig.enabled=False")
        return ObserveScanReport(
            proposals_emitted=0,
            proposals_skipped_duplicate=0,
            signals_evaluated=0,
            disabled_skipped=1,
        )

    now_dt = now if now is not None else datetime.now(tz=UTC)
    cutoff = _window_cutoff(config.window_hours, now_dt)

    # Build the set of already-pending proposals for idempotency.
    pending_keys = _pending_proposal_keys(conn)

    emitted = 0
    skipped_dup = 0
    evaluated = 0

    # --- Signal 1: dispatch failure cluster (per-task) -------------------
    for _task_id, identifier, count in _dispatch_failure_counts(conn, cutoff):
        evaluated += 1
        if count < config.failure_threshold:
            continue
        key = (SIGNAL_DISPATCH_FAILURE, identifier)
        if key in pending_keys:
            skipped_dup += 1
            logger.debug(
                "auto_observe: dispatch_failure_cluster on %s already pending, skipping",
                identifier,
            )
            continue
        proposal = _failure_proposal(identifier, count, config.window_hours)
        _emit_proposal(
            conn,
            signal_kind=SIGNAL_DISPATCH_FAILURE,
            target_identifier=identifier,
            title=proposal["title"],
            subtitle=proposal["subtitle"],
            count=count,
            window_hours=config.window_hours,
        )
        pending_keys.add(key)  # prevent double-emit in the same pass
        emitted += 1
        logger.info(
            "auto_observe: emitted dispatch_failure_cluster proposal for %s (count=%d)",
            identifier,
            count,
        )

    # --- Signal 2: critic rejection cluster (per-task) -------------------
    for _task_id, identifier, count in _critic_rejection_counts(conn, cutoff):
        evaluated += 1
        if count < config.failure_threshold:
            continue
        key = (SIGNAL_CRITIC_REJECTION, identifier)
        if key in pending_keys:
            skipped_dup += 1
            logger.debug(
                "auto_observe: critic_rejection_cluster on %s already pending, skipping",
                identifier,
            )
            continue
        proposal = _rejection_proposal(identifier, count, config.window_hours)
        _emit_proposal(
            conn,
            signal_kind=SIGNAL_CRITIC_REJECTION,
            target_identifier=identifier,
            title=proposal["title"],
            subtitle=proposal["subtitle"],
            count=count,
            window_hours=config.window_hours,
        )
        pending_keys.add(key)
        emitted += 1
        logger.info(
            "auto_observe: emitted critic_rejection_cluster proposal for %s (count=%d)",
            identifier,
            count,
        )

    # --- Signal 3: worktree drift cluster (system-wide) ------------------
    evaluated += 1
    drift_count = _worktree_drift_count(conn, cutoff)
    if drift_count >= config.drift_threshold:
        key = (SIGNAL_WORKTREE_DRIFT, "*")
        if key in pending_keys:
            skipped_dup += 1
            logger.debug(
                "auto_observe: worktree_drift_cluster already pending, skipping"
            )
        else:
            proposal = _drift_proposal(drift_count, config.window_hours)
            _emit_proposal(
                conn,
                signal_kind=SIGNAL_WORKTREE_DRIFT,
                target_identifier="*",
                title=proposal["title"],
                subtitle=proposal["subtitle"],
                count=drift_count,
                window_hours=config.window_hours,
            )
            emitted += 1
            logger.info(
                "auto_observe: emitted worktree_drift_cluster proposal (count=%d)",
                drift_count,
            )

    return ObserveScanReport(
        proposals_emitted=emitted,
        proposals_skipped_duplicate=skipped_dup,
        signals_evaluated=evaluated,
        disabled_skipped=0,
    )


# ---------------------------------------------------------------------------
# Public API — list proposals
# ---------------------------------------------------------------------------


def list_proposals(
    conn: sqlite3.Connection,
    *,
    include_accepted: bool = False,
) -> list[ObserveProposal]:
    """Return all (or only pending) proposals from the ledger.

    Arguments:
        conn: SQLite connection.
        include_accepted: if True, returns accepted proposals too; if
            False (default) only pending proposals are returned.

    Proposals are returned in reverse-chronological order (newest first).
    """
    rows = conn.execute(
        "SELECT id, payload, created_at FROM event WHERE kind = ? ORDER BY id DESC",
        (_PROPOSAL_KIND,),
    ).fetchall()

    if not rows:
        return []

    # Build accepted proposal ID set.
    accepted_ids: set[int] = set()
    acc_rows = conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (_ACCEPTED_KIND,),
    ).fetchall()
    for row in acc_rows:
        try:
            p = json.loads(row[0] or "{}")
            pid = p.get("proposal_event_id")
            if pid is not None:
                accepted_ids.add(int(pid))
        except (TypeError, ValueError, KeyError):
            pass

    proposals: list[ObserveProposal] = []
    for row in rows:
        event_id = row[0]
        is_accepted = event_id in accepted_ids
        if not include_accepted and is_accepted:
            continue
        try:
            payload = json.loads(row[1] or "{}")
        except (TypeError, ValueError):
            payload = {}
        proposals.append(
            ObserveProposal(
                event_id=event_id,
                signal_kind=payload.get("signal_kind", "unknown"),
                target_identifier=payload.get("target_identifier", ""),
                title=payload.get("title", ""),
                subtitle=payload.get("subtitle", ""),
                count=int(payload.get("count", 0)),
                window_hours=int(payload.get("window_hours", DEFAULT_WINDOW_HOURS)),
                created_at=row[2] or "",
                accepted=is_accepted,
            )
        )
    return proposals


# ---------------------------------------------------------------------------
# Public API — accept a proposal
# ---------------------------------------------------------------------------


def accept(
    conn: sqlite3.Connection,
    proposal_event_id: int,
    *,
    config: ObserveConfig | None = None,
    next_identifier: str | None = None,
) -> bool:
    """Accept a pending proposal, optionally injecting a front-table entry.

    Writes an ``observe_accepted`` event to the ledger.  If
    ``config.auto_inject_front`` is True (the default), also upserts a
    row into the ``front`` table so the next ``seed_from_roadmap`` pass
    picks it up.

    Arguments:
        conn: SQLite connection.
        proposal_event_id: the ``id`` of the ``observe_proposal`` event
            returned by :func:`list_proposals`.
        config: observation policy.  Defaults to ``ObserveConfig()``.
        next_identifier: identifier for the new front/task row, e.g.
            ``"T3-1"``.  If None, a stable synthetic identifier is
            generated from the signal kind and target.

    Returns True on success, False if the proposal was not found or is
    already accepted.
    """
    if config is None:
        config = ObserveConfig()

    # Verify the proposal exists.
    row = conn.execute(
        "SELECT payload FROM event WHERE id = ? AND kind = ?",
        (proposal_event_id, _PROPOSAL_KIND),
    ).fetchone()
    if row is None:
        logger.warning(
            "auto_observe: accept called with unknown proposal_event_id=%d",
            proposal_event_id,
        )
        return False

    # Verify it is not already accepted.
    existing = conn.execute(
        "SELECT 1 FROM event WHERE kind = ? "
        "AND json_extract(payload, '$.proposal_event_id') = ?",
        (_ACCEPTED_KIND, proposal_event_id),
    ).fetchone()
    if existing is not None:
        logger.info(
            "auto_observe: proposal %d is already accepted, skipping",
            proposal_event_id,
        )
        return False

    try:
        proposal_payload = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        proposal_payload = {}

    signal_kind = proposal_payload.get("signal_kind", "unknown")
    target = proposal_payload.get("target_identifier", "")
    title = proposal_payload.get("title", "")
    subtitle = proposal_payload.get("subtitle", "")

    # Generate a synthetic identifier if not provided.
    if next_identifier is None:
        safe_target = target.replace("-", "_").replace("*", "all")
        next_identifier = f"observe_{signal_kind[:16]}_{safe_target}"[:64]

    # Emit the acceptance event.
    acceptance_payload = {
        "proposal_event_id": proposal_event_id,
        "signal_kind": signal_kind,
        "target_identifier": target,
        "injected_identifier": next_identifier,
    }
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (_ACCEPTED_KIND, json.dumps(acceptance_payload)),
        )

    logger.info(
        "auto_observe: accepted proposal %d (%s on %s)",
        proposal_event_id,
        signal_kind,
        target,
    )

    # Optionally inject into the front table so seed_from_roadmap picks it up.
    if config.auto_inject_front:
        _inject_front(conn, next_identifier, title, subtitle)

    return True


def _inject_front(
    conn: sqlite3.Connection,
    identifier: str,
    title: str,
    subtitle: str,
) -> None:
    """Upsert a row into the ``front`` table for the accepted proposal.

    Uses INSERT OR IGNORE so a re-accepted proposal does not reset
    ``last_seeded_at`` on an existing front row.
    """
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO front "
            "(name, description, identifier, tier, title, subtitle, active) "
            "VALUES (?, ?, ?, 99, ?, ?, 1)",
            (
                identifier,           # name (unique key)
                subtitle[:500],       # description
                identifier,           # identifier (roadmap-style key)
                title,
                subtitle,
            ),
        )
    logger.info(
        "auto_observe: injected front row for identifier %r (title=%r)",
        identifier,
        title,
    )


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_THRESHOLD",
    "DEFAULT_WINDOW_HOURS",
    "ObserveConfig",
    "ObserveProposal",
    "ObserveScanReport",
    "SIGNAL_CRITIC_REJECTION",
    "SIGNAL_DISPATCH_FAILURE",
    "SIGNAL_WORKTREE_DRIFT",
    "accept",
    "list_proposals",
    "scan",
]
