"""Live-tail stream-json dispatch logs.

During Phase 2 dogfooding, a dispatch that's "taking too long" is the
most common debugging task. The supervisor has a `.jsonl` log per
dispatch (stream-json output of the `claude -p` subprocess) but
`tail -f log.jsonl` dumps unreadable JSON blobs.

This module parses a stream-json log, formats each event for humans,
and exposes three entry points:

- ``iter_events(path)`` — iterate parsed events, skipping malformed
  lines gracefully (partial-line-at-EOF tolerance).
- ``format_event(event)`` — turn one parsed event into a single
  human-readable line with timestamp + type + one-line body.
- ``tail(path, follow, event_timeout_s)`` — CLI-facing generator
  yielding formatted lines; ``follow=True`` acts like ``tail -f``,
  blocking for new appends up to ``event_timeout_s`` between polls.

No adapter coupling. No engine state. Safe to run concurrently with
the dispatcher writing to the same file.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParsedEvent:
    """One stream-json event, parsed into a neutral shape.

    The raw dict is preserved on ``raw`` so callers that need uncommon
    fields can reach through.
    """

    kind: str            # e.g. "assistant", "tool_use", "result", "system"
    subtype: str          # for system events, e.g. "init" / "api_retry"
    session_id: str       # echoes claude's session UUID
    text: str             # best-effort one-line summary
    raw: dict


def _summary_from_raw(evt: dict) -> str:
    """Derive a one-line summary from a stream-json event.

    Strategy: prefer the first text block if there is one, else the
    tool name, else a short JSON of the payload truncated to 200
    chars. Never returns None — empty string if nothing useful.
    """
    etype = evt.get("type", "")

    # Assistant/user messages: concatenate text-type content blocks.
    if etype in ("assistant", "user"):
        msg = evt.get("message") or {}
        parts: list[str] = []
        for block in msg.get("content", []) or []:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    parts.append(f"<tool_use: {name}>")
                elif block.get("type") == "tool_result":
                    parts.append("<tool_result>")
        if parts:
            return " ".join(parts).replace("\n", " ").strip()[:300]

    # Result events: show the final text + cost.
    if etype == "result":
        text = str(evt.get("result", ""))
        cost = evt.get("total_cost_usd")
        cost_str = f" (${cost:.4f})" if cost is not None else ""
        return (text.replace("\n", " ").strip()[:200]) + cost_str

    # System events: subtype + key details.
    if etype == "system":
        sub = evt.get("subtype", "")
        if sub == "init":
            model = evt.get("model", "?")
            return f"init model={model}"
        if sub == "api_retry":
            return (
                f"api_retry attempt={evt.get('attempt')} "
                f"error={evt.get('error')} "
                f"delay_ms={evt.get('retry_delay_ms')}"
            )
        return sub or "system"

    # Fallback: short JSON.
    try:
        return json.dumps(evt, separators=(",", ":"))[:200]
    except (TypeError, ValueError):
        return str(evt)[:200]


def parse_line(line: str) -> ParsedEvent | None:
    """Parse one NDJSON line into a ``ParsedEvent``, or None if malformed.

    Empty lines return None. Partial lines (JSON without closing brace)
    return None — callers can safely swallow them (matches the
    truncated-JSON-at-EOF behavior of ``dispatch_invoke``).
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return ParsedEvent(
        kind=str(raw.get("type", "")),
        subtype=str(raw.get("subtype", "")),
        session_id=str(raw.get("session_id", "")),
        text=_summary_from_raw(raw),
        raw=raw,
    )


def iter_events(path: Path) -> Iterator[ParsedEvent]:
    """Iterate events in the log file.

    Malformed / partial lines are skipped silently. Does not follow the
    file — use ``tail`` with ``follow=True`` for that.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            evt = parse_line(line)
            if evt is not None:
                yield evt


def format_event(event: ParsedEvent) -> str:
    """Format one event as a single human-readable line."""
    label = event.kind
    if event.subtype:
        label = f"{label}/{event.subtype}"
    session_short = event.session_id[:8] if event.session_id else "--------"
    return f"[{session_short}] {label}: {event.text}".rstrip()


def tail(
    path: Path,
    *,
    follow: bool = False,
    poll_interval_s: float = 0.5,
    event_timeout_s: float | None = None,
) -> Iterator[str]:
    """Yield formatted lines from ``path``, optionally following appends.

    Arguments:
        path: path to the stream-json log. Must exist (raises otherwise).
        follow: if True, block between polls for new appends. Exits
            cleanly if the file is deleted or if ``event_timeout_s``
            elapses without a new event.
        poll_interval_s: how long to sleep between EOF checks in
            follow mode.
        event_timeout_s: maximum silence in follow mode before we
            stop (None = forever). Useful in tests.

    Yields formatted strings. Callers print them.
    """
    if not path.is_file():
        raise FileNotFoundError(f"dispatch log not found at {path}")

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        # Drain whatever's already there.
        for line in fh:
            evt = parse_line(line)
            if evt is not None:
                yield format_event(evt)

        if not follow:
            return

        last_event_at = time.monotonic()
        while True:
            line = fh.readline()
            if line:
                evt = parse_line(line)
                if evt is not None:
                    yield format_event(evt)
                    last_event_at = time.monotonic()
                # else: partial line — try again next poll.
                continue

            # EOF. Check timeout and file existence.
            if not path.is_file():
                return
            if (event_timeout_s is not None
                    and (time.monotonic() - last_event_at) > event_timeout_s):
                return
            time.sleep(poll_interval_s)


__all__ = [
    "ParsedEvent",
    "format_event",
    "iter_events",
    "parse_line",
    "tail",
]
