"""Tests for `oxi_core.v3._render` — the Rich rendering helpers.

We don't snapshot ANSI output; the goal is to confirm:

1. ``make_console`` honours ``NO_COLOR`` and a non-TTY stream.
2. The rendering helpers emit the substrings legacy tests rely on, so
   ``oxi status`` and ``oxi v3 tick`` keep their grep-friendly output
   even when rendered through Rich.
"""

from __future__ import annotations

import io

import pytest

from oxi_core.v3._render import (
    make_console,
    render_budget_line,
    render_recent_events,
    render_status_counts,
    render_status_header,
    render_tick_iteration_header,
    render_tick_summary,
)


@pytest.fixture
def buf() -> io.StringIO:
    return io.StringIO()


def _strip_ansi(text: str) -> str:
    """Tests assert on plain text, not on ANSI codes — drop them."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_make_console_disables_color_when_NO_COLOR_set(buf, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    console = make_console(buf)
    assert console.no_color is True


def test_make_console_no_color_on_non_tty(buf, monkeypatch):
    # Without NO_COLOR, a non-TTY buffer should still strip color since
    # the Console isn't talking to a terminal.
    monkeypatch.delenv("NO_COLOR", raising=False)
    console = make_console(buf)
    console.print("[bold red]ALERT[/]")
    output = buf.getvalue()
    # The bold-red wrapper should not have rendered as ANSI escape codes
    # because StringIO isn't a TTY.
    assert "\x1b[" not in output
    assert "ALERT" in output


def test_status_header_killswitch_off(buf):
    console = make_console(buf)
    render_status_header(
        console,
        version="0.1.0",
        instance="oxi-test",
        plan_tier="standard",
        repo="owner/repo",
        killswitch_active=False,
        killswitch_reason="",
        killswitch_path="/tmp/oxi.kill",
    )
    out = _strip_ansi(buf.getvalue())
    assert "oxi" in out
    assert "0.1.0" in out
    assert "oxi-test" in out
    assert "plan tier" in out
    assert "standard" in out
    assert "owner/repo" in out
    assert "killswitch" in out
    assert "off" in out
    # The "ACTIVE" marker must NOT appear when killswitch is off.
    assert "ACTIVE" not in out


def test_status_header_killswitch_active_with_reason(buf):
    console = make_console(buf)
    render_status_header(
        console,
        version="0.1.0",
        instance="oxi-test",
        plan_tier="standard",
        repo="owner/repo",
        killswitch_active=True,
        killswitch_reason="maintenance window",
        killswitch_path="/tmp/oxi.kill",
    )
    out = _strip_ansi(buf.getvalue())
    assert "ACTIVE" in out
    assert "maintenance window" in out
    assert "/tmp/oxi.kill" in out


def test_budget_line_substrings(buf):
    console = make_console(buf)
    render_budget_line(
        console,
        today_spend_usd=1.23,
        daily_soft_warn_usd=5.0,
        daily_hard_cap_usd=10.0,
        verdict="OK",
        marker="  ",
    )
    out = _strip_ansi(buf.getvalue())
    assert "budget (today)" in out
    assert "$1.23" in out
    assert "$5.00" in out
    assert "$10.00" in out
    assert "OK" in out


def test_status_counts_empty_emits_no_tasks(buf):
    console = make_console(buf)
    render_status_counts(console, {})
    out = _strip_ansi(buf.getvalue())
    assert "task counts by status" in out
    assert "(no tasks)" in out


def test_status_counts_renders_each_row(buf):
    console = make_console(buf)
    render_status_counts(console, {"merged": 7, "dispatched": 2})
    out = _strip_ansi(buf.getvalue())
    assert "merged" in out
    assert "7" in out
    assert "dispatched" in out
    assert "2" in out


def test_recent_events_empty(buf):
    console = make_console(buf)
    render_recent_events(console, [])
    out = _strip_ansi(buf.getvalue())
    assert "recent events" in out
    assert "(none)" in out


def test_recent_events_renders_rows(buf):
    console = make_console(buf)
    rows = [
        {"created_at": "2026-04-25 10:00:00", "kind": "dispatch_started", "task_id": 5},
        {"created_at": "2026-04-25 10:01:00", "kind": "pr_observed", "task_id": 5},
    ]
    render_recent_events(console, rows)
    out = _strip_ansi(buf.getvalue())
    assert "2026-04-25 10:00:00" in out
    assert "dispatch_started" in out
    assert "task#5" in out
    assert "pr_observed" in out


def test_tick_iteration_header(buf):
    console = make_console(buf)
    render_tick_iteration_header(
        console,
        iteration=2,
        total=3,
        timestamp="14:22:10",
        real_claude=False,
    )
    out = _strip_ansi(buf.getvalue())
    assert "tick 2/3" in out
    assert "14:22:10" in out
    assert "reconciliation" in out


def test_tick_iteration_header_real_claude_label(buf):
    console = make_console(buf)
    render_tick_iteration_header(
        console,
        iteration=1,
        total=1,
        timestamp="00:00:00",
        real_claude=True,
    )
    out = _strip_ansi(buf.getvalue())
    assert "REAL CLAUDE" in out


def test_tick_summary_clean(buf):
    console = make_console(buf)
    render_tick_summary(
        console, abandoned=0, recovered=0, real_claude=False
    )
    out = _strip_ansi(buf.getvalue())
    assert "tick done" in out
    assert "abandoned: 0" in out
    assert "auto_recovered: 0" in out


def test_tick_summary_with_abandoned_flags_warning(buf):
    console = make_console(buf)
    render_tick_summary(
        console, abandoned=2, recovered=0, real_claude=True
    )
    out = _strip_ansi(buf.getvalue())
    assert "abandoned: 2" in out
    assert "real-claude" in out


def test_make_console_does_not_force_terminal_when_NO_COLOR_unset(buf, monkeypatch):
    """Sanity: NO_COLOR=unset on a StringIO must not blow up."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    console = make_console(buf)
    assert console.no_color is False
