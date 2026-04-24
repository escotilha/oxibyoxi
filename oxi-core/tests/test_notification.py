"""Tests for oxi_core.v3.notification — backend contracts."""

from __future__ import annotations

import logging

import pytest

from oxi_core.v3.notification import (
    Level,
    LoggingBackend,
    Notification,
    NotificationBackend,
    RecordingBackend,
    StderrBackend,
)


def _notification(level: Level = Level.INFO) -> Notification:
    return Notification(
        level=level, title="t", body="b", tags=("budget",),
    )


# ---------------------------------------------------------------------------
# RecordingBackend
# ---------------------------------------------------------------------------


def test_recording_backend_records_in_order():
    b = RecordingBackend()
    b.send(_notification(Level.INFO))
    b.send(_notification(Level.WARN))
    assert b.count == 2
    assert [n.level for n in b.records] == [Level.INFO, Level.WARN]


def test_recording_backend_last_returns_none_when_empty():
    b = RecordingBackend()
    assert b.last() is None


def test_recording_backend_last_returns_most_recent():
    b = RecordingBackend()
    b.send(_notification(Level.INFO))
    b.send(_notification(Level.ALERT))
    assert b.last().level is Level.ALERT


def test_recording_backend_clear_empties_list():
    b = RecordingBackend()
    b.send(_notification())
    b.clear()
    assert b.count == 0


# ---------------------------------------------------------------------------
# LoggingBackend
# ---------------------------------------------------------------------------


def test_logging_backend_writes_through_logger(caplog):
    b = LoggingBackend(logger_name="oxi.test-notif")
    with caplog.at_level(logging.WARNING, logger="oxi.test-notif"):
        b.send(Notification(level=Level.WARN, title="x", body="y"))
    assert any("x" in rec.getMessage() for rec in caplog.records)


def test_logging_backend_maps_levels(caplog):
    b = LoggingBackend(logger_name="oxi.test-notif")
    cases = [
        (Level.INFO, logging.INFO),
        (Level.WARN, logging.WARNING),
        (Level.ALERT, logging.ERROR),
        (Level.DEAD, logging.CRITICAL),
    ]
    for level, expected in cases:
        caplog.clear()
        with caplog.at_level(expected, logger="oxi.test-notif"):
            b.send(Notification(level=level, title="t", body="b"))
        assert any(rec.levelno == expected for rec in caplog.records)


# ---------------------------------------------------------------------------
# StderrBackend
# ---------------------------------------------------------------------------


def test_stderr_backend_writes_to_stderr(capsys):
    b = StderrBackend()
    b.send(Notification(
        level=Level.ALERT,
        title="service-down",
        body="details here\nmore details",
        tags=("prod", "budget"),
    ))
    captured = capsys.readouterr()
    # Only stderr should be touched.
    assert captured.out == ""
    assert "ALERT" in captured.err
    assert "service-down" in captured.err
    assert "[prod]" in captured.err
    assert "[budget]" in captured.err
    assert "details here" in captured.err
    assert "more details" in captured.err


def test_stderr_backend_handles_empty_body(capsys):
    b = StderrBackend()
    b.send(Notification(level=Level.INFO, title="ping", body=""))
    captured = capsys.readouterr()
    assert "ping" in captured.err


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_all_backends_satisfy_protocol():
    for b in (LoggingBackend(), StderrBackend(), RecordingBackend()):
        assert isinstance(b, NotificationBackend)


def test_custom_backend_satisfies_protocol():
    class _CustomBackend:
        def __init__(self):
            self.got = []
        def send(self, n: Notification) -> None:
            self.got.append(n)
    assert isinstance(_CustomBackend(), NotificationBackend)


# ---------------------------------------------------------------------------
# Notification dataclass
# ---------------------------------------------------------------------------


def test_notification_is_frozen():
    from dataclasses import FrozenInstanceError
    n = _notification()
    with pytest.raises(FrozenInstanceError):
        n.title = "other"  # type: ignore[misc]


def test_notification_tags_default_to_empty():
    n = Notification(level=Level.INFO, title="t", body="b")
    assert n.tags == ()
