"""Tests for the structlog/stdlib logging bridge.

Covers:
  - configure_logging() is idempotent and re-entry-safe
  - JSON renderer is the default; produces parseable JSON to stderr
  - Console renderer is selected via OXI_LOG_FORMAT=console
  - Stdlib logging.getLogger(...).info(...) routes through structlog
  - get_logger() returns a structlog BoundLogger
  - OXI_LOG_LEVEL controls the root logger level
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from typing import Any

import pytest

from oxi_core.v3 import logging_setup


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Force a fresh configure on every test; restore env at teardown."""
    saved_env = {
        k: os.environ.get(k)
        for k in ("OXI_LOG_FORMAT", "OXI_LOG_LEVEL")
    }
    logging_setup.configure_logging._done = False  # type: ignore[attr-defined]
    yield
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # Also reset for the next test.
    logging_setup.configure_logging._done = False  # type: ignore[attr-defined]


def test_configure_is_idempotent_without_force():
    logging_setup.configure_logging()
    handlers_before = list(logging.getLogger().handlers)
    logging_setup.configure_logging()
    assert list(logging.getLogger().handlers) == handlers_before


def test_configure_force_reinstalls_handler():
    logging_setup.configure_logging()
    first = list(logging.getLogger().handlers)
    logging_setup.configure_logging(force=True)
    second = list(logging.getLogger().handlers)
    # New handler instance, same count.
    assert len(first) == len(second) == 1
    assert first[0] is not second[0]


def test_default_format_is_json(capfd: pytest.CaptureFixture[str]):
    os.environ.pop("OXI_LOG_FORMAT", None)
    logging_setup.configure_logging(force=True)
    logging.getLogger("oxi.test").info("hello", extra={"foo": "bar"})
    err = capfd.readouterr().err.strip()
    assert err, "expected log line on stderr"
    parsed: dict[str, Any] = json.loads(err)
    assert parsed["event"] == "hello"
    assert parsed["level"] == "info"
    assert parsed["logger"] == "oxi.test"


def test_explicit_json_format(capfd: pytest.CaptureFixture[str]):
    os.environ["OXI_LOG_FORMAT"] = "json"
    logging_setup.configure_logging(force=True)
    logging.getLogger("oxi.t2").warning("warn-event")
    parsed = json.loads(capfd.readouterr().err.strip())
    assert parsed["level"] == "warning"
    assert parsed["event"] == "warn-event"


def test_console_format_is_not_json(capfd: pytest.CaptureFixture[str]):
    os.environ["OXI_LOG_FORMAT"] = "console"
    logging_setup.configure_logging(force=True)
    logging.getLogger("oxi.console").info("console-event")
    err = capfd.readouterr().err.strip()
    assert err, "expected log line on stderr"
    # Console renderer is human-readable — not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(err)
    assert "console-event" in err


def test_invalid_format_falls_back_to_json(capfd: pytest.CaptureFixture[str]):
    os.environ["OXI_LOG_FORMAT"] = "yaml"  # unsupported
    logging_setup.configure_logging(force=True)
    logging.getLogger("oxi.fb").info("fallback-test")
    parsed = json.loads(capfd.readouterr().err.strip())
    assert parsed["event"] == "fallback-test"


def test_log_level_from_env(capfd: pytest.CaptureFixture[str]):
    os.environ["OXI_LOG_LEVEL"] = "WARNING"
    logging_setup.configure_logging(force=True)
    log = logging.getLogger("oxi.lvl")
    log.info("filtered")  # should not appear
    log.warning("kept")
    err = capfd.readouterr().err.strip().splitlines()
    assert len(err) == 1
    parsed = json.loads(err[0])
    assert parsed["event"] == "kept"


def test_invalid_level_falls_back_to_info(capfd: pytest.CaptureFixture[str]):
    os.environ["OXI_LOG_LEVEL"] = "BOGUS"
    logging_setup.configure_logging(force=True)
    assert logging.getLogger().level == logging.INFO


def test_get_logger_returns_structlog_bound(capfd: pytest.CaptureFixture[str]):
    logging_setup.configure_logging(force=True)
    log = logging_setup.get_logger("oxi.struct")
    log.info("structured", task_id=42, host="local")
    parsed = json.loads(capfd.readouterr().err.strip())
    assert parsed["event"] == "structured"
    assert parsed["task_id"] == 42
    assert parsed["host"] == "local"


def test_existing_stdlib_loggers_route_through_renderer(
    capfd: pytest.CaptureFixture[str],
):
    """Existing oxi modules use logging.getLogger — confirm they render."""
    logging_setup.configure_logging(force=True)
    logging.getLogger("oxi_core.v3.dispatch").error("dispatch-failed")
    parsed = json.loads(capfd.readouterr().err.strip())
    assert parsed["level"] == "error"
    assert parsed["logger"] == "oxi_core.v3.dispatch"
    assert parsed["event"] == "dispatch-failed"
