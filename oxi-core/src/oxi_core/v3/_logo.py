r"""ASCII logo used by ``oxi --version``, the dashboard, and the README.

A two-line glyph reading as bullhorns over the project name.  Pure
ASCII (no BMP-only escapes) so it renders identically in terminals,
in HTML ``<pre>`` blocks, and in Markdown fenced code.

    ┄ \\_// ┄
    ┄  oxi ┄

The shipped form is just the inner glyph; surrounding decoration is
each surface's call.

Width: 6 characters.  Height: 2 lines.  No trailing whitespace on
either line — important so terminals don't render extra background
where the logo backdrop is colored.

Why this glyph: the curved \\_// motif reads as horns (the project's
name, oxi, evokes ox), and the lowercase ``oxi`` underneath ties the
mark to the wordmark without needing extra typography.  At 6 chars
wide it fits inside a 4-tile dashboard hero row, an 80-column
terminal, and the README top.
"""

from __future__ import annotations

# The two lines of the glyph.  Kept as separate constants so callers
# can join them with their own separator (e.g. CSS line-height in HTML
# vs. plain newlines in CLI).
LOGO_LINE_1 = r" \_//"
LOGO_LINE_2 = r"  oxi"

# Convenience: full glyph as a single newline-joined string.  Use this
# for CLI output and for ``<pre>`` tags in HTML.
LOGO = "\n".join([LOGO_LINE_1, LOGO_LINE_2])


__all__ = [
    "LOGO",
    "LOGO_LINE_1",
    "LOGO_LINE_2",
]
