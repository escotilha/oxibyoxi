"""Continuous-dispatch supervisor — ``oxi v3 saturate``.

Replaces the bash ``~/.oxi-supervisor.sh`` with a first-class engine
subcommand.  A single ``oxi v3 saturate`` invocation loops indefinitely,
running ingest → auto_observe → seed → dispatch_loop on every iteration
until one of the following hard-stop conditions fires:

1. **Daily budget exhausted** — the budget gate trips
   ``HARD_STOP`` for the current UTC day. A ``daily_budget_reached``
   event is written to the ledger and the loop exits cleanly (workers
   already in-flight are never killed).

2. **Killswitch set** — ``kill.is_set()`` returns True (operator ran
   ``oxi v3 kill`` from another terminal, or the file was created by an
   external tool).  The loop drains in-flight workers and exits 0.

3. **SIGTERM** — signal-safe shutdown: the handler sets the
   ``EngineState.request_stop()`` flag and the loop exits after the
   current dispatch completes.  In-flight workers are never killed —
   ``saturate`` waits for them before writing the PID-file tombstone and
   exiting.

Daemon-readiness
----------------
A PID file is written at ``adapter.paths().repo_root + ".oxi/saturate.pid"``
(i.e. ``<repo_root>/.oxi/saturate.pid``).  On startup ``saturate``
checks whether the PID file exists and whether the recorded PID is live;
if so it refuses to start, printing a clear error.  On clean exit (any
of the three stop paths above) the PID file is removed.

Daemon wrappers (launchd, systemd, tmux) need no special shim — just
run ``oxi v3 saturate`` and let the normal process lifecycle do the rest.

Loop body
---------
Each iteration:

1. ``ingest_roadmap.ingest()`` — re-read the roadmap file and upsert
   fronts (idempotent; cheap; picks up in-flight roadmap edits).
2. ``auto_observe.scan()`` — check ledger for recurring failure patterns
   and emit proposals.
3. ``seed_from_roadmap.seed()`` — replenish the planned-task queue if
   below target.
4. ``dispatch.dispatch_loop()`` — run up to ``concurrency`` dispatches
   in parallel; each checks the budget gate before starting.

The inter-iteration sleep is 60 seconds when the dispatch loop found
nothing to do (no planned tasks), or 0 seconds otherwise, so the
supervisor self-throttles on an empty queue without spinning.

Architecture notes
------------------
- No new DB columns.  Everything is expressed via existing ledger events.
- ``dispatch_loop`` does its own concurrency management via
  ``_dispatch_loop_parallel``; ``saturate`` only sets the ``--concurrency``
  ceiling.
- ``max_cost_per_day`` is plumbed into the adapter's hard-cap check via
  an override mechanism: when the caller passes a value, it's compared
  against the current day's spend before entering each iteration rather
  than relying solely on the adapter's ``BudgetCaps``.  This lets
  operators lower the limit at the CLI without editing adapter code.
- Signal handling is best-effort: SIGTERM calls
  ``engine_state.request_stop()``.  On Windows SIGTERM has subtleties;
  this code targets macOS/Linux.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Ledger event kind emitted when the daily budget ceiling is reached.
EVENT_DAILY_BUDGET_REACHED = "daily_budget_reached"

#: Ledger event kind emitted when saturate starts.
EVENT_SATURATE_STARTED = "saturate_started"

#: Ledger event kind emitted when saturate exits.
EVENT_SATURATE_STOPPED = "saturate_stopped"

#: Default sleep between iterations when the queue is empty (seconds).
DEFAULT_IDLE_SLEEP_S: int = 60

#: Default sleep between iterations when work was done (seconds).
DEFAULT_ACTIVE_SLEEP_S: int = 5


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def pid_path(repo_root: Path, oxi_dir: str = ".oxi") -> Path:
    """Return the path to the saturate PID file.

    Convention: ``<repo_root>/<oxi_dir>/saturate.pid``.
    """
    return repo_root / oxi_dir / "saturate.pid"


def _read_pid(path: Path) -> int | None:
    """Return the PID stored in ``path``, or None if absent/unparseable."""
    try:
        text = path.read_text().strip()
        return int(text)
    except (FileNotFoundError, ValueError, OSError):
        return None


def _pid_is_live(pid: int) -> bool:
    """Return True if the process with ``pid`` is currently running.

    Uses ``os.kill(pid, 0)``; on permission error (process exists but
    owned by another user), conservatively returns True.
    """
    try:
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just not ours


def check_pid_file(path: Path) -> tuple[bool, int | None]:
    """Check whether a live saturate instance already holds the PID file.

    Returns:
        (live, pid) where ``live`` is True if another process appears to
        be running, ``pid`` is the recorded PID (or None).
    """
    pid = _read_pid(path)
    if pid is None:
        return (False, None)
    live = _pid_is_live(pid)
    return (live, pid)


def write_pid_file(path: Path) -> None:
    """Write the current process's PID to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()) + "\n")


def remove_pid_file(path: Path) -> None:
    """Delete the PID file if it exists."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Run report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SaturateReport:
    """Summary returned by ``run()`` on exit.

    Attributes:
        iterations: number of full loop iterations completed.
        total_dispatched: total dispatch results received (all classifications).
        stop_reason: human-readable string explaining why the loop ended.
        exit_code: suggested process exit code (0 = clean, 1 = error).
    """

    iterations: int
    total_dispatched: int
    stop_reason: str
    exit_code: int


# ---------------------------------------------------------------------------
# Budget override check
# ---------------------------------------------------------------------------


def _day_spend_exceeds_cap(
    conn: sqlite3.Connection,
    max_cost_per_day_usd: float | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True if today's spend already meets or exceeds *max_cost_per_day_usd*.

    This is a pre-iteration check that lets the CLI-supplied ``--max-cost-per-day``
    flag act as a tighter ceiling than the adapter's hard cap.  When
    ``max_cost_per_day_usd`` is None, this always returns False (no extra cap).
    """
    if max_cost_per_day_usd is None:
        return False

    now_dt = now if now is not None else datetime.now(tz=UTC)
    window_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    since_str = window_start.strftime("%Y-%m-%d %H:%M:%S")

    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM task WHERE updated_at >= ?",
        (since_str,),
    ).fetchone()
    today_spend = float(row[0] or 0.0)
    return today_spend >= max_cost_per_day_usd


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run(
    *,
    conn: sqlite3.Connection,
    concurrency: int,
    max_cost_per_day_usd: float | None = None,
    idle_sleep_s: int = DEFAULT_IDLE_SLEEP_S,
    active_sleep_s: int = DEFAULT_ACTIVE_SLEEP_S,
    repo_root: Path,
    oxi_dir: str = ".oxi",
    anthropic_api_key: str | None = None,
    # Injectable for tests.
    _now: datetime | None = None,
) -> SaturateReport:
    """Continuous dispatch loop.

    Caller must have already registered an adapter and set up the DB
    connection.  ``conn`` is used for all budget + ledger operations;
    dispatch internally opens its own connection for concurrent safety
    when ``concurrency > 1``.

    Arguments:
        conn: SQLite connection (row_factory = sqlite3.Row).
        concurrency: maximum parallel dispatches per iteration.
        max_cost_per_day_usd: optional per-day spend ceiling.  When set,
            overrides the adapter's hard cap at the saturate level.
        idle_sleep_s: sleep between iterations when nothing was dispatched.
        active_sleep_s: sleep between iterations when work was dispatched.
        repo_root: repository root (adapter paths relative to this).
        oxi_dir: sub-directory under repo_root that holds PID file etc.
        anthropic_api_key: forwarded verbatim to dispatch_one.
        _now: wall-clock override (tests only).

    Returns a ``SaturateReport`` describing why the loop stopped.
    """
    from ..adapter import get_active_adapter
    from . import auto_observe, dispatch, ingest_roadmap, seed_from_roadmap
    from . import budget as budget_mod
    from . import kill as kill_mod
    from .engine_health import EngineHealth
    from .engine_state import EngineState

    adapter = get_active_adapter()

    state = EngineState(
        plan_tier=adapter.plan_tier(),
        max_concurrent=concurrency,
    )
    health = EngineHealth()

    _pid_path = pid_path(repo_root, oxi_dir)

    # --- SIGTERM handler (signal-safe) ------------------------------------
    def _handle_sigterm(signum: int, frame: object) -> None:  # noqa: ARG001
        logger.warning("saturate: SIGTERM received — requesting clean stop")
        state.request_stop()

    original_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # --- PID file ---------------------------------------------------------
    write_pid_file(_pid_path)

    # --- Startup event ----------------------------------------------------
    now_dt = _now if _now is not None else datetime.now(tz=UTC)
    _emit_event(
        conn,
        EVENT_SATURATE_STARTED,
        {
            "pid": os.getpid(),
            "concurrency": concurrency,
            "max_cost_per_day_usd": max_cost_per_day_usd,
            "timestamp": now_dt.isoformat(),
        },
    )

    iterations = 0
    total_dispatched = 0
    stop_reason = "unknown"
    exit_code = 0

    logger.info(
        "saturate: starting (concurrency=%d, max_cost_per_day=%.2f)",
        concurrency,
        max_cost_per_day_usd if max_cost_per_day_usd is not None else float("inf"),
    )

    try:
        while True:
            if state.is_stopping():
                stop_reason = "sigterm"
                break

            if kill_mod.is_set():
                logger.info("saturate: killswitch active — stopping cleanly")
                stop_reason = "killswitch"
                state.request_stop()
                break

            # CLI-level budget cap (tighter than adapter cap).
            if _day_spend_exceeds_cap(conn, max_cost_per_day_usd, now=_now):
                logger.info(
                    "saturate: daily budget cap ($%.2f) reached — stopping",
                    max_cost_per_day_usd,
                )
                _emit_event(
                    conn,
                    EVENT_DAILY_BUDGET_REACHED,
                    {
                        "max_cost_per_day_usd": max_cost_per_day_usd,
                        "timestamp": (
                            (_now if _now is not None else datetime.now(tz=UTC)).isoformat()
                        ),
                    },
                )
                stop_reason = "daily_budget_reached"
                break

            # Adapter-level hard cap (budget.check emits its own events).
            if budget_mod.is_hard_stopped(conn):
                logger.info("saturate: adapter budget hard-stop — stopping")
                stop_reason = "budget_hard_stop"
                break

            if health.is_unhealthy():
                logger.warning(
                    "saturate: engine UNHEALTHY — stopping. "
                    "Run `oxi v3 heal` then restart saturate."
                )
                stop_reason = "engine_unhealthy"
                exit_code = 1
                break

            # ------------ per-iteration body ----------------------------

            # 1. Ingest roadmap (idempotent).
            try:
                ingest_report = ingest_roadmap.ingest(conn, repo_root=repo_root)
                logger.debug(
                    "saturate[%d]: ingest inserted=%d updated=%d",
                    iterations,
                    ingest_report.inserted,
                    ingest_report.updated,
                )
            except FileNotFoundError as exc:
                logger.warning("saturate: roadmap not found, skipping ingest: %s", exc)

            # 2. Auto-observe scan.
            try:
                observe_report = auto_observe.scan(conn, state)
                if observe_report.proposals_emitted:
                    logger.info(
                        "saturate[%d]: auto_observe emitted %d proposal(s)",
                        iterations,
                        observe_report.proposals_emitted,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("saturate: auto_observe.scan failed: %s", exc)

            # 3. Seed.
            try:
                seed_report = seed_from_roadmap.seed(conn, state)
                if seed_report.seeded:
                    logger.info(
                        "saturate[%d]: seeded %d task(s) (planned_after=%d)",
                        iterations,
                        seed_report.seeded,
                        seed_report.planned_after,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("saturate: seed failed: %s", exc)

            # 4. Dispatch loop.
            try:
                results = await dispatch.dispatch_loop(
                    conn=conn,
                    adapter=adapter,
                    engine_state=state,
                    engine_health=health,
                    repo_root=repo_root,
                    max_iterations=concurrency,
                    concurrency=concurrency,
                    anthropic_api_key=anthropic_api_key,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "saturate[%d]: dispatch_loop raised: %s", iterations, exc
                )
                results = []

            iterations += 1
            total_dispatched += len(results)

            # Sleep between iterations.  Short sleep when we dispatched
            # something (more work may be ready quickly); longer sleep
            # on an empty queue (prevents tight spin when idle).
            if state.is_stopping():
                break

            sleep_s = active_sleep_s if results else idle_sleep_s
            logger.debug(
                "saturate[%d]: sleeping %ds (dispatched=%d)",
                iterations,
                sleep_s,
                len(results),
            )

            # Interruptible sleep — check stop every second.
            for _ in range(sleep_s):
                if state.is_stopping():
                    break
                await asyncio.sleep(1)

    finally:
        # Always clean up PID file + restore signal handler.
        remove_pid_file(_pid_path)
        signal.signal(signal.SIGTERM, original_sigterm)

        _emit_event(
            conn,
            EVENT_SATURATE_STOPPED,
            {
                "pid": os.getpid(),
                "iterations": iterations,
                "total_dispatched": total_dispatched,
                "stop_reason": stop_reason,
                "exit_code": exit_code,
                "timestamp": (
                    (_now if _now is not None else datetime.now(tz=UTC)).isoformat()
                ),
            },
        )
        logger.info(
            "saturate: stopped (reason=%s, iterations=%d, dispatched=%d)",
            stop_reason,
            iterations,
            total_dispatched,
        )

    return SaturateReport(
        iterations=iterations,
        total_dispatched=total_dispatched,
        stop_reason=stop_reason,
        exit_code=exit_code,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_event(conn: sqlite3.Connection, kind: str, payload: dict) -> None:
    """Insert a task_id=NULL engine-level event."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
                (kind, json.dumps(payload)),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("saturate: failed to emit %r event: %s", kind, exc)


__all__ = [
    "DEFAULT_ACTIVE_SLEEP_S",
    "DEFAULT_IDLE_SLEEP_S",
    "EVENT_DAILY_BUDGET_REACHED",
    "EVENT_SATURATE_STARTED",
    "EVENT_SATURATE_STOPPED",
    "SaturateReport",
    "check_pid_file",
    "pid_path",
    "remove_pid_file",
    "run",
    "write_pid_file",
]
