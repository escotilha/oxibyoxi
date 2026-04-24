"""Tests for oxi_core.v3.oauth_watch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oxi_core import db
from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3 import oauth_watch
from oxi_core.v3.notification import Level, RecordingBackend


@dataclass
class _Adapter:
    db_path_value: str

    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig(db_path=self.db_path_value)
    def budget(self): return BudgetCaps()
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (
            DispatchHost(
                name="local", ssh_alias=None,
                max_concurrent=1, worktree_root="/tmp",
            ),
        )
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def env(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "notifier": RecordingBackend(),
        "tmp": tmp_path,
    }
    handle.connection.close()


def _write_creds(path: Path, expires_at: datetime, shape: str = "top_level") -> None:
    if shape == "top_level":
        path.write_text(json.dumps({
            "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        }))
    elif shape == "oauth_account":
        # Epoch milliseconds — same shape Claude CLI stores when OAuth-authed.
        path.write_text(json.dumps({
            "oauthAccount": {
                "expiresAt": int(expires_at.timestamp() * 1000),
            },
        }))
    elif shape == "nested_iso":
        path.write_text(json.dumps({
            "claudeAiOauth": {
                "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
            },
        }))
    elif shape == "top_level_epoch":
        path.write_text(json.dumps({
            "expiresAt": int(expires_at.timestamp() * 1000),
        }))
    else:
        raise ValueError(f"unknown shape {shape!r}")


# ---------------------------------------------------------------------------
# Missing / malformed creds
# ---------------------------------------------------------------------------


def test_missing_file_returns_unknown(env):
    result = oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=env["tmp"] / "does-not-exist.json",
    )
    assert result.status is oauth_watch.Status.UNKNOWN
    assert result.notified is False
    assert env["notifier"].count == 0


def test_malformed_json_returns_unknown(env):
    path = env["tmp"] / "creds.json"
    path.write_text("{not valid json")
    result = oauth_watch.check(
        env["conn"], env["notifier"], credentials_path=path,
    )
    assert result.status is oauth_watch.Status.UNKNOWN


def test_unknown_shape_returns_unknown(env):
    path = env["tmp"] / "creds.json"
    path.write_text(json.dumps({"foo": "bar"}))
    result = oauth_watch.check(
        env["conn"], env["notifier"], credentials_path=path,
    )
    assert result.status is oauth_watch.Status.UNKNOWN


# ---------------------------------------------------------------------------
# Shape recognition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", [
    "top_level", "oauth_account", "nested_iso", "top_level_epoch",
])
def test_recognizes_credential_shapes(env, shape):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(days=7)  # comfortably in the future
    path = env["tmp"] / "creds.json"
    _write_creds(path, expires, shape=shape)
    result = oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now,
    )
    assert result.status is oauth_watch.Status.OK
    assert result.expires_at is not None


# ---------------------------------------------------------------------------
# Status buckets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hours_ahead,expected_status", [
    (72, oauth_watch.Status.OK),
    (36, oauth_watch.Status.EXPIRING_SOON),
    (6, oauth_watch.Status.EXPIRING_URGENTLY),
    (0.5, oauth_watch.Status.CRITICAL),
    (-1, oauth_watch.Status.EXPIRED),
])
def test_status_thresholds(env, hours_ahead, expected_status):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(hours=hours_ahead)
    path = env["tmp"] / "creds.json"
    _write_creds(path, expires)
    result = oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now,
        info_hours=48, warn_hours=12, alert_hours=1,
    )
    assert result.status is expected_status


# ---------------------------------------------------------------------------
# Notification behavior
# ---------------------------------------------------------------------------


def test_ok_status_does_not_notify(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(days=30)
    path = env["tmp"] / "creds.json"
    _write_creds(path, expires)
    result = oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now,
    )
    assert result.notified is False
    assert env["notifier"].count == 0


def test_expiring_soon_fires_info_level(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(hours=36)
    path = env["tmp"] / "creds.json"
    _write_creds(path, expires)
    oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now,
        info_hours=48, warn_hours=12, alert_hours=1,
    )
    assert env["notifier"].count == 1
    assert env["notifier"].last().level is Level.INFO


def test_expired_fires_dead_level(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    expires = now - timedelta(hours=1)
    path = env["tmp"] / "creds.json"
    _write_creds(path, expires)
    oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now,
    )
    assert env["notifier"].last().level is Level.DEAD
    assert "EXPIRED" in env["notifier"].last().title


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_same_status_not_refired_within_window(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(hours=36)
    path = env["tmp"] / "creds.json"
    _write_creds(path, expires)

    oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now,
        info_hours=48, warn_hours=12, alert_hours=1,
    )
    oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now + timedelta(minutes=30),
        info_hours=48, warn_hours=12, alert_hours=1,
    )
    assert env["notifier"].count == 1


def test_escalating_status_fires_through_rate_limit(env):
    """INFO then WARN fire both events even if INFO was just raised."""
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    path = env["tmp"] / "creds.json"

    # First pass: 36h out → INFO.
    _write_creds(path, now + timedelta(hours=36))
    oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now,
        info_hours=48, warn_hours=12, alert_hours=1,
    )
    # Second pass: creds file rewritten with closer expiry — WARN.
    _write_creds(path, now + timedelta(hours=6))
    oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now + timedelta(minutes=10),
        info_hours=48, warn_hours=12, alert_hours=1,
    )
    assert env["notifier"].count == 2
    levels = [n.level for n in env["notifier"].records]
    assert Level.INFO in levels
    assert Level.WARN in levels


# ---------------------------------------------------------------------------
# Ledger event
# ---------------------------------------------------------------------------


def test_notification_emits_ledger_event(env):
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    path = env["tmp"] / "creds.json"
    _write_creds(path, now + timedelta(hours=36))
    oauth_watch.check(
        env["conn"], env["notifier"],
        credentials_path=path, now=now,
        info_hours=48,
    )
    kinds = [r[0] for r in env["conn"].execute("SELECT kind FROM event")]
    assert "oauth_watch_expiring_soon" in kinds


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


def test_status_dataclass_is_frozen():
    from dataclasses import FrozenInstanceError
    s = oauth_watch.OAuthStatus(
        status=oauth_watch.Status.OK,
        expires_at=None, hours_remaining=None, notified=False,
    )
    with pytest.raises(FrozenInstanceError):
        s.notified = True  # type: ignore[misc]
