"""Rich-based rendering helpers for the CLI.

The CLI's user-facing surfaces (``oxi status``, ``oxi v3 tick``) used to
emit raw ``print()`` calls.  This module wraps the same data in Rich
primitives — ``Table``, ``Panel``, ``Console`` — so a TTY operator gets
structured output while non-TTY consumers (pipes, redirects, ``NO_COLOR``)
get the same plain text the assertions in ``tests/test_cli.py`` rely on.

Rich auto-degrades when:

- ``NO_COLOR`` is set (any value, per https://no-color.org)
- the output stream is not a TTY (pipe, redirect)
- ``TERM=dumb``

The :func:`make_console` helper centralises that detection so every
caller gets identical behaviour.

This module deliberately holds no engine state and does no DB access.
Callers pass already-shaped data; rendering is pure formatting.
"""

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


def make_console(stream: TextIO | None = None) -> Console:
    """Return a Console that respects NO_COLOR and TTY detection.

    When ``NO_COLOR`` is set, Rich will skip ANSI codes entirely.
    When the underlying stream isn't a TTY, Rich also skips them.

    Callers that need to *force* plain output (CI snapshot tests, for
    instance) can set ``NO_COLOR=1`` in the environment — there is
    intentionally no Python-level override; we want the same env var
    to control every code path.
    """
    target = stream if stream is not None else sys.stdout
    no_color = "NO_COLOR" in os.environ
    return Console(
        file=target,
        no_color=no_color,
        # force_terminal=None lets Rich detect the TTY itself.
        force_terminal=None,
        # Avoid wrapping long status lines on narrow terminals — operators
        # rely on grep-friendly output.
        soft_wrap=False,
        # Highlighting can colorise numbers/paths in ways that surprise
        # operators reading the output.  Keep semantics in our control.
        highlight=False,
    )


# ---------------------------------------------------------------------------
# `oxi status` rendering
# ---------------------------------------------------------------------------


def render_status_header(
    console: Console,
    *,
    version: str,
    instance: str,
    plan_tier: str,
    repo: str,
    killswitch_active: bool,
    killswitch_reason: str,
    killswitch_path: str,
    killswitch_glyph: str = "",
) -> None:
    """Top block of ``oxi status``: version + adapter identity + killswitch.

    Rendered as a small Rich ``Table`` (no header row) so the columns
    line up regardless of label length.  When the killswitch is active
    the value cell is shown in bold red; tests still match the substring
    ``"ACTIVE"`` because Rich emits it as part of the styled text.

    ``killswitch_glyph`` is the standard-vocabulary glyph (typically
    ``PAUSED`` from ``oxi_core.v3._glyphs``).  When provided, it's
    prepended to the ACTIVE marker.  Defaulting to ``""`` keeps this
    helper backward-compatible for tests that don't pass it.
    """
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
    table.add_column("label", style="dim", no_wrap=True)
    table.add_column("value", overflow="fold")

    table.add_row("oxi", version)
    table.add_row("instance", instance)
    table.add_row("plan tier", plan_tier)
    table.add_row("repo", repo)

    if killswitch_active:
        suffix = f" — {killswitch_reason}" if killswitch_reason else ""
        prefix = f"{killswitch_glyph} " if killswitch_glyph else ""
        table.add_row("killswitch", f"[bold red]{prefix}ACTIVE[/]{suffix}")
        table.add_row("", f"[dim]{killswitch_path}[/]")
    else:
        table.add_row("killswitch", "off")

    console.print(table)
    console.print()


def render_budget_line(
    console: Console,
    *,
    today_spend_usd: float,
    daily_soft_warn_usd: float,
    daily_hard_cap_usd: float,
    verdict: str,
    marker: str,
) -> None:
    """Render the budget summary line.

    Colour comes from the ``marker`` glyph — ``⚠`` (warn) goes amber,
    ``✗`` (hard-stop) goes red, plain (OK) stays default.  Tests assert
    on the cap values, so the numeric formatting is preserved exactly.
    """
    style = ""
    if marker.strip() == "⚠":
        style = "yellow"
    elif marker.strip() == "✗":
        style = "bold red"

    body = (
        f"{marker}budget (today): "
        f"${today_spend_usd:.2f} spent / "
        f"${daily_soft_warn_usd:.2f} warn / "
        f"${daily_hard_cap_usd:.2f} hard — {verdict}"
    )
    if style:
        console.print(f"[{style}]{body}[/]")
    else:
        console.print(body)
    console.print()


def render_status_counts(
    console: Console,
    counts: dict[str, int],
) -> None:
    """Render the status histogram as a small two-column table.

    Empty ``counts`` prints ``(no tasks)`` so the existing test assertion
    keeps working.
    """
    console.print("task counts by status:")
    if not counts:
        console.print("  (no tasks)")
        return
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
    table.add_column("status", style="cyan", no_wrap=True)
    table.add_column("count", justify="right")
    for status_name in sorted(counts):
        table.add_row(f"  {status_name}", str(counts[status_name]))
    console.print(table)


def render_recent_events(
    console: Console,
    events: list[Any],
) -> None:
    """Render the last-N events block.

    ``events`` is a list of sqlite3.Row-like objects with
    ``created_at``, ``kind``, and ``task_id`` keys.  Empty list prints
    ``(none)``.
    """
    console.print()
    console.print("recent events:")
    if not events:
        console.print("  (none)")
        return
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
    table.add_column("when", style="dim", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("task")
    for ev in events:
        table.add_row(
            f"  {ev['created_at']}",
            f"{ev['kind']}",
            f"task#{ev['task_id']}",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# `oxi v3 tick` rendering
# ---------------------------------------------------------------------------


def render_tick_iteration_header(
    console: Console,
    *,
    iteration: int,
    total: int,
    timestamp: str,
    real_claude: bool,
) -> None:
    """Print a header rule for a single tick iteration.

    Format: ``── tick 1/3 · 14:22:10 · reconciliation ──``.  When piped
    or under ``NO_COLOR`` the rule degrades to dashes only — the
    timestamp + label remain readable.
    """
    label = "REAL CLAUDE" if real_claude else "reconciliation"
    title = f"tick {iteration}/{total} · {timestamp} · {label}"
    console.rule(title, style="dim", align="left")


def render_tick_summary(
    console: Console,
    *,
    abandoned: int,
    recovered: int,
    real_claude: bool,
) -> None:
    """Final summary box at the end of ``oxi v3 tick``.

    Renders inside a Rich ``Panel`` so the iteration log above it has a
    clear visual terminator.  Body contents are 3 lines (5 with
    real-claude additions in a future iteration; keeping it minimal for
    this PR).
    """
    if abandoned:
        title_style = "yellow"
        glyph = "⚠"
    elif recovered:
        title_style = "green"
        glyph = "✓"
    else:
        title_style = "dim"
        glyph = "·"

    body_lines = [
        f"abandoned: {abandoned}",
        f"auto_recovered: {recovered}",
        f"mode: {'real-claude' if real_claude else 'reconciliation-only'}",
    ]
    panel = Panel(
        "\n".join(body_lines),
        title=f"[{title_style}]{glyph} tick done[/]",
        title_align="left",
        border_style=title_style,
        padding=(0, 1),
    )
    console.print(panel)


__all__ = [
    "make_console",
    "render_budget_line",
    "render_recent_events",
    "render_status_counts",
    "render_status_header",
    "render_tick_iteration_header",
    "render_tick_summary",
]
