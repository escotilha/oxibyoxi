"""NotificationBackend protocol — how oxi reaches operators.

Deadman detection, budget hard-stops, OAuth expiry, CI failure filer
— all need to surface something outside the engine's own DB. The
channel varies by fork (Slack, email, webhook, PagerDuty, stdout).
``NotificationBackend`` is the interface every module uses; forks
supply the implementation.

Backends here:

- ``LoggingBackend``: writes notifications to a Python logger. Safe
  default for forks that haven't wired something yet.
- ``StderrBackend``: writes to stderr. Useful for CLI-driven runs.
- ``RecordingBackend``: in-memory list. Tests use this.

Real backends (Slack webhook, email, etc.) are fork-specific and live
in each fork's adapter package. The protocol here guarantees any
backend can be substituted for another.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class Level(Enum):
    """Notification urgency."""

    INFO = "info"
    WARN = "warn"
    ALERT = "alert"
    DEAD = "dead"


@dataclass(frozen=True)
class Notification:
    """One notification event."""

    level: Level
    title: str
    body: str
    tags: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class NotificationBackend(Protocol):
    """Minimal interface every notification implementation satisfies."""

    def send(self, notification: Notification) -> None: ...


# ---------------------------------------------------------------------------
# Default backends
# ---------------------------------------------------------------------------


@dataclass
class LoggingBackend:
    """Writes notifications through ``logging``. Safe default for forks.

    Levels map: INFO→info, WARN→warning, ALERT→error, DEAD→critical.
    Uses the ``oxi.notification`` logger name so operators can route.
    """

    logger_name: str = "oxi.notification"

    def send(self, notification: Notification) -> None:
        logger = logging.getLogger(self.logger_name)
        mapping = {
            Level.INFO: logging.INFO,
            Level.WARN: logging.WARNING,
            Level.ALERT: logging.ERROR,
            Level.DEAD: logging.CRITICAL,
        }
        level = mapping.get(notification.level, logging.INFO)
        tags = " ".join(f"[{t}]" for t in notification.tags)
        logger.log(level, "%s %s — %s", tags, notification.title, notification.body)


@dataclass
class StderrBackend:
    """Writes notifications to stderr. Useful for CLI-driven runs.

    Includes level + title on the first line, body indented on the
    next, tags appended in brackets. No color — callers can filter
    by level prefix if they want it.
    """

    def send(self, notification: Notification) -> None:
        tags = (
            " " + " ".join(f"[{t}]" for t in notification.tags)
            if notification.tags
            else ""
        )
        sys.stderr.write(
            f"[{notification.level.value.upper()}] "
            f"{notification.title}{tags}\n"
        )
        if notification.body:
            for line in notification.body.splitlines():
                sys.stderr.write(f"  {line}\n")


@dataclass
class RecordingBackend:
    """Records notifications in a list. Tests use this."""

    records: list[Notification] = field(default_factory=list)

    def send(self, notification: Notification) -> None:
        self.records.append(notification)

    def clear(self) -> None:
        self.records.clear()

    @property
    def count(self) -> int:
        return len(self.records)

    def last(self) -> Notification | None:
        return self.records[-1] if self.records else None


__all__ = [
    "Level",
    "LoggingBackend",
    "Notification",
    "NotificationBackend",
    "RecordingBackend",
    "StderrBackend",
]
