"""ANSI color helpers for terminal output.

Rules (in priority order):
1. ``NO_COLOR`` env var is set (any value) → no escape codes.
2. ``sys.stdout`` is not a TTY (e.g. pipe, redirect) → no escape codes.
3. Otherwise: emit ANSI SGR escape codes.

Callers should pass the output stream when they have one; the default
is ``sys.stdout`` which is the right choice for every current call site.

Color semantics used by ``oxi v3 tick``:
- green  — task made progress / dispatch succeeded
- yellow — task is stuck / abandoned / heartbeat reaped
- red    — task failed / dispatch failed

References:
- https://no-color.org/ — the ``NO_COLOR`` spec
- ECMA-48 §8.3.117 — SGR (Select Graphic Rendition)
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

# SGR codes used in this module. Named explicitly to make grep-ability
# easier; the numeric values are part of ECMA-48 and are stable.
_RESET = "\033[0m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


# ---------------------------------------------------------------------------
# Brand accent
# ---------------------------------------------------------------------------

# Single accent color used by the dashboard and (where the renderer
# supports it) the CLI.  Picked to evoke the "ox" / oxidation thread in
# the project name without colliding with the semantic green/yellow/red
# vocabulary used for status reporting.  Surfaces should reference this
# constant rather than hardcoding the hex.
ACCENT_HEX = "#E66A2C"
# Darker shade for hover / active states on the same accent.
ACCENT_HEX_DARK = "#B04E1B"


def _colors_enabled(stream: TextIO | None = None) -> bool:
    """Return True when ANSI escape codes should be emitted.

    Checks (in order):
    - ``NO_COLOR`` set in env → False (spec: any value, including empty).
    - ``stream`` (default: ``sys.stdout``) is not a TTY → False.
    - Otherwise → True.
    """
    if "NO_COLOR" in os.environ:
        return False
    target = stream if stream is not None else sys.stdout
    return hasattr(target, "isatty") and target.isatty()


def _wrap(code: str, text: str, stream: TextIO | None = None) -> str:
    """Wrap *text* with an SGR *code* if colors are enabled, else return *text*."""
    if not _colors_enabled(stream):
        return text
    return f"{code}{text}{_RESET}"


def green(text: str, stream: TextIO | None = None) -> str:
    """Return *text* wrapped in ANSI green (SGR 32), or plain if disabled."""
    return _wrap(_GREEN, text, stream)


def yellow(text: str, stream: TextIO | None = None) -> str:
    """Return *text* wrapped in ANSI yellow (SGR 33), or plain if disabled."""
    return _wrap(_YELLOW, text, stream)


def red(text: str, stream: TextIO | None = None) -> str:
    """Return *text* wrapped in ANSI red (SGR 31), or plain if disabled."""
    return _wrap(_RED, text, stream)


__all__ = [
    "ACCENT_HEX",
    "ACCENT_HEX_DARK",
    "green",
    "red",
    "yellow",
]
