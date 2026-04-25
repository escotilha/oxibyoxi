r"""Monitor Claude OAuth credential expiry.

Expired OAuth silently kills every dispatch — the subprocess exits
immediately with an unhelpful auth error, and the operator has no
signal until they notice the dashboard is not advancing. Re-auth
requires a browser, so there's no autonomous remediation; the best
we can do is give the operator lead time to re-auth before things
actually break.

``oauth_watch.check`` reads the standard Claude CLI credentials file
(``~/.claude/.credentials.json`` by default, overridable for tests),
computes the time-to-expiry, and compares it to four thresholds:

- ``info_threshold``:  early signal (default: 48 hours).
- ``warn_threshold``:  operator should schedule re-auth (12 hours).
- ``alert_threshold``: imminent (1 hour).
- ``dead``:            already expired.

Each level fires via the injected ``NotificationBackend``. Rate-
limited per level per the same ledger-event check deadman uses.

Supports two credential shapes seen in the wild:

- \`{ "expiresAt": "<ISO-8601>" }\`  (OAuth token stored as a string)
- \`{ "oauthAccount": { "expiresAt": 12345 } }\`  (milliseconds epoch)

Unknown shapes → return a special ``UNKNOWN`` status without firing,
so a malformed file doesn't trigger a false alarm.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, cast

from .notification import Level, Notification, NotificationBackend

logger = logging.getLogger(__name__)


# Defaults chosen so the operator has time to react: 48h first ping,
# 12h firm warning, 1h alert, immediate when expired.
DEFAULT_INFO_HOURS = 48.0
DEFAULT_WARN_HOURS = 12.0
DEFAULT_ALERT_HOURS = 1.0

# Standard Claude CLI credentials file location.
DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


class Status(Enum):
    OK = "ok"
    EXPIRING_SOON = "expiring_soon"  # within info_threshold
    EXPIRING_URGENTLY = "expiring_urgently"  # within warn_threshold
    CRITICAL = "critical"  # within alert_threshold
    EXPIRED = "expired"
    UNKNOWN = "unknown"  # couldn't parse or file missing


@dataclass(frozen=True)
class OAuthStatus:
    """One-shot snapshot of the OAuth credential state."""

    status: Status
    expires_at: datetime | None
    hours_remaining: float | None
    notified: bool


def _read_credentials(path: Path) -> dict[str, Any] | None:
    """Load the credentials file into a dict, or return None.

    Missing file → None. JSON parse errors → None.
    """
    if not path.is_file():
        return None
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _extract_expiry(creds: dict[str, Any]) -> datetime | None:
    """Extract the expiry timestamp from common credential shapes.

    Returns None for unrecognized shapes. Never raises.
    """
    # Shape 1: {"expiresAt": "<ISO-8601>"} at the top level.
    value = creds.get("expiresAt")
    if isinstance(value, str):
        return _parse_iso_or_none(value)
    if isinstance(value, (int, float)):
        return _parse_epoch_ms_or_none(value)

    # Shape 2: {"oauthAccount": {"expiresAt": <ISO or epoch ms>}}
    account = creds.get("oauthAccount")
    if isinstance(account, dict):
        value = account.get("expiresAt")
        if isinstance(value, str):
            return _parse_iso_or_none(value)
        if isinstance(value, (int, float)):
            return _parse_epoch_ms_or_none(value)

    # Shape 3: {"claudeAiOauth": {"expiresAt": ...}}  — seen in some versions
    for key in ("claudeAiOauth", "auth"):
        nested = creds.get(key)
        if isinstance(nested, dict):
            value = nested.get("expiresAt")
            if isinstance(value, str):
                return _parse_iso_or_none(value)
            if isinstance(value, (int, float)):
                return _parse_epoch_ms_or_none(value)

    return None


def _parse_iso_or_none(value: str) -> datetime | None:
    try:
        # Accept trailing Z as UTC.
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _parse_epoch_ms_or_none(value: int | float) -> datetime | None:
    try:
        # Claude stores expiresAt as epoch milliseconds in the OAuth
        # token object. Values below 10^11 look like seconds; above
        # are milliseconds.
        if value < 10**11:
            # seconds
            return datetime.fromtimestamp(value, tz=UTC)
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _status_for(hours_remaining: float, *, info: float, warn: float, alert: float) -> Status:
    if hours_remaining <= 0:
        return Status.EXPIRED
    if hours_remaining <= alert:
        return Status.CRITICAL
    if hours_remaining <= warn:
        return Status.EXPIRING_URGENTLY
    if hours_remaining <= info:
        return Status.EXPIRING_SOON
    return Status.OK


def _status_to_level(status: Status) -> Level | None:
    """Map an OAuth status to a notification level, or None for OK/UNKNOWN."""
    return {
        Status.OK: None,
        Status.UNKNOWN: None,
        Status.EXPIRING_SOON: Level.INFO,
        Status.EXPIRING_URGENTLY: Level.WARN,
        Status.CRITICAL: Level.ALERT,
        Status.EXPIRED: Level.DEAD,
    }[status]


def _already_notified_within(
    conn: sqlite3.Connection,
    status: Status,
    since: datetime,
) -> bool:
    """Has an ``oauth_watch_<status>`` event been emitted since ``since``?"""
    kind = f"oauth_watch_{status.value}"
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT 1 FROM event WHERE kind = ? AND created_at >= ? LIMIT 1",
        (kind, since_str),
    ).fetchone()
    return row is not None


def _emit_event(
    conn: sqlite3.Connection,
    status: Status,
    payload: dict[str, Any],
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (f"oauth_watch_{status.value}", json.dumps(payload)),
        )


def check(
    conn: sqlite3.Connection,
    notifier: NotificationBackend,
    *,
    credentials_path: Path | None = None,
    info_hours: float = DEFAULT_INFO_HOURS,
    warn_hours: float = DEFAULT_WARN_HOURS,
    alert_hours: float = DEFAULT_ALERT_HOURS,
    notification_rate_limit_hours: float = DEFAULT_WARN_HOURS,
    now: datetime | None = None,
) -> OAuthStatus:
    """Read the credentials file; notify if within threshold; return status.

    Arguments:
        conn: SQLite connection (for the rate-limit ledger check).
        notifier: where to send alerts when a threshold is crossed.
        credentials_path: override for the standard Claude CLI path.
            Defaults to ``~/.claude/.credentials.json``.
        info/warn/alert_hours: how many hours before expiry each level fires.
        notification_rate_limit_hours: don't re-fire same status within window.
        now: override for tests.
    """
    now_dt = now if now is not None else datetime.now(tz=UTC)
    path = credentials_path if credentials_path is not None else DEFAULT_CREDENTIALS_PATH

    creds = _read_credentials(path)
    if creds is None:
        # File missing or unreadable — emit UNKNOWN without firing a
        # notification. Operators without Claude OAuth configured
        # shouldn't get spam.
        return OAuthStatus(
            status=Status.UNKNOWN,
            expires_at=None,
            hours_remaining=None,
            notified=False,
        )

    expires_at = _extract_expiry(creds)
    if expires_at is None:
        return OAuthStatus(
            status=Status.UNKNOWN,
            expires_at=None,
            hours_remaining=None,
            notified=False,
        )

    hours_remaining = (expires_at - now_dt).total_seconds() / 3600.0
    status = _status_for(
        hours_remaining,
        info=info_hours, warn=warn_hours, alert=alert_hours,
    )
    level = _status_to_level(status)

    if level is None:
        return OAuthStatus(
            status=status,
            expires_at=expires_at,
            hours_remaining=hours_remaining,
            notified=False,
        )

    # Rate-limit per-status.
    window_start = now_dt - timedelta(hours=notification_rate_limit_hours)
    if _already_notified_within(conn, status, window_start):
        return OAuthStatus(
            status=status,
            expires_at=expires_at,
            hours_remaining=hours_remaining,
            notified=False,
        )

    if status is Status.EXPIRED:
        title = "oxi oauth — credentials EXPIRED"
        body = (
            f"Credentials expired at {expires_at.isoformat()}. "
            "Every dispatch will fail until the operator re-authenticates."
        )
    else:
        title = (
            f"oxi oauth — credentials expiring in "
            f"{hours_remaining:.1f}h ({status.value})"
        )
        body = (
            f"Expires at {expires_at.isoformat()}. Schedule re-auth "
            "before it does."
        )
    notifier.send(Notification(
        level=level,
        title=title,
        body=body,
        tags=("oauth_watch",),
    ))
    _emit_event(conn, status, {
        "expires_at": expires_at.isoformat(),
        "hours_remaining": hours_remaining,
        "credentials_path": str(path),
    })
    return OAuthStatus(
        status=status,
        expires_at=expires_at,
        hours_remaining=hours_remaining,
        notified=True,
    )


__all__ = [
    "DEFAULT_ALERT_HOURS",
    "DEFAULT_CREDENTIALS_PATH",
    "DEFAULT_INFO_HOURS",
    "DEFAULT_WARN_HOURS",
    "OAuthStatus",
    "Status",
    "check",
]
