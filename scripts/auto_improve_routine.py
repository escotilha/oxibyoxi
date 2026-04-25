#!/usr/bin/env python3
"""Entry point for the auto-improve Routine (Claude Code Routines / GHA).

Invoked once per day by the Anthropic Claude Code Routine defined in
``routines/auto-improve.toml``.  Also used by the CI smoke test
(``tests/test_auto_improve_routine.py``) and the optional GHA fallback
(T1-B11, ships only if Routines GA slips).

What this script does
---------------------
1. Discovers and registers the adapter (via ``OXI_ADAPTER`` env var or
   installed entry-point).  If no adapter is found, logs and exits 1.
2. Opens the SQLite DB via ``oxi_core.db.connect()``.
3. Builds a minimal ``EngineState`` (max_concurrent=1, plan_tier from
   adapter).
4. Calls ``auto_external.scan(conn, state)`` and handles the result:
   - ``AutoImproveAlreadyRanError`` → log + exit 0  (idempotent skip)
   - Any other exception → log + exit 1
   - Clean return → log the :class:`ScanReport` + exit 0

Credentials
-----------
No API tokens appear in this file or in ``routines/auto-improve.toml``.
The Anthropic API key is injected at runtime by the Managed Agent Vault
(see ``routines/auto-improve.toml``); the GitHub token is injected via
the MCP GitHub tool, also configured in the routine.

Idempotency
-----------
``auto_external.scan()`` raises ``AutoImproveAlreadyRanError`` if any
``external_proposal_emitted`` event was written to the ledger today
(UTC).  This script catches that exception and exits 0, so running it
twice in a day is safe.

Usage
-----
    scripts/auto_improve_routine.py [--db PATH] [--dry-run]

    --db PATH     Override DB path (default: resolved via adapter).
    --dry-run     Open DB and run idempotency check, but do NOT call
                  scan().  Useful for verifying Routine config before
                  the first real run.

Exit codes
----------
    0 — scan completed (or was cleanly skipped: already ran today / dry-run)
    1 — unrecoverable error (adapter missing, DB error, scan exception)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup — structured output for Routine log capture.
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
logger = logging.getLogger("auto_improve_routine")


# ---------------------------------------------------------------------------
# Bootstrap: make oxi-core importable when run from the repo root.
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_OXI_CORE_SRC = _REPO_ROOT / "oxi-core" / "src"
if _OXI_CORE_SRC.exists() and str(_OXI_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_OXI_CORE_SRC))


# ---------------------------------------------------------------------------
# Imports — deferred until after sys.path is patched.
# ---------------------------------------------------------------------------

def _import_engine() -> None:
    """Validate that the engine modules are importable; raise on failure."""
    import oxi_core.db  # noqa: F401
    import oxi_core.v3.auto_external  # noqa: F401
    import oxi_core.v3.engine_state  # noqa: F401


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Entry point.  Returns an exit code (0 = success, 1 = error)."""
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--db",
        metavar="PATH",
        help="Override DB path (default: resolved via adapter)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Check idempotency but skip the actual scan",
    )
    args = p.parse_args()

    # -- Validate imports ----------------------------------------------------
    try:
        _import_engine()
    except ImportError as exc:
        logger.error("auto_improve_routine: import failed — %s", exc)
        logger.error(
            "auto_improve_routine: ensure oxi-core is installed or "
            "the oxi-core/src directory is on PYTHONPATH"
        )
        return 1

    from oxi_core import db
    from oxi_core.adapter import AdapterNotRegisteredError, load_adapter
    from oxi_core.v3.auto_external import AutoImproveAlreadyRanError, ScanReport, scan
    from oxi_core.v3.engine_state import EngineState

    # -- Adapter discovery ---------------------------------------------------
    try:
        load_adapter()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "auto_improve_routine: adapter load warning — %s "
            "(proceeding without adapter; DB path must be set via --db or "
            "OXI_ADAPTER env var)",
            exc,
        )

    # -- DB path resolution --------------------------------------------------
    db_path: Path | None = Path(args.db) if args.db else None

    # -- Open DB -------------------------------------------------------------
    try:
        handle = db.connect(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("auto_improve_routine: DB connect failed — %s", exc)
        return 1

    conn = handle.connection
    logger.info("auto_improve_routine: DB at %s", handle.path)

    # -- Resolve plan_tier from adapter (or use default) ---------------------
    plan_tier = "standard"
    try:
        from oxi_core.adapter import get_active_adapter
        adapter = get_active_adapter()
        plan_tier = adapter.plan_tier() or plan_tier
    except AdapterNotRegisteredError:
        logger.info(
            "auto_improve_routine: no adapter registered — using plan_tier=%r",
            plan_tier,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "auto_improve_routine: could not read plan_tier — %s — using %r",
            exc,
            plan_tier,
        )

    # -- Build EngineState ---------------------------------------------------
    try:
        state = EngineState(plan_tier=plan_tier, max_concurrent=1)
    except ValueError as exc:
        logger.error("auto_improve_routine: EngineState build failed — %s", exc)
        conn.close()
        return 1

    # -- Dry-run short-circuit -----------------------------------------------
    if args.dry_run:
        from oxi_core.v3.auto_external import scan_already_ran_today
        count = scan_already_ran_today(conn)
        if count:
            logger.info(
                "auto_improve_routine: [dry-run] scan already ran today "
                "(%d event(s)) — would skip",
                count,
            )
        else:
            logger.info(
                "auto_improve_routine: [dry-run] no prior scan today — "
                "scan() would run"
            )
        conn.close()
        return 0

    # -- Scan ----------------------------------------------------------------
    try:
        report: ScanReport = scan(conn, state)
    except AutoImproveAlreadyRanError as exc:
        # Idempotent skip — not an error.
        logger.info("auto_improve_routine: %s", exc)
        conn.close()
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("auto_improve_routine: scan() raised — %s", exc, exc_info=True)
        conn.close()
        return 1
    finally:
        # Ensure the connection is closed regardless of outcome.
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # -- Report ---------------------------------------------------------------
    if report.stopped:
        logger.info("auto_improve_routine: engine stopped mid-scan — exiting 0")
        return 0

    logger.info(
        "auto_improve_routine: scan complete — "
        "sources=%d ranking=%d judge=%d dedup=%d emitted=%d digest=%r",
        report.sources_fetched,
        report.candidates_after_ranking,
        report.candidates_after_judge,
        report.candidates_after_dedup,
        report.proposals_emitted,
        report.digest_path or "(none)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
