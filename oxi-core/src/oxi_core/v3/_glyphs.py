"""Status glyph vocabulary used across CLI, dashboard, and brief output.

Five glyphs, one meaning each. Used everywhere a task or engine state
needs a visual marker — so an operator scanning a screenshot of any
surface sees the same shape for the same state.

Glyph        Meaning
─────        ───────
``○``        queued / planned — task waiting to dispatch
``●``        running / dispatching — task in flight, PR not yet observed
``✓``        merged / success / healthy — done, or budget OK
``✗``        failed / abandoned / hard-stop — terminal failure state
``⏸``        killswitched / paused — engine halted by operator

These are pure BMP Unicode — no escape codes, no ANSI baked in. The
caller decides how to color them (Rich applies styling at print time;
the dashboard's CSS colors them via the surrounding span).

Status names follow the engine's canonical task-status vocabulary:

    planned, dispatched, merged, failed, abandoned

plus the engine-level state ``killswitched`` (synthetic — only used
by code that already knows the killswitch is active) and ``running``
which is ``dispatched AND pr_number IS NULL`` per the dashboard's
hero-row computation.
"""

from __future__ import annotations

# Public glyph constants. Keep these as module-level strings so they
# can be `from . import _glyphs` and used like `_glyphs.OK`.
QUEUED = "○"
RUNNING = "●"
OK = "✓"
FAIL = "✗"
PAUSED = "⏸"


# Canonical status → glyph mapping.  Anything we don't recognise falls
# through to ``""`` (empty string) so call sites don't accidentally
# render a literal "?" or break their formatting.  Update this table
# rather than adding ad-hoc glyphs scattered across the codebase.
_STATUS_TO_GLYPH: dict[str, str] = {
    # Canonical task statuses (oxi-core/v3 schema).
    "planned": QUEUED,
    "dispatched": RUNNING,
    "merged": OK,
    "failed": FAIL,
    "abandoned": FAIL,
    # Synthetic engine-level states.
    "killswitched": PAUSED,
    "paused": PAUSED,
    "running": RUNNING,
    "queued": QUEUED,
    "ok": OK,
    "healthy": OK,
}


def glyph_for_status(status: str) -> str:
    """Return the glyph for *status*, or ``""`` if unknown.

    Lookup is case-insensitive.  Empty string for unknown lets callers
    do ``f"{glyph_for_status(s)} {s}"`` without producing a stray
    leading space when the status doesn't have a known glyph yet —
    they get ``" planned"`` for known and ``"foo"`` for unknown.
    """
    if not status:
        return ""
    return _STATUS_TO_GLYPH.get(status.lower(), "")


__all__ = [
    "FAIL",
    "OK",
    "PAUSED",
    "QUEUED",
    "RUNNING",
    "glyph_for_status",
]
