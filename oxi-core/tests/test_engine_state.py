"""Tests for oxi_core.v3.engine_state."""

from __future__ import annotations

import pytest

from oxi_core.v3.engine_state import EngineState


def test_engine_state_construction():
    s = EngineState(plan_tier="standard", max_concurrent=4)
    assert s.plan_tier == "standard"
    assert s.max_concurrent == 4
    assert s.is_stopping() is False


def test_rejects_empty_plan_tier():
    with pytest.raises(ValueError, match="plan_tier must be non-empty"):
        EngineState(plan_tier="", max_concurrent=4)


def test_rejects_invalid_max_concurrent():
    with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
        EngineState(plan_tier="standard", max_concurrent=0)
    with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
        EngineState(plan_tier="standard", max_concurrent=-5)


def test_request_stop_flips_is_stopping():
    s = EngineState(plan_tier="standard", max_concurrent=4)
    assert s.is_stopping() is False
    s.request_stop()
    assert s.is_stopping() is True


def test_request_stop_is_idempotent():
    s = EngineState(plan_tier="standard", max_concurrent=4)
    s.request_stop()
    s.request_stop()
    s.request_stop()
    assert s.is_stopping() is True


def test_clear_stop_resets_flag():
    s = EngineState(plan_tier="standard", max_concurrent=4)
    s.request_stop()
    s.clear_stop()
    assert s.is_stopping() is False


def test_stop_reason_is_informational_only():
    """The reason string is not stored — it's purely for the caller's logs."""
    s = EngineState(plan_tier="standard", max_concurrent=4)
    s.request_stop("test stop")
    s.request_stop("different reason")
    # Both succeed; no reason is retrievable from the object.
    assert s.is_stopping() is True


def test_two_state_objects_are_independent():
    """EngineState is not a singleton — tests can have their own instances."""
    a = EngineState(plan_tier="standard", max_concurrent=4)
    b = EngineState(plan_tier="standard", max_concurrent=4)
    a.request_stop()
    assert a.is_stopping() is True
    assert b.is_stopping() is False
