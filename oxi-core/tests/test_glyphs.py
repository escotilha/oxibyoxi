"""Tests for ``oxi_core.v3._glyphs`` — the shared status-glyph vocabulary.

We don't snapshot rendered output; the contract this module provides
is just "the same status string maps to the same glyph everywhere".
The tests pin the canonical mappings and assert the unknown-status
fallback is empty (so callers can do ``f"{glyph_for_status(s)} {s}"``
without producing a stray leading space).
"""

from __future__ import annotations

from oxi_core.v3._glyphs import (
    FAIL,
    OK,
    PAUSED,
    QUEUED,
    RUNNING,
    glyph_for_status,
)


def test_constants_are_single_characters():
    """The vocabulary is intentionally tight — five single glyphs."""
    for glyph in (QUEUED, RUNNING, OK, FAIL, PAUSED):
        assert isinstance(glyph, str)
        assert len(glyph) == 1


def test_constants_are_distinct():
    """Each state must be visually distinguishable."""
    glyphs = {QUEUED, RUNNING, OK, FAIL, PAUSED}
    assert len(glyphs) == 5


def test_glyph_for_planned():
    assert glyph_for_status("planned") == QUEUED


def test_glyph_for_dispatched():
    assert glyph_for_status("dispatched") == RUNNING


def test_glyph_for_merged():
    assert glyph_for_status("merged") == OK


def test_glyph_for_failed():
    assert glyph_for_status("failed") == FAIL


def test_glyph_for_abandoned():
    """Abandoned tasks share the FAIL glyph — they're terminal failures
    from the operator's point of view."""
    assert glyph_for_status("abandoned") == FAIL


def test_glyph_for_killswitched():
    assert glyph_for_status("killswitched") == PAUSED


def test_glyph_for_paused():
    assert glyph_for_status("paused") == PAUSED


def test_glyph_for_running():
    assert glyph_for_status("running") == RUNNING


def test_glyph_for_queued():
    assert glyph_for_status("queued") == QUEUED


def test_glyph_for_ok():
    assert glyph_for_status("ok") == OK


def test_glyph_for_healthy():
    assert glyph_for_status("healthy") == OK


def test_glyph_for_unknown_status_is_empty():
    """Unknown statuses return ``""`` so callers don't render literal
    placeholders or a stray "?"."""
    assert glyph_for_status("unknown") == ""
    assert glyph_for_status("foo-bar") == ""


def test_glyph_for_empty_string_is_empty():
    assert glyph_for_status("") == ""


def test_glyph_lookup_case_insensitive():
    assert glyph_for_status("PLANNED") == QUEUED
    assert glyph_for_status("Dispatched") == RUNNING
    assert glyph_for_status("MERGED") == OK


# ---------------------------------------------------------------------------
# Integration smoke tests — the glyphs land where they're supposed to.
# ---------------------------------------------------------------------------


def test_brief_render_includes_glyphs():
    """The markdown brief prepends the status glyph in the histogram."""
    from datetime import UTC, datetime

    from oxi_core.v3.brief import Brief

    brief = Brief(
        generated_at=datetime(2026, 4, 25, 10, 0, tzinfo=UTC),
        window_hours=24,
        status_counts={"merged": 3, "planned": 2, "failed": 1},
        total_cost_usd=0.0,
        recent_merges=[],
        recent_failures=[],
    )
    out = brief.render()
    # Every status line should include both the glyph and the label.
    assert f"{OK} **merged**" in out
    assert f"{QUEUED} **planned**" in out
    assert f"{FAIL} **failed**" in out


def test_brief_render_unknown_status_no_stray_space():
    """Unknown statuses get no glyph prefix — the bullet should still
    look natural ('- **foo**: 1'), not '-  **foo**: 1'."""
    from datetime import UTC, datetime

    from oxi_core.v3.brief import Brief

    brief = Brief(
        generated_at=datetime(2026, 4, 25, 10, 0, tzinfo=UTC),
        window_hours=24,
        status_counts={"weird-state": 1},
        total_cost_usd=0.0,
        recent_merges=[],
        recent_failures=[],
    )
    out = brief.render()
    assert "- **weird-state**: 1" in out
    # No double-space artefact.
    assert "-  **weird-state**" not in out
