"""Tests for oxi_core.v3.tail_dispatch."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from oxi_core.v3.tail_dispatch import (
    ParsedEvent,
    format_event,
    iter_events,
    parse_line,
    tail,
)

# ---------------------------------------------------------------------------
# parse_line
# ---------------------------------------------------------------------------


def test_parse_assistant_event_with_text():
    raw = {
        "type": "assistant",
        "session_id": "abc12345",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }
    evt = parse_line(json.dumps(raw))
    assert evt is not None
    assert evt.kind == "assistant"
    assert evt.session_id == "abc12345"
    assert "hello" in evt.text


def test_parse_system_init():
    raw = {"type": "system", "subtype": "init", "model": "sonnet-4-6"}
    evt = parse_line(json.dumps(raw))
    assert evt.kind == "system"
    assert evt.subtype == "init"
    assert "sonnet-4-6" in evt.text


def test_parse_api_retry_includes_details():
    raw = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 2,
        "error": "rate_limit",
        "retry_delay_ms": 1000,
    }
    evt = parse_line(json.dumps(raw))
    assert "api_retry" in evt.text
    assert "attempt=2" in evt.text
    assert "rate_limit" in evt.text


def test_parse_result_includes_cost():
    raw = {
        "type": "result",
        "result": "done",
        "total_cost_usd": 0.1234,
    }
    evt = parse_line(json.dumps(raw))
    assert "done" in evt.text
    assert "$0.1234" in evt.text


def test_parse_tool_use_summarized():
    raw = {
        "type": "assistant",
        "session_id": "s",
        "message": {
            "content": [
                {"type": "text", "text": "I'll read the file."},
                {"type": "tool_use", "name": "Read"},
            ],
        },
    }
    evt = parse_line(json.dumps(raw))
    assert "Read" in evt.text


def test_parse_empty_line_returns_none():
    assert parse_line("") is None
    assert parse_line("   \n") is None


def test_parse_malformed_json_returns_none():
    assert parse_line('{"type": "assistant"') is None
    assert parse_line("not json at all") is None


def test_parse_non_dict_returns_none():
    """JSON that's a list/string/number isn't an event."""
    assert parse_line("[1, 2, 3]") is None
    assert parse_line('"string"') is None
    assert parse_line("42") is None


# ---------------------------------------------------------------------------
# format_event
# ---------------------------------------------------------------------------


def test_format_includes_session_prefix():
    evt = ParsedEvent(
        kind="assistant", subtype="", session_id="deadbeef-1234",
        text="hello world", raw={},
    )
    line = format_event(evt)
    assert line.startswith("[deadbeef]")
    assert "assistant" in line
    assert "hello world" in line


def test_format_subtype_joined_with_slash():
    evt = ParsedEvent(
        kind="system", subtype="init", session_id="s",
        text="model=x", raw={},
    )
    assert "system/init" in format_event(evt)


def test_format_handles_empty_session():
    evt = ParsedEvent(kind="result", subtype="", session_id="", text="t", raw={})
    assert "[--------]" in format_event(evt)


# ---------------------------------------------------------------------------
# iter_events
# ---------------------------------------------------------------------------


def _write_ndjson(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_iter_events_parses_all_lines(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    _write_ndjson(log, [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {
            "type": "assistant", "session_id": "s",
            "message": {"content": [{"type": "text", "text": "hi"}]},
        },
        {"type": "result", "session_id": "s", "result": "ok"},
    ])
    events = list(iter_events(log))
    assert [e.kind for e in events] == ["system", "assistant", "result"]


def test_iter_events_skips_malformed(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    with log.open("w") as fh:
        fh.write('{"type": "system", "subtype": "init"}\n')
        fh.write("garbage line\n")
        fh.write('{"type": "result", "result": "done"}\n')
        # Truncated final line.
        fh.write('{"type": "assistant"')
    events = list(iter_events(log))
    assert [e.kind for e in events] == ["system", "result"]


# ---------------------------------------------------------------------------
# tail — non-follow mode
# ---------------------------------------------------------------------------


def test_tail_non_follow_yields_all_lines(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    _write_ndjson(log, [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "result", "session_id": "s", "result": "done"},
    ])
    lines = list(tail(log, follow=False))
    assert len(lines) == 2
    assert "system/init" in lines[0]


def test_tail_missing_file_raises(tmp_path: Path):
    log = tmp_path / "does-not-exist.jsonl"
    with pytest.raises(FileNotFoundError):
        list(tail(log, follow=False))


# ---------------------------------------------------------------------------
# tail — follow mode
# ---------------------------------------------------------------------------


def test_tail_follow_times_out_after_silence(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    _write_ndjson(log, [
        {"type": "system", "subtype": "init", "session_id": "s"},
    ])

    start = time.monotonic()
    lines = list(tail(
        log, follow=True, poll_interval_s=0.05, event_timeout_s=0.3,
    ))
    elapsed = time.monotonic() - start
    assert "system/init" in lines[0]
    # Should exit within ~0.3s + tolerance.
    assert elapsed < 1.5


def test_tail_follow_picks_up_appends(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    _write_ndjson(log, [
        {"type": "system", "subtype": "init", "session_id": "s"},
    ])

    collected: list[str] = []
    stop = threading.Event()

    def _producer() -> None:
        time.sleep(0.15)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "assistant", "session_id": "s",
                "message": {"content": [{"type": "text", "text": "late"}]},
            }) + "\n")
        time.sleep(0.15)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "result", "session_id": "s", "result": "done",
            }) + "\n")

    t = threading.Thread(target=_producer, daemon=True)
    t.start()

    for line in tail(
        log, follow=True, poll_interval_s=0.05, event_timeout_s=0.5,
    ):
        collected.append(line)
        if "result" in line:
            stop.set()
            break

    t.join(timeout=2.0)
    kinds_seen = [
        line for line in collected
        if "system" in line or "assistant" in line or "result" in line
    ]
    # We got all three — initial init, live-appended assistant, result.
    assert len(kinds_seen) >= 3


# ---------------------------------------------------------------------------
# ParsedEvent dataclass
# ---------------------------------------------------------------------------


def test_parsed_event_is_frozen():
    from dataclasses import FrozenInstanceError
    evt = ParsedEvent(kind="x", subtype="", session_id="s", text="t", raw={})
    with pytest.raises(FrozenInstanceError):
        evt.kind = "y"  # type: ignore[misc]
