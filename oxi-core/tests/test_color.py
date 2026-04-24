"""Tests for oxi_core.v3._color — ANSI color helpers.

Key invariants:
- NO_COLOR=1 (or any value) → plain text, no escape codes.
- Non-TTY stdout → plain text, no escape codes.
- TTY stdout with no NO_COLOR → ANSI escape codes present.
- Output text content is stable across all three modes.
"""

from __future__ import annotations

import io

import pytest

from oxi_core.v3._color import _colors_enabled, green, red, yellow

# ---------------------------------------------------------------------------
# _colors_enabled
# ---------------------------------------------------------------------------


def test_colors_disabled_by_no_color(monkeypatch):
    """NO_COLOR env var (any value) disables colors regardless of TTY."""
    monkeypatch.setenv("NO_COLOR", "1")
    # Even if we pass a fake TTY stream, NO_COLOR wins.
    fake_tty = type("FakeTTY", (), {"isatty": lambda self: True})()
    assert _colors_enabled(fake_tty) is False


def test_colors_disabled_by_no_color_empty_value(monkeypatch):
    """NO_COLOR='' (empty string) still disables colors per spec."""
    monkeypatch.setenv("NO_COLOR", "")
    fake_tty = type("FakeTTY", (), {"isatty": lambda self: True})()
    assert _colors_enabled(fake_tty) is False


def test_colors_disabled_when_not_tty(monkeypatch):
    """Non-TTY stream → colors disabled even without NO_COLOR."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _colors_enabled(io.StringIO()) is False


def test_colors_enabled_on_tty(monkeypatch):
    """TTY stream without NO_COLOR → colors enabled."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    fake_tty = type("FakeTTY", (), {"isatty": lambda self: True})()
    assert _colors_enabled(fake_tty) is True


def test_colors_disabled_when_stdout_not_tty(monkeypatch):
    """No stream arg + sys.stdout not a TTY (default in test runner) → disabled."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    # capsys / pytest capture sys.stdout as a non-TTY buffer.
    assert _colors_enabled() is False


# ---------------------------------------------------------------------------
# green / yellow / red — plain-text passthrough when colors off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", [green, yellow, red])
def test_plain_passthrough_no_color(fn, monkeypatch):
    """With NO_COLOR set, the color functions return the text unchanged."""
    monkeypatch.setenv("NO_COLOR", "1")
    assert fn("hello") == "hello"


@pytest.mark.parametrize("fn", [green, yellow, red])
def test_plain_passthrough_non_tty(fn, monkeypatch):
    """Non-TTY stream → text returned unchanged."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert fn("world", io.StringIO()) == "world"


# ---------------------------------------------------------------------------
# green / yellow / red — escape codes emitted on TTY
# ---------------------------------------------------------------------------


def _fake_tty():
    return type("FakeTTY", (), {"isatty": lambda self: True})()


def test_green_emits_escape_on_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = green("ok", _fake_tty())
    assert "\033[32m" in result
    assert "ok" in result
    assert result.endswith("\033[0m")


def test_yellow_emits_escape_on_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = yellow("stuck", _fake_tty())
    assert "\033[33m" in result
    assert "stuck" in result
    assert result.endswith("\033[0m")


def test_red_emits_escape_on_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = red("fail", _fake_tty())
    assert "\033[31m" in result
    assert "fail" in result
    assert result.endswith("\033[0m")


# ---------------------------------------------------------------------------
# Text content is stable regardless of color mode
# ---------------------------------------------------------------------------


def test_text_content_stable_across_modes(monkeypatch):
    """The non-escape-code content of the line is identical in all three modes."""
    phrase = "  heartbeat: considered=1 abandoned=1 protected_by_pr=0 fresh=0"

    # NO_COLOR mode
    monkeypatch.setenv("NO_COLOR", "1")
    no_color_out = yellow(phrase)

    # non-TTY mode
    monkeypatch.delenv("NO_COLOR")
    non_tty_out = yellow(phrase, io.StringIO())

    # TTY mode (strip escape codes for comparison)
    tty_out = yellow(phrase, _fake_tty())
    import re
    stripped = re.sub(r"\033\[[0-9;]*m", "", tty_out)

    assert no_color_out == phrase
    assert non_tty_out == phrase
    assert stripped == phrase
