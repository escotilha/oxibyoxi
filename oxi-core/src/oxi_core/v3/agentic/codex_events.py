"""``codex_events`` — parse and normalise the ``codex --json`` event stream.

Overview
--------
``codex exec --json`` emits newline-delimited JSON (JSONL) to stdout.
This module contains **pure functions** that:

1. Parse a JSONL byte stream or sequence of raw strings into a list of
   raw event dicts (``parse_jsonl``).
2. Map codex-native event types to the canonical event types that
   ``dispatch_invoke.py``'s ``DispatchResult`` understands
   (``normalize_event`` / ``normalize_events``).
3. Build a synthetic ``DispatchResult``-compatible event list from a
   codex event sequence (``to_dispatch_events``).

No I/O, no subprocess, no async.  All functions operate on plain Python
dicts and strings.

Codex-native event types
------------------------
The codex CLI with ``--json`` emits the following event types (as of
codex 0.1.x):

==================== ===========================================================
Codex type           Meaning
==================== ===========================================================
``turn.started``     A new agent turn has begun (assistant is thinking/acting).
``turn.completed``   The turn finished; carries token usage + cost.
``item.completed``   The assistant produced a message item (text or refusal).
``tool.call.started``  A tool invocation is about to begin.
``tool.call.completed``  A tool invocation finished (with output).
``error``            A hard error terminated the session.
==================== ===========================================================

Canonical event types (``dispatch_invoke`` / ``DispatchResult``)
-----------------------------------------------------------------
The rest of oxi speaks this vocabulary:

==================== ===========================================================
Canonical type       Meaning
==================== ===========================================================
``system``           Lifecycle / meta events (init, retries, errors).
``assistant``        The model produced a response (message content).
``tool_use``         A tool call was initiated.
``tool_result``      A tool call returned a result.
``result``           Session-level summary (cost, outcome).
==================== ===========================================================

Mapping
-------
+--------------------------+--------------+---------------------+
| Codex type               | Canonical    | Notes               |
+==========================+==============+=====================+
| ``turn.started``         | ``system``   | subtype=turn_started|
+--------------------------+--------------+---------------------+
| ``turn.completed``       | ``result``   | carries cost fields |
+--------------------------+--------------+---------------------+
| ``item.completed``       | ``assistant``| wraps item payload  |
+--------------------------+--------------+---------------------+
| ``tool.call.started``    | ``tool_use`` | carries tool+input  |
+--------------------------+--------------+---------------------+
| ``tool.call.completed``  | ``tool_result``| carries output    |
+--------------------------+--------------+---------------------+
| ``error``                | ``system``   | subtype=error       |
+--------------------------+--------------+---------------------+
| *(anything else)*        | ``system``   | subtype=unknown     |
+--------------------------+--------------+---------------------+

Round-trip property
-------------------
For any successful codex run (one that emits at least one
``turn.completed`` event), replaying the normalized event sequence
through ``DispatchResult.result_event()`` returns a non-None dict::

    events = normalize_events(parse_jsonl(raw_jsonl))
    dr = DispatchResult(
        classification=Classification.SUCCESS,
        exit_code=0,
        session_id="...",
        events=events,
    )
    assert dr.result_event() is not None  # guaranteed

This invariant is enforced by the test suite.

Parse errors
------------
Lines that are not valid JSON are silently dropped and collected in the
``ParseResult.parse_errors`` list.  Blank lines are ignored.  This
tolerates the trailing-truncated-line scenario (SIGKILL mid-write)
without raising an exception.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

#: A raw codex event dict as emitted by ``codex --json``.
CodexEvent = dict[str, Any]

#: A normalized event dict in the canonical ``dispatch_invoke`` vocabulary.
CanonicalEvent = dict[str, Any]

# ---------------------------------------------------------------------------
# Codex → canonical type mapping
# ---------------------------------------------------------------------------

#: Maps each known codex event type to a canonical type string.
CODEX_TO_CANONICAL: dict[str, str] = {
    "turn.started": "system",
    "turn.completed": "result",
    "item.completed": "assistant",
    "tool.call.started": "tool_use",
    "tool.call.completed": "tool_result",
    "error": "system",
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """Outcome of ``parse_jsonl``.

    Attributes:
        events: Successfully parsed event dicts, in order.
        parse_errors: ``(line_index, raw_line, error_message)`` tuples for
            lines that were not valid JSON.  Empty when the stream is clean.
    """

    events: list[CodexEvent] = field(default_factory=list)
    parse_errors: list[tuple[int, str, str]] = field(default_factory=list)


def parse_jsonl(text: str) -> ParseResult:
    """Parse a JSONL string into a ``ParseResult``.

    Each non-blank line is decoded as JSON.  Lines that fail to decode
    are recorded in ``ParseResult.parse_errors`` and skipped; they do
    not raise.  This matches the truncated-line tolerance in
    ``dispatch_invoke.invoke()`` and ``codex._run_subprocess()``.

    Parameters
    ----------
    text:
        Raw JSONL string (may contain ``\\n``-separated lines, trailing
        newline optional, blank lines ignored).

    Returns
    -------
    ParseResult
        All successfully parsed events plus any per-line parse errors.
    """
    result = ParseResult()
    for idx, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            result.parse_errors.append((idx, raw, str(exc)))
            continue
        if isinstance(event, dict):
            result.events.append(event)
        else:
            # Non-dict JSON (arrays, scalars) — unexpected but tolerated.
            result.parse_errors.append(
                (idx, raw, f"expected a JSON object, got {type(event).__name__}")
            )
    return result


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_event(event: CodexEvent) -> CanonicalEvent:
    """Map one codex event dict to a canonical event dict.

    The original codex fields are preserved under a ``codex`` key so
    callers can inspect raw provenance.  The canonical ``type`` field is
    set to the mapped value; a ``subtype`` is added where needed to
    disambiguate within the canonical type.

    Parameters
    ----------
    event:
        A raw event dict parsed from ``codex --json`` output.

    Returns
    -------
    CanonicalEvent
        A new dict with at minimum ``type``, ``codex_type``, and
        ``codex`` (the original event).
    """
    codex_type: str = event.get("type", "")
    canonical_type = CODEX_TO_CANONICAL.get(codex_type, "system")

    # Build the normalized event, preserving all original fields and
    # adding the canonical overlay on top.
    normalized: CanonicalEvent = {
        # Canonical vocabulary
        "type": canonical_type,
        # Provenance — the original codex event type and the full raw event.
        "codex_type": codex_type,
        "codex": event,
    }

    # --- Per-type enrichment ---

    if codex_type == "turn.started":
        normalized["subtype"] = "turn_started"
        _copy_if_present(event, normalized, "turn_id", "session_id")

    elif codex_type == "turn.completed":
        # The canonical 'result' event must carry cost so DispatchResult
        # can extract it via result_event().
        _copy_if_present(event, normalized, "turn_id", "session_id")
        normalized["result"] = "done"
        normalized["total_cost_usd"] = float(event.get("total_cost_usd", 0.0))
        if "usage" in event:
            normalized["usage"] = event["usage"]

    elif codex_type == "item.completed":
        _copy_if_present(event, normalized, "turn_id", "session_id")
        # Surface the item payload so callers can extract message text.
        if "item" in event:
            normalized["message"] = event["item"]

    elif codex_type == "tool.call.started":
        _copy_if_present(event, normalized, "turn_id", "session_id", "call_id", "tool")
        if "input" in event:
            normalized["input"] = event["input"]

    elif codex_type == "tool.call.completed":
        _copy_if_present(event, normalized, "turn_id", "session_id", "call_id", "tool")
        if "output" in event:
            normalized["output"] = event["output"]

    elif codex_type == "error":
        normalized["subtype"] = "error"
        _copy_if_present(event, normalized, "turn_id", "session_id", "code", "message")

    else:
        # Future / unknown codex event types fall through as system/unknown.
        normalized["subtype"] = "unknown"
        _copy_if_present(event, normalized, "turn_id", "session_id")

    return normalized


def normalize_events(events: list[CodexEvent]) -> list[CanonicalEvent]:
    """Normalize a sequence of codex event dicts to canonical events.

    Applies ``normalize_event`` to each element in order.  The input
    list is not mutated.

    Parameters
    ----------
    events:
        Raw codex event dicts (typically from ``parse_jsonl``).

    Returns
    -------
    list[CanonicalEvent]
        Normalized events in the same order.
    """
    return [normalize_event(e) for e in events]


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def parse_and_normalize(text: str) -> tuple[list[CanonicalEvent], ParseResult]:
    """Parse JSONL text and normalize in one call.

    Convenience wrapper over ``parse_jsonl`` + ``normalize_events``.

    Parameters
    ----------
    text:
        Raw JSONL string from ``codex --json`` output.

    Returns
    -------
    tuple[list[CanonicalEvent], ParseResult]
        ``(normalized_events, parse_result)`` where ``parse_result``
        carries any per-line parse errors encountered.
    """
    pr = parse_jsonl(text)
    return normalize_events(pr.events), pr


def total_cost_usd(events: list[CanonicalEvent]) -> float:
    """Sum ``total_cost_usd`` across all canonical ``result`` events.

    Each ``turn.completed`` → ``result`` event may carry a cost.  For
    multi-turn codex sessions the per-turn costs are summed.

    Parameters
    ----------
    events:
        Canonical events (from ``normalize_events``).

    Returns
    -------
    float
        Total USD cost; 0.0 if no result events are present.
    """
    return sum(
        float(e.get("total_cost_usd", 0.0))
        for e in events
        if e.get("type") == "result"
    )


def has_successful_result(events: list[CanonicalEvent]) -> bool:
    """Return True if the event stream contains at least one canonical result event.

    A successful codex run always emits at least one ``turn.completed``
    which normalizes to ``type=result``.  This predicate is the
    round-trip guard: if it returns True,
    ``DispatchResult.result_event()`` is guaranteed to return non-None.

    Parameters
    ----------
    events:
        Canonical events (from ``normalize_events``).
    """
    return any(e.get("type") == "result" for e in events)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _copy_if_present(
    src: dict[str, Any],
    dst: dict[str, Any],
    *keys: str,
) -> None:
    """Copy ``keys`` from ``src`` to ``dst`` if they exist in ``src``."""
    for key in keys:
        if key in src:
            dst[key] = src[key]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "CODEX_TO_CANONICAL",
    "CanonicalEvent",
    "CodexEvent",
    "ParseResult",
    "has_successful_result",
    "normalize_event",
    "normalize_events",
    "parse_and_normalize",
    "parse_jsonl",
    "total_cost_usd",
]
