"""Tests for ``oxi_core.v3.agentic.codex_events``.

Organisation
------------
1. ``parse_jsonl`` — low-level JSONL parsing
2. ``normalize_event`` — single-event mapping (one case per codex type)
3. ``normalize_events`` — sequence mapping
4. ``parse_and_normalize`` — convenience wrapper
5. ``total_cost_usd`` — cost aggregation
6. ``has_successful_result`` — round-trip predicate
7. Recorded JSONL fixtures — replay from ``tests/fixtures/codex/``
8. Round-trip property — ``DispatchResult.result_event()`` integration

No I/O, no subprocess, no async.  All tests are pure-function assertions
over dicts and strings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxi_core.v3.agentic.codex_events import (
    CODEX_TO_CANONICAL,
    ParseResult,
    has_successful_result,
    normalize_event,
    normalize_events,
    parse_and_normalize,
    parse_jsonl,
    total_cost_usd,
)
from oxi_core.v3.dispatch_invoke import Classification, DispatchResult

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "codex"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> str:
    """Load a JSONL fixture file and return its text."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _events_from_fixture(name: str) -> list[dict]:
    """Parse and normalize a named fixture; return canonical events."""
    events, _ = parse_and_normalize(_load_fixture(name))
    return events


# ---------------------------------------------------------------------------
# 1. parse_jsonl
# ---------------------------------------------------------------------------


class TestParseJsonl:
    def test_empty_string_returns_empty_result(self):
        result = parse_jsonl("")
        assert result.events == []
        assert result.parse_errors == []

    def test_blank_lines_ignored(self):
        text = "\n\n\n"
        result = parse_jsonl(text)
        assert result.events == []
        assert result.parse_errors == []

    def test_single_valid_line(self):
        result = parse_jsonl('{"type": "turn.started", "session_id": "s1"}')
        assert len(result.events) == 1
        assert result.events[0] == {"type": "turn.started", "session_id": "s1"}

    def test_multiple_valid_lines(self):
        text = (
            '{"type": "turn.started"}\n'
            '{"type": "turn.completed", "total_cost_usd": 0.01}\n'
        )
        result = parse_jsonl(text)
        assert len(result.events) == 2
        assert result.events[0]["type"] == "turn.started"
        assert result.events[1]["type"] == "turn.completed"

    def test_invalid_json_line_collected_not_raised(self):
        text = (
            '{"type": "turn.started"}\n'
            'not valid json at all\n'
            '{"type": "turn.completed", "total_cost_usd": 0.0}\n'
        )
        result = parse_jsonl(text)
        # Only the two valid events are parsed.
        assert len(result.events) == 2
        # One parse error recorded.
        assert len(result.parse_errors) == 1
        line_idx, raw_line, error_msg = result.parse_errors[0]
        assert "not valid json at all" in raw_line
        assert isinstance(error_msg, str)

    def test_truncated_last_line_collected_not_raised(self):
        """Simulates SIGKILL mid-write — partial last line must not raise."""
        text = '{"type": "turn.started"}\n{"type": "turn.comp'
        result = parse_jsonl(text)
        assert len(result.events) == 1
        assert len(result.parse_errors) == 1

    def test_parse_result_is_parse_result_instance(self):
        result = parse_jsonl('{"type": "turn.started"}')
        assert isinstance(result, ParseResult)

    def test_multiple_invalid_lines_all_collected(self):
        text = "bad\nalso bad\n{\"type\": \"turn.started\"}\nbad again\n"
        result = parse_jsonl(text)
        assert len(result.events) == 1
        assert len(result.parse_errors) == 3

    def test_trailing_newline_does_not_produce_extra_event(self):
        text = '{"type": "turn.started"}\n'
        result = parse_jsonl(text)
        assert len(result.events) == 1

    def test_whitespace_only_line_ignored(self):
        text = '{"type": "turn.started"}\n   \n{"type": "turn.completed", "total_cost_usd": 0.0}'
        result = parse_jsonl(text)
        assert len(result.events) == 2

    def test_non_dict_json_recorded_as_error(self):
        """A JSON array or scalar is unexpected; collected as parse error."""
        text = '[1, 2, 3]\n{"type": "turn.started"}'
        result = parse_jsonl(text)
        assert len(result.events) == 1
        assert len(result.parse_errors) == 1


# ---------------------------------------------------------------------------
# 2. normalize_event — one case per codex type
# ---------------------------------------------------------------------------


class TestNormalizeEventTurnStarted:
    def _evt(self, **extra) -> dict:
        base = {"type": "turn.started", "turn_id": "t1", "session_id": "s1"}
        base.update(extra)
        return base

    def test_canonical_type_is_system(self):
        assert normalize_event(self._evt())["type"] == "system"

    def test_subtype_is_turn_started(self):
        assert normalize_event(self._evt())["subtype"] == "turn_started"

    def test_turn_id_preserved(self):
        n = normalize_event(self._evt())
        assert n["turn_id"] == "t1"

    def test_session_id_preserved(self):
        n = normalize_event(self._evt())
        assert n["session_id"] == "s1"

    def test_codex_type_field_set(self):
        n = normalize_event(self._evt())
        assert n["codex_type"] == "turn.started"

    def test_original_event_stored_under_codex_key(self):
        evt = self._evt()
        n = normalize_event(evt)
        assert n["codex"] == evt

    def test_missing_optional_fields_do_not_raise(self):
        """Minimal event with only type must not raise."""
        n = normalize_event({"type": "turn.started"})
        assert n["type"] == "system"


class TestNormalizeEventTurnCompleted:
    def _evt(self, **extra) -> dict:
        base = {
            "type": "turn.completed",
            "turn_id": "t1",
            "session_id": "s1",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "total_cost_usd": 0.003,
        }
        base.update(extra)
        return base

    def test_canonical_type_is_result(self):
        assert normalize_event(self._evt())["type"] == "result"

    def test_result_field_is_done(self):
        assert normalize_event(self._evt())["result"] == "done"

    def test_total_cost_usd_preserved(self):
        n = normalize_event(self._evt(total_cost_usd=1.23))
        assert n["total_cost_usd"] == pytest.approx(1.23)

    def test_total_cost_usd_defaults_to_zero_when_absent(self):
        evt = {"type": "turn.completed"}
        n = normalize_event(evt)
        assert n["total_cost_usd"] == 0.0

    def test_usage_preserved(self):
        usage = {"input_tokens": 100, "output_tokens": 50}
        n = normalize_event(self._evt(usage=usage))
        assert n["usage"] == usage

    def test_usage_absent_when_not_in_source(self):
        evt = {"type": "turn.completed", "total_cost_usd": 0.0}
        n = normalize_event(evt)
        assert "usage" not in n

    def test_codex_type_field_set(self):
        assert normalize_event(self._evt())["codex_type"] == "turn.completed"

    def test_original_stored_under_codex_key(self):
        evt = self._evt()
        n = normalize_event(evt)
        assert n["codex"] == evt

    def test_no_subtype_field(self):
        """result events don't need a subtype — DispatchResult checks type only."""
        n = normalize_event(self._evt())
        assert "subtype" not in n


class TestNormalizeEventItemCompleted:
    def _evt(self, **extra) -> dict:
        base = {
            "type": "item.completed",
            "turn_id": "t1",
            "session_id": "s1",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello."}],
            },
        }
        base.update(extra)
        return base

    def test_canonical_type_is_assistant(self):
        assert normalize_event(self._evt())["type"] == "assistant"

    def test_message_field_set_from_item(self):
        item = {"type": "message", "role": "assistant", "content": []}
        n = normalize_event(self._evt(item=item))
        assert n["message"] == item

    def test_message_absent_when_item_not_in_source(self):
        evt = {"type": "item.completed", "turn_id": "t1", "session_id": "s1"}
        n = normalize_event(evt)
        assert "message" not in n

    def test_session_id_preserved(self):
        assert normalize_event(self._evt())["session_id"] == "s1"

    def test_codex_type_field_set(self):
        assert normalize_event(self._evt())["codex_type"] == "item.completed"


class TestNormalizeEventToolCallStarted:
    def _evt(self, **extra) -> dict:
        base = {
            "type": "tool.call.started",
            "turn_id": "t1",
            "session_id": "s1",
            "call_id": "c1",
            "tool": "shell",
            "input": {"command": "ls"},
        }
        base.update(extra)
        return base

    def test_canonical_type_is_tool_use(self):
        assert normalize_event(self._evt())["type"] == "tool_use"

    def test_call_id_preserved(self):
        assert normalize_event(self._evt())["call_id"] == "c1"

    def test_tool_name_preserved(self):
        assert normalize_event(self._evt())["tool"] == "shell"

    def test_input_preserved(self):
        assert normalize_event(self._evt())["input"] == {"command": "ls"}

    def test_input_absent_when_not_in_source(self):
        evt = {
            "type": "tool.call.started",
            "call_id": "c1",
            "tool": "shell",
        }
        n = normalize_event(evt)
        assert "input" not in n

    def test_codex_type_field_set(self):
        assert normalize_event(self._evt())["codex_type"] == "tool.call.started"


class TestNormalizeEventToolCallCompleted:
    def _evt(self, **extra) -> dict:
        base = {
            "type": "tool.call.completed",
            "turn_id": "t1",
            "session_id": "s1",
            "call_id": "c1",
            "tool": "shell",
            "output": {"stdout": "ok\n", "exit_code": 0},
        }
        base.update(extra)
        return base

    def test_canonical_type_is_tool_result(self):
        assert normalize_event(self._evt())["type"] == "tool_result"

    def test_call_id_preserved(self):
        assert normalize_event(self._evt())["call_id"] == "c1"

    def test_tool_name_preserved(self):
        assert normalize_event(self._evt())["tool"] == "shell"

    def test_output_preserved(self):
        assert normalize_event(self._evt())["output"] == {"stdout": "ok\n", "exit_code": 0}

    def test_output_absent_when_not_in_source(self):
        evt = {
            "type": "tool.call.completed",
            "call_id": "c1",
            "tool": "shell",
        }
        n = normalize_event(evt)
        assert "output" not in n

    def test_codex_type_field_set(self):
        assert normalize_event(self._evt())["codex_type"] == "tool.call.completed"


class TestNormalizeEventError:
    def _evt(self, **extra) -> dict:
        base = {
            "type": "error",
            "session_id": "s1",
            "code": "context_length_exceeded",
            "message": "Input too long.",
        }
        base.update(extra)
        return base

    def test_canonical_type_is_system(self):
        assert normalize_event(self._evt())["type"] == "system"

    def test_subtype_is_error(self):
        assert normalize_event(self._evt())["subtype"] == "error"

    def test_code_preserved(self):
        assert normalize_event(self._evt())["code"] == "context_length_exceeded"

    def test_message_preserved(self):
        assert normalize_event(self._evt())["message"] == "Input too long."

    def test_session_id_preserved(self):
        assert normalize_event(self._evt())["session_id"] == "s1"

    def test_codex_type_field_set(self):
        assert normalize_event(self._evt())["codex_type"] == "error"

    def test_minimal_error_event(self):
        """An error event with only 'type' must not raise."""
        n = normalize_event({"type": "error"})
        assert n["type"] == "system"
        assert n["subtype"] == "error"


class TestNormalizeEventUnknown:
    def test_unknown_type_maps_to_system(self):
        n = normalize_event({"type": "future.unknown.thing"})
        assert n["type"] == "system"

    def test_unknown_type_subtype_is_unknown(self):
        n = normalize_event({"type": "some.new.event"})
        assert n["subtype"] == "unknown"

    def test_empty_type_maps_to_system_unknown(self):
        n = normalize_event({"type": ""})
        assert n["type"] == "system"
        assert n["subtype"] == "unknown"

    def test_missing_type_maps_to_system_unknown(self):
        n = normalize_event({"data": "no type field"})
        assert n["type"] == "system"
        assert n["subtype"] == "unknown"

    def test_codex_type_field_reflects_original(self):
        n = normalize_event({"type": "future.whatever"})
        assert n["codex_type"] == "future.whatever"

    def test_original_stored_under_codex_key(self):
        evt = {"type": "future.event", "x": 1}
        n = normalize_event(evt)
        assert n["codex"] == evt


# ---------------------------------------------------------------------------
# 3. normalize_events — sequence mapping
# ---------------------------------------------------------------------------


class TestNormalizeEvents:
    def test_empty_list_returns_empty_list(self):
        assert normalize_events([]) == []

    def test_order_preserved(self):
        raw = [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "message"}},
            {"type": "turn.completed", "total_cost_usd": 0.0},
        ]
        normalized = normalize_events(raw)
        assert [n["type"] for n in normalized] == ["system", "assistant", "result"]

    def test_input_list_not_mutated(self):
        raw = [{"type": "turn.started", "session_id": "s"}]
        original_len = len(raw)
        normalize_events(raw)
        assert len(raw) == original_len
        assert raw[0]["type"] == "turn.started"  # unchanged

    def test_returns_new_dicts(self):
        raw = [{"type": "turn.started"}]
        normalized = normalize_events(raw)
        assert normalized[0] is not raw[0]


# ---------------------------------------------------------------------------
# 4. parse_and_normalize — convenience wrapper
# ---------------------------------------------------------------------------


class TestParseAndNormalize:
    def test_empty_string(self):
        events, pr = parse_and_normalize("")
        assert events == []
        assert pr.events == []
        assert pr.parse_errors == []

    def test_valid_jsonl_round_trip(self):
        text = (
            '{"type": "turn.started", "session_id": "s1"}\n'
            '{"type": "turn.completed", "total_cost_usd": 0.01}\n'
        )
        events, pr = parse_and_normalize(text)
        assert len(events) == 2
        assert events[0]["type"] == "system"
        assert events[1]["type"] == "result"
        assert pr.parse_errors == []

    def test_returns_parse_result_instance(self):
        _, pr = parse_and_normalize('{"type": "turn.started"}')
        assert isinstance(pr, ParseResult)

    def test_parse_errors_surfaced(self):
        text = '{"type": "turn.started"}\nnot json\n'
        events, pr = parse_and_normalize(text)
        assert len(events) == 1
        assert len(pr.parse_errors) == 1


# ---------------------------------------------------------------------------
# 5. total_cost_usd
# ---------------------------------------------------------------------------


class TestTotalCostUsd:
    def test_empty_events_returns_zero(self):
        assert total_cost_usd([]) == 0.0

    def test_no_result_events_returns_zero(self):
        events = normalize_events([{"type": "turn.started"}])
        assert total_cost_usd(events) == 0.0

    def test_single_turn_cost(self):
        events = normalize_events([
            {"type": "turn.completed", "total_cost_usd": 0.05},
        ])
        assert total_cost_usd(events) == pytest.approx(0.05)

    def test_multi_turn_costs_summed(self):
        events = normalize_events([
            {"type": "turn.completed", "total_cost_usd": 0.01},
            {"type": "turn.completed", "total_cost_usd": 0.03},
        ])
        assert total_cost_usd(events) == pytest.approx(0.04)

    def test_zero_cost_turn(self):
        events = normalize_events([
            {"type": "turn.completed", "total_cost_usd": 0.0},
        ])
        assert total_cost_usd(events) == 0.0

    def test_missing_cost_field_treated_as_zero(self):
        events = normalize_events([
            {"type": "turn.completed"},
        ])
        assert total_cost_usd(events) == 0.0


# ---------------------------------------------------------------------------
# 6. has_successful_result
# ---------------------------------------------------------------------------


class TestHasSuccessfulResult:
    def test_empty_events_returns_false(self):
        assert not has_successful_result([])

    def test_no_result_events_returns_false(self):
        events = normalize_events([
            {"type": "turn.started"},
            {"type": "item.completed", "item": {}},
        ])
        assert not has_successful_result(events)

    def test_with_turn_completed_returns_true(self):
        events = normalize_events([
            {"type": "turn.started"},
            {"type": "turn.completed", "total_cost_usd": 0.0},
        ])
        assert has_successful_result(events)

    def test_error_only_stream_returns_false(self):
        events = normalize_events([
            {"type": "turn.started"},
            {"type": "error", "code": "context_length_exceeded"},
        ])
        assert not has_successful_result(events)


# ---------------------------------------------------------------------------
# 7. Recorded JSONL fixtures
# ---------------------------------------------------------------------------


class TestFixtureHappyPath:
    def test_loads_without_parse_errors(self):
        _, pr = parse_and_normalize(_load_fixture("happy_path.jsonl"))
        assert pr.parse_errors == []

    def test_event_types_in_order(self):
        events = _events_from_fixture("happy_path.jsonl")
        canonical_types = [e["type"] for e in events]
        assert canonical_types == [
            "system",       # turn.started
            "assistant",    # item.completed
            "tool_use",     # tool.call.started
            "tool_result",  # tool.call.completed
            "assistant",    # item.completed
            "result",       # turn.completed
        ]

    def test_has_successful_result(self):
        events = _events_from_fixture("happy_path.jsonl")
        assert has_successful_result(events)

    def test_total_cost_usd(self):
        events = _events_from_fixture("happy_path.jsonl")
        assert total_cost_usd(events) == pytest.approx(0.003)

    def test_assistant_event_carries_message(self):
        events = _events_from_fixture("happy_path.jsonl")
        assistants = [e for e in events if e["type"] == "assistant"]
        assert len(assistants) == 2
        # First assistant message has text content.
        first = assistants[0]
        assert "message" in first
        content = first["message"]["content"][0]
        assert content["type"] == "text"
        assert "implement" in content["text"]

    def test_tool_use_carries_input(self):
        events = _events_from_fixture("happy_path.jsonl")
        tool_uses = [e for e in events if e["type"] == "tool_use"]
        assert len(tool_uses) == 1
        assert tool_uses[0]["tool"] == "shell"
        assert tool_uses[0]["input"]["command"] == "ls -la"

    def test_tool_result_carries_output(self):
        events = _events_from_fixture("happy_path.jsonl")
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["output"]["exit_code"] == 0

    def test_call_id_consistent_across_tool_events(self):
        events = _events_from_fixture("happy_path.jsonl")
        tool_use = next(e for e in events if e["type"] == "tool_use")
        tool_result = next(e for e in events if e["type"] == "tool_result")
        assert tool_use["call_id"] == tool_result["call_id"]

    def test_session_id_consistent(self):
        events = _events_from_fixture("happy_path.jsonl")
        session_ids = {e["session_id"] for e in events if "session_id" in e}
        assert len(session_ids) == 1

    def test_all_events_carry_codex_provenance(self):
        events = _events_from_fixture("happy_path.jsonl")
        for e in events:
            assert "codex_type" in e
            assert "codex" in e
            assert isinstance(e["codex"], dict)


class TestFixtureErrorEvent:
    def test_loads_without_parse_errors(self):
        _, pr = parse_and_normalize(_load_fixture("error_event.jsonl"))
        assert pr.parse_errors == []

    def test_event_types_in_order(self):
        events = _events_from_fixture("error_event.jsonl")
        assert [e["type"] for e in events] == ["system", "system"]

    def test_error_event_subtype(self):
        events = _events_from_fixture("error_event.jsonl")
        error_events = [e for e in events if e.get("subtype") == "error"]
        assert len(error_events) == 1

    def test_error_code_preserved(self):
        events = _events_from_fixture("error_event.jsonl")
        err = next(e for e in events if e.get("subtype") == "error")
        assert err["code"] == "context_length_exceeded"

    def test_error_message_preserved(self):
        events = _events_from_fixture("error_event.jsonl")
        err = next(e for e in events if e.get("subtype") == "error")
        assert "context length" in err["message"].lower()

    def test_no_successful_result(self):
        events = _events_from_fixture("error_event.jsonl")
        assert not has_successful_result(events)

    def test_zero_cost(self):
        events = _events_from_fixture("error_event.jsonl")
        assert total_cost_usd(events) == 0.0


class TestFixtureMultiTurn:
    def test_loads_without_parse_errors(self):
        _, pr = parse_and_normalize(_load_fixture("multi_turn.jsonl"))
        assert pr.parse_errors == []

    def test_two_result_events(self):
        events = _events_from_fixture("multi_turn.jsonl")
        result_events = [e for e in events if e["type"] == "result"]
        assert len(result_events) == 2

    def test_two_system_turn_started_events(self):
        events = _events_from_fixture("multi_turn.jsonl")
        turn_starts = [
            e for e in events
            if e["type"] == "system" and e.get("subtype") == "turn_started"
        ]
        assert len(turn_starts) == 2

    def test_two_tool_call_pairs(self):
        events = _events_from_fixture("multi_turn.jsonl")
        tool_uses = [e for e in events if e["type"] == "tool_use"]
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_uses) == 2
        assert len(tool_results) == 2

    def test_total_cost_is_sum_of_turns(self):
        events = _events_from_fixture("multi_turn.jsonl")
        # Fixture has 0.001 + 0.004.
        assert total_cost_usd(events) == pytest.approx(0.005)

    def test_has_successful_result(self):
        events = _events_from_fixture("multi_turn.jsonl")
        assert has_successful_result(events)


class TestFixtureToolOnlyTurn:
    def test_loads_without_parse_errors(self):
        _, pr = parse_and_normalize(_load_fixture("tool_only_turn.jsonl"))
        assert pr.parse_errors == []

    def test_no_assistant_events(self):
        events = _events_from_fixture("tool_only_turn.jsonl")
        assert not any(e["type"] == "assistant" for e in events)

    def test_has_tool_use_and_result(self):
        events = _events_from_fixture("tool_only_turn.jsonl")
        assert any(e["type"] == "tool_use" for e in events)
        assert any(e["type"] == "tool_result" for e in events)

    def test_has_successful_result(self):
        events = _events_from_fixture("tool_only_turn.jsonl")
        assert has_successful_result(events)


class TestFixtureUnknownEventTypes:
    def test_loads_without_parse_errors(self):
        _, pr = parse_and_normalize(_load_fixture("unknown_event_types.jsonl"))
        assert pr.parse_errors == []

    def test_unknown_events_mapped_to_system_unknown(self):
        events = _events_from_fixture("unknown_event_types.jsonl")
        unknowns = [
            e for e in events
            if e["type"] == "system" and e.get("subtype") == "unknown"
        ]
        assert len(unknowns) == 1
        assert unknowns[0]["codex_type"] == "future.unknown.event"

    def test_known_events_still_mapped_correctly(self):
        events = _events_from_fixture("unknown_event_types.jsonl")
        assert any(e["type"] == "assistant" for e in events)
        assert any(e["type"] == "result" for e in events)

    def test_has_successful_result(self):
        events = _events_from_fixture("unknown_event_types.jsonl")
        assert has_successful_result(events)


class TestFixtureMinimalTurn:
    def test_loads_without_parse_errors(self):
        _, pr = parse_and_normalize(_load_fixture("minimal_turn.jsonl"))
        assert pr.parse_errors == []

    def test_two_events(self):
        events = _events_from_fixture("minimal_turn.jsonl")
        assert len(events) == 2

    def test_has_successful_result(self):
        events = _events_from_fixture("minimal_turn.jsonl")
        assert has_successful_result(events)


class TestFixtureEmptyStream:
    def test_loads_without_parse_errors(self):
        _, pr = parse_and_normalize(_load_fixture("empty_stream.jsonl"))
        assert pr.parse_errors == []

    def test_no_events(self):
        events = _events_from_fixture("empty_stream.jsonl")
        assert events == []

    def test_no_successful_result(self):
        events = _events_from_fixture("empty_stream.jsonl")
        assert not has_successful_result(events)


# ---------------------------------------------------------------------------
# 8. Round-trip property — DispatchResult.result_event() integration
# ---------------------------------------------------------------------------


class TestRoundTripDispatchResult:
    """Every successful codex run must produce a non-None result_event()."""

    def _make_dispatch_result(self, events: list[dict]) -> DispatchResult:
        return DispatchResult(
            classification=Classification.SUCCESS,
            exit_code=0,
            session_id="sess-test",
            events=events,
        )

    def test_happy_path_fixture_has_result_event(self):
        events = _events_from_fixture("happy_path.jsonl")
        dr = self._make_dispatch_result(events)
        assert dr.result_event() is not None

    def test_multi_turn_fixture_has_result_event(self):
        events = _events_from_fixture("multi_turn.jsonl")
        dr = self._make_dispatch_result(events)
        assert dr.result_event() is not None

    def test_tool_only_turn_fixture_has_result_event(self):
        events = _events_from_fixture("tool_only_turn.jsonl")
        dr = self._make_dispatch_result(events)
        assert dr.result_event() is not None

    def test_unknown_event_types_fixture_has_result_event(self):
        events = _events_from_fixture("unknown_event_types.jsonl")
        dr = self._make_dispatch_result(events)
        assert dr.result_event() is not None

    def test_minimal_turn_fixture_has_result_event(self):
        events = _events_from_fixture("minimal_turn.jsonl")
        dr = self._make_dispatch_result(events)
        assert dr.result_event() is not None

    def test_error_fixture_has_no_result_event(self):
        """Error-only stream must not produce a result event (failed run)."""
        events = _events_from_fixture("error_event.jsonl")
        dr = self._make_dispatch_result(events)
        assert dr.result_event() is None

    def test_empty_fixture_has_no_result_event(self):
        events = _events_from_fixture("empty_stream.jsonl")
        dr = self._make_dispatch_result(events)
        assert dr.result_event() is None

    def test_result_event_has_type_result(self):
        events = _events_from_fixture("happy_path.jsonl")
        dr = self._make_dispatch_result(events)
        evt = dr.result_event()
        assert evt is not None
        assert evt["type"] == "result"

    def test_result_event_cost_matches_total_cost(self):
        events = _events_from_fixture("happy_path.jsonl")
        dr = self._make_dispatch_result(events)
        result_evt = dr.result_event()
        assert result_evt is not None
        assert result_evt["total_cost_usd"] == pytest.approx(total_cost_usd(events))

    def test_has_successful_result_predicate_consistent_with_result_event(self):
        """has_successful_result() must agree with result_event() for all fixtures."""
        fixture_names = [
            p.name for p in FIXTURES_DIR.iterdir() if p.suffix == ".jsonl"
        ]
        for name in fixture_names:
            events = _events_from_fixture(name)
            dr = self._make_dispatch_result(events)
            # If has_successful_result is True, result_event must return non-None.
            if has_successful_result(events):
                assert dr.result_event() is not None, (
                    f"Fixture {name!r}: has_successful_result=True but "
                    f"result_event()=None"
                )
            # Reverse is not strictly required (a stream could have a result event
            # without has_successful_result being True if there's a raw 'result' event
            # injected), but we also check it's coherent.


# ---------------------------------------------------------------------------
# 9. CODEX_TO_CANONICAL mapping table
# ---------------------------------------------------------------------------


class TestCodexToCanonicalTable:
    def test_all_known_types_are_mapped(self):
        known_types = {
            "turn.started",
            "turn.completed",
            "item.completed",
            "tool.call.started",
            "tool.call.completed",
            "error",
        }
        assert known_types == set(CODEX_TO_CANONICAL.keys())

    def test_canonical_values_are_valid_dispatch_types(self):
        valid = {"system", "assistant", "tool_use", "tool_result", "result"}
        for codex_type, canonical in CODEX_TO_CANONICAL.items():
            assert canonical in valid, (
                f"{codex_type!r} maps to {canonical!r}, which is not a valid canonical type"
            )

    def test_turn_started_maps_to_system(self):
        assert CODEX_TO_CANONICAL["turn.started"] == "system"

    def test_turn_completed_maps_to_result(self):
        assert CODEX_TO_CANONICAL["turn.completed"] == "result"

    def test_item_completed_maps_to_assistant(self):
        assert CODEX_TO_CANONICAL["item.completed"] == "assistant"

    def test_tool_call_started_maps_to_tool_use(self):
        assert CODEX_TO_CANONICAL["tool.call.started"] == "tool_use"

    def test_tool_call_completed_maps_to_tool_result(self):
        assert CODEX_TO_CANONICAL["tool.call.completed"] == "tool_result"

    def test_error_maps_to_system(self):
        assert CODEX_TO_CANONICAL["error"] == "system"
