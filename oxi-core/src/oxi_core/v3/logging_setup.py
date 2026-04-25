"""Structured logging configuration for oxi.

We use structlog as the rendering layer, but every existing module in
the codebase calls `logging.getLogger(__name__).info(...)` against the
stdlib. So this module configures stdlib logging to route through
structlog's :class:`ProcessorFormatter`, which gives us:

  - All existing call sites keep working unchanged.
  - Output is JSON when ``OXI_LOG_FORMAT=json`` (the default for
    automation / CI / Routines), or pretty-printed key-value pairs
    when ``OXI_LOG_FORMAT=console`` (developer-friendly).
  - Log level set via ``OXI_LOG_LEVEL`` (default: INFO).
  - ``print()`` calls in CLI commands are deliberately untouched —
    those are user-facing output, not diagnostic logging.

Call ``configure_logging()`` once at process startup. Idempotent — a
second call clears prior handlers and reconfigures.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def _level_from_env() -> int:
    raw = os.environ.get("OXI_LOG_LEVEL", "INFO").upper().strip()
    return getattr(logging, raw, logging.INFO)


def _format_from_env() -> str:
    raw = os.environ.get("OXI_LOG_FORMAT", "json").lower().strip()
    return raw if raw in ("json", "console") else "json"


def configure_logging(*, force: bool = False) -> None:
    """Wire structlog into stdlib logging. Safe to call repeatedly."""
    if not force and getattr(configure_logging, "_done", False):
        return

    level = _level_from_env()
    fmt = _format_from_env()

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "console":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(),
        )
    else:
        renderer = structlog.processors.JSONRenderer()

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    configure_logging._done = True  # type: ignore[attr-defined]


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog-bound logger.

    New code should prefer this over ``logging.getLogger(__name__)``
    so callers can use ``log.info("event_name", key=value)`` syntax.
    Existing stdlib loggers continue to work — both paths render
    through the same handler.
    """
    return structlog.stdlib.get_logger(name)


__all__ = ["configure_logging", "get_logger"]
