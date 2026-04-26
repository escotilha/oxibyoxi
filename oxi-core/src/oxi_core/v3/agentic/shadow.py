"""``shadow`` — codex shadow-run harness for ``ClaudeCodeAdapter`` calls.

Overview
--------
When the operator sets ``OXI_AGENTIC_SHADOW=codex``, every
``dispatch_invoke.invoke()`` call is shadowed: the same prompt is
dispatched concurrently to a ``CodexCliAdapter`` in a sibling worktree.
Both outcomes are then compared and persisted to the ledger as paired
``agentic_shadow_observed`` events.

**Zero behavior change** — the shadow result is observed only.  The
primary ``DispatchResult`` from the Claude invocation is returned to the
caller unchanged; the codex shadow result is fire-and-forget from the
caller's perspective.  Divergence or codex failures are logged and
persisted for offline analysis; they never affect the primary outcome.

Architecture
------------
The shadow harness is a thin async wrapper:

1. ``should_shadow()`` — reads ``OXI_AGENTIC_SHADOW`` from the
   environment.  Returns True only when the value is ``"codex"``
   (case-insensitive).  No other values are supported today.

2. ``run_shadow()`` — the main entry point.  Given a primary
   ``DispatchInvocation`` and its already-computed ``DispatchResult``,
   spawns the codex shadow concurrently (non-blocking from the caller's
   perspective — ``asyncio.create_task``), then returns the primary
   result immediately.  The background task persists the paired event
   to the ledger when it completes.

3. ``compare_results()`` — pure function: computes ``shape_match`` and
   ``cost_delta_usd`` from the two results.  ``shape_match`` is True
   when both results share the same ``Classification`` value.  This is
   the coarsest useful signal: at the analysis stage we care first about
   whether both engines agree on success vs. failure, not about the
   exact bytes each produced.

4. ``persist_shadow_event()`` — writes one ``agentic_shadow_observed``
   event to the ledger.  The event is not tied to a specific task_id
   (``task_id=None``) because the shadow is a cross-cutting concern;
   the ``session_id`` field in the payload links it to the primary
   dispatch.

Worktree strategy
-----------------
Codex requires a real git worktree to operate (it reads/writes files).
The shadow harness provisions a sibling worktree under the same
``worktree_root`` as the primary dispatch, with a ``shadow-`` prefix on
the branch name.  The worktree is created lazily and left on disk for
inspection; it is NOT automatically cleaned up (that's the branch
janitor's job).

If worktree provisioning fails, the shadow is skipped and a warning is
logged — never a crash.

Environment variable
--------------------
``OXI_AGENTIC_SHADOW``
    Set to ``"codex"`` to enable shadow-runs.  Any other value (or
    absent) disables the harness entirely.  The value is checked at
    call time, not at import time, so operators can toggle it live
    (e.g. in a running saturate loop) without restarting the engine.

Ledger event
------------
Kind: ``agentic_shadow_observed`` (``LedgerEvent.AGENTIC_SHADOW_OBSERVED``)

Payload schema::

    {
        "session_id":         str,   # primary DispatchInvocation.session_id
        "primary_class":      str,   # Classification.value of the primary result
        "shadow_class":       str,   # Classification.value of the shadow result
        "shape_match":        bool,  # primary_class == shadow_class
        "primary_cost_usd":   float,
        "shadow_cost_usd":    float,
        "cost_delta_usd":     float, # shadow_cost_usd - primary_cost_usd
        "primary_wall_s":     float,
        "shadow_wall_s":      float,
        "shadow_backend":     str,   # "codex" today
        "shadow_exit_code":   int | null,
        "shadow_error":       str,   # non-empty only when shadow itself failed
    }

Dashboard
---------
The ``dashboard.py`` ``render_html()`` function picks up the last 50
``agentic_shadow_observed`` events and renders an "Agentic shadow" panel
showing:

- Agreement rate (% shape_match over the last 50 runs)
- Mean cost delta (shadow cost − primary cost)
- Per-row table: session_id (truncated), primary_class, shadow_class,
  shape_match glyph, cost_delta_usd
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..dispatch_invoke import Classification, DispatchInvocation, DispatchResult
from ..ledger_events import LedgerEvent

if TYPE_CHECKING:
    from .codex import CodexCliAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Environment variable that enables the shadow harness.
ENV_VAR = "OXI_AGENTIC_SHADOW"

#: The only accepted value for ``OXI_AGENTIC_SHADOW`` (case-insensitive).
SHADOW_BACKEND_CODEX = "codex"

#: Branch prefix for shadow worktrees.
SHADOW_BRANCH_PREFIX = "shadow-"

#: Wall-clock timeout for shadow invocations.  Longer than the typical
#: codex run to avoid false negatives on slow tasks, but short enough to
#: not leave zombie worktrees open indefinitely.
SHADOW_WALL_CLOCK_TIMEOUT_S = 900.0  # 15 minutes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def should_shadow() -> bool:
    """Return True when ``OXI_AGENTIC_SHADOW=codex`` is set.

    Checked at call time so operators can toggle it without restarting
    the engine.  Case-insensitive comparison.
    """
    value = os.environ.get(ENV_VAR, "").strip().lower()
    return value == SHADOW_BACKEND_CODEX


@dataclass(frozen=True)
class ShadowComparison:
    """Outcome of comparing a primary and shadow ``DispatchResult``.

    Attributes:
        shape_match: True when both results share the same
            ``Classification`` (e.g. both SUCCESS, or both FAILED).
            This is the coarsest useful agreement signal.
        primary_cost_usd: Cost reported by the primary (Claude) result.
        shadow_cost_usd: Cost reported by the shadow (codex) result.
        cost_delta_usd: ``shadow_cost_usd - primary_cost_usd``.  Negative
            means the shadow was cheaper; positive means it was more
            expensive.
    """

    shape_match: bool
    primary_cost_usd: float
    shadow_cost_usd: float
    cost_delta_usd: float


def compare_results(
    primary: DispatchResult,
    shadow: DispatchResult,
) -> ShadowComparison:
    """Compare primary and shadow ``DispatchResult``\\s.

    Pure function — no I/O.

    Parameters
    ----------
    primary:
        The ``DispatchResult`` from the primary (Claude) invocation.
    shadow:
        The ``DispatchResult`` from the shadow (codex) invocation.

    Returns
    -------
    ShadowComparison
        Comparison metrics.
    """
    shape_match = primary.classification is shadow.classification
    cost_delta = shadow.cost_usd - primary.cost_usd
    return ShadowComparison(
        shape_match=shape_match,
        primary_cost_usd=primary.cost_usd,
        shadow_cost_usd=shadow.cost_usd,
        cost_delta_usd=cost_delta,
    )


def persist_shadow_event(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    primary: DispatchResult,
    shadow: DispatchResult,
    comparison: ShadowComparison,
    shadow_error: str = "",
) -> None:
    """Write one ``agentic_shadow_observed`` event to the ledger.

    The event is not tied to a specific ``task_id``; the ``session_id``
    field in the payload links it to the primary dispatch.

    Parameters
    ----------
    conn:
        Open SQLite connection (migrations already applied).
    session_id:
        The ``session_id`` from the primary ``DispatchInvocation``.
    primary:
        Primary dispatch result (from Claude).
    shadow:
        Shadow dispatch result (from codex).
    comparison:
        Pre-computed ``ShadowComparison``.
    shadow_error:
        Non-empty string if the shadow harness itself failed (e.g.
        worktree provisioning error) — distinct from a codex *result*
        classification of FAILED.
    """
    payload = {
        "session_id": session_id,
        "primary_class": primary.classification.value,
        "shadow_class": shadow.classification.value,
        "shape_match": comparison.shape_match,
        "primary_cost_usd": comparison.primary_cost_usd,
        "shadow_cost_usd": comparison.shadow_cost_usd,
        "cost_delta_usd": comparison.cost_delta_usd,
        "primary_wall_s": primary.wall_clock_seconds,
        "shadow_wall_s": shadow.wall_clock_seconds,
        "shadow_backend": SHADOW_BACKEND_CODEX,
        "shadow_exit_code": shadow.exit_code,
        "shadow_error": shadow_error,
    }
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (None, LedgerEvent.AGENTIC_SHADOW_OBSERVED, json.dumps(payload)),
        )


async def run_shadow(
    *,
    invocation: DispatchInvocation,
    primary_result: DispatchResult,
    conn: sqlite3.Connection,
    codex_adapter: "CodexCliAdapter",
    worktree_root: Path,
) -> None:
    """Dispatch a shadow run to codex and persist the paired event.

    This coroutine is designed to be launched as an ``asyncio.Task`` (fire
    and forget) so it does not block the caller.  All exceptions are caught
    and logged; the primary dispatch outcome is never affected.

    Parameters
    ----------
    invocation:
        The original ``DispatchInvocation`` handed to Claude.  The prompt
        is reused verbatim; a sibling worktree is provisioned for codex.
    primary_result:
        The already-completed ``DispatchResult`` from the Claude
        invocation.
    conn:
        Open SQLite connection for ledger persistence.
    codex_adapter:
        A ``CodexCliAdapter`` instance (caller is responsible for
        instantiation and any version-pin smoke test).
    worktree_root:
        Directory under which sibling worktrees are created.  The shadow
        worktree will be ``<worktree_root>/shadow-<session_id[:8]>/``.
    """
    session_id = invocation.session_id
    shadow_error = ""
    shadow_result: DispatchResult | None = None

    try:
        # Provision a temporary directory for the shadow codex run.
        # We use a flat sibling directory (not a git worktree) since
        # codex just needs a working directory to operate in; it can
        # read the prompt and write notes there.
        shadow_dir = worktree_root / f"{SHADOW_BRANCH_PREFIX}{session_id[:8]}"
        shadow_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "shadow: dispatching codex shadow for session %s in %s",
            session_id,
            shadow_dir,
        )

        from .codex import CodexSpawnResult

        codex_spawn: CodexSpawnResult = await codex_adapter.spawn(
            invocation.prompt,
            shadow_dir,
            wall_clock_timeout_s=SHADOW_WALL_CLOCK_TIMEOUT_S,
        )

        # Map CodexSpawnResult → DispatchResult so compare_results can
        # work uniformly across both backends.
        shadow_result = _codex_spawn_to_dispatch_result(
            codex_spawn, session_id=session_id
        )

        logger.info(
            "shadow: codex finished session=%s exit_code=%s wall_s=%.1f",
            session_id,
            codex_spawn.exit_code,
            codex_spawn.wall_clock_seconds,
        )

    except Exception as exc:  # noqa: BLE001
        shadow_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "shadow: codex shadow failed for session %s — %s",
            session_id,
            shadow_error,
        )
        # Build a synthetic FAILED result so persist_shadow_event always
        # has a valid object to work with.
        shadow_result = DispatchResult(
            classification=Classification.FAILED,
            exit_code=None,
            session_id=session_id,
        )

    comparison = compare_results(primary_result, shadow_result)
    logger.info(
        "shadow: session=%s shape_match=%s cost_delta_usd=%+.4f",
        session_id,
        comparison.shape_match,
        comparison.cost_delta_usd,
    )

    try:
        persist_shadow_event(
            conn,
            session_id=session_id,
            primary=primary_result,
            shadow=shadow_result,
            comparison=comparison,
            shadow_error=shadow_error,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "shadow: failed to persist shadow event for session %s — %s",
            session_id,
            exc,
        )


def maybe_schedule_shadow(
    *,
    invocation: DispatchInvocation,
    primary_result: DispatchResult,
    conn: sqlite3.Connection,
    worktree_root: Path,
) -> "asyncio.Task[None] | None":
    """Schedule a shadow run if ``OXI_AGENTIC_SHADOW=codex`` is set.

    This is the single call-site integration point.  Callers (e.g.
    ``dispatch.dispatch_one``) call this after ``invoke()`` returns.
    The shadow is launched as a background ``asyncio.Task``; the primary
    result is never blocked on it.

    Returns the created ``asyncio.Task`` (or None if shadowing is
    disabled), so callers that care can ``await`` it in tests.

    Parameters
    ----------
    invocation:
        Original Claude ``DispatchInvocation``.
    primary_result:
        Completed ``DispatchResult`` from the primary Claude invocation.
    conn:
        Open SQLite connection.
    worktree_root:
        Root directory for shadow worktree provisioning.
    """
    if not should_shadow():
        return None

    try:
        from .codex import CodexCliAdapter

        codex_adapter = CodexCliAdapter()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "shadow: failed to instantiate CodexCliAdapter — %s; shadow disabled",
            exc,
        )
        return None

    coro = run_shadow(
        invocation=invocation,
        primary_result=primary_result,
        conn=conn,
        codex_adapter=codex_adapter,
        worktree_root=worktree_root,
    )
    task: asyncio.Task[None] = asyncio.create_task(coro)
    return task


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _codex_spawn_to_dispatch_result(
    spawn: "CodexSpawnResult",
    *,
    session_id: str,
) -> DispatchResult:
    """Convert a ``CodexSpawnResult`` to a ``DispatchResult``.

    Classification heuristic:
    - exit_code 0    → SUCCESS
    - exit_code None → TIMEOUT (killed without an exit code)
    - any other      → FAILED

    Cost is extracted from the first ``result`` event (if any) in the
    codex event stream, mirroring how ``dispatch_invoke.invoke()``
    extracts cost from the claude event stream.
    """

    if spawn.exit_code == 0:
        classification = Classification.SUCCESS
    elif spawn.exit_code is None:
        classification = Classification.TIMEOUT
    else:
        classification = Classification.FAILED

    # Extract cost from the codex event stream.  Codex emits a
    # ``turn.completed`` event with ``total_cost_usd``; the codex_events
    # normalizer maps it to ``type=result``.  We do a minimal inline
    # extraction here to avoid pulling in the full normalizer.
    cost_usd = 0.0
    for evt in spawn.events:
        if evt.get("type") == "turn.completed":
            cost_usd = float(evt.get("total_cost_usd", 0.0))
            break
        # Also accept the already-normalised canonical form if someone
        # has run normalize_events() upstream.
        if evt.get("type") == "result":
            cost_usd = float(evt.get("total_cost_usd", 0.0))
            break

    return DispatchResult(
        classification=classification,
        exit_code=spawn.exit_code,
        session_id=session_id,
        events=spawn.events,
        trailing_line=spawn.trailing_line,
        stderr_text=spawn.stderr_text,
        cost_usd=cost_usd,
        wall_clock_seconds=spawn.wall_clock_seconds,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "ENV_VAR",
    "SHADOW_BACKEND_CODEX",
    "SHADOW_WALL_CLOCK_TIMEOUT_S",
    "ShadowComparison",
    "compare_results",
    "maybe_schedule_shadow",
    "persist_shadow_event",
    "run_shadow",
    "should_shadow",
]
