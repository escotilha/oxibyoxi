"""Tests for oxi_core.v3.spec_ingest."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxi_core.v3.spec_ingest import (
    DEFAULT_FORBIDDEN_STRINGS,
    SpecIngestError,
    SpecItem,
    ingest,
    ingest_to_file,
    parse_spec,
    render_roadmap,
)

# ---------------------------------------------------------------------------
# parse_spec — happy path
# ---------------------------------------------------------------------------


def test_parse_spec_simple():
    spec = """\
## Tier 0

- id: install-runbook
  title: install runbook
  subtitle: zero-to-first-tick under 5 min

- id: rollback-runbook
  title: rollback runbook
"""
    items = parse_spec(spec)
    assert len(items) == 2
    assert items[0].slug == "install-runbook"
    assert items[0].tier == 0
    assert items[0].title == "install runbook"
    assert items[0].subtitle == "zero-to-first-tick under 5 min"
    assert items[1].slug == "rollback-runbook"
    assert items[1].subtitle == ""


def test_parse_spec_multiple_tiers():
    spec = """\
## Tier 0

- id: blocker
  title: a blocker

## Tier 1

- id: polish
  title: a polish item

## Tier 2

- id: cleanup
  title: a cleanup
"""
    items = parse_spec(spec)
    assert [it.tier for it in items] == [0, 1, 2]


def test_parse_spec_ignores_prose_between_items():
    spec = """\
## Tier 0 — must ship before anything else

Three lines of human prose.
The ingester ignores prose between Tier headings and items.

- id: foo
  title: foo task
"""
    items = parse_spec(spec)
    assert len(items) == 1
    assert items[0].slug == "foo"


def test_parse_spec_handles_blank_subtitle():
    spec = """\
## Tier 0

- id: bare
  title: bare item
"""
    items = parse_spec(spec)
    assert items[0].subtitle == ""


# ---------------------------------------------------------------------------
# parse_spec — error paths
# ---------------------------------------------------------------------------


def test_parse_spec_rejects_item_before_tier():
    spec = """\
- id: orphan
  title: oops
"""
    with pytest.raises(SpecIngestError, match="appears before any"):
        parse_spec(spec)


def test_parse_spec_rejects_missing_title():
    spec = """\
## Tier 0

- id: notitled
  subtitle: only a subtitle
"""
    with pytest.raises(SpecIngestError, match="missing required 'title:'"):
        parse_spec(spec)


def test_parse_spec_rejects_unsafe_slug():
    spec = """\
## Tier 0

- id: Has Spaces
  title: bad slug
"""
    with pytest.raises(SpecIngestError, match="kebab-case"):
        parse_spec(spec)


def test_parse_spec_rejects_uppercase_slug():
    spec = """\
## Tier 0

- id: HasCaps
  title: bad slug
"""
    with pytest.raises(SpecIngestError, match="kebab-case"):
        parse_spec(spec)


def test_parse_spec_rejects_empty_input():
    with pytest.raises(SpecIngestError, match="no items found"):
        parse_spec("")


def test_parse_spec_rejects_only_prose():
    spec = "Just some prose. No items.\n## Section heading\nMore prose.\n"
    with pytest.raises(SpecIngestError, match="no items found"):
        parse_spec(spec)


# ---------------------------------------------------------------------------
# Forbidden strings
# ---------------------------------------------------------------------------


def test_parse_spec_rejects_forbidden_in_title():
    spec = """\
## Tier 0

- id: leak
  title: port from psos
"""
    with pytest.raises(SpecIngestError, match="forbidden string"):
        parse_spec(spec)


def test_parse_spec_rejects_forbidden_in_subtitle():
    spec = """\
## Tier 0

- id: leak
  title: a clean title
  subtitle: based on /opt/psos patterns
"""
    with pytest.raises(SpecIngestError, match="forbidden string"):
        parse_spec(spec)


def test_parse_spec_rejects_forbidden_in_slug():
    # Slug shape regex would reject most forbidden strings (uppercase,
    # paths with /), so this test uses a slug that's both kebab-case
    # *and* contains a forbidden substring.
    spec = """\
## Tier 0

- id: psos-port
  title: a totally clean title
"""
    with pytest.raises(SpecIngestError, match="forbidden string"):
        parse_spec(spec)


def test_parse_spec_custom_forbidden_list():
    """Operators can extend the forbidden list with their own."""
    spec = """\
## Tier 0

- id: secret
  title: do not mention SECRET
"""
    custom = (*DEFAULT_FORBIDDEN_STRINGS, "secret")
    with pytest.raises(SpecIngestError, match="forbidden string"):
        parse_spec(spec, forbidden=custom)


# ---------------------------------------------------------------------------
# render_roadmap
# ---------------------------------------------------------------------------


def test_render_roadmap_basic():
    items = [
        SpecItem(tier=0, slug="a", title="first task", subtitle="first sub"),
        SpecItem(tier=0, slug="b", title="second task"),
        SpecItem(tier=1, slug="c", title="tier1 task"),
    ]
    output = render_roadmap(items)
    # Identifiers assigned in order
    assert "**T0-1 · first task**" in output
    assert "_first sub_" in output
    assert "**T0-2 · second task**" in output
    assert "**T1-1 · tier1 task**" in output


def test_render_roadmap_no_subtitle_omits_italic_line():
    items = [SpecItem(tier=0, slug="a", title="just a title")]
    output = render_roadmap(items)
    assert "**T0-1 · just a title**" in output
    # No italic line should appear under the bare title.
    lines = output.splitlines()
    title_idx = next(i for i, line in enumerate(lines) if "T0-1" in line)
    if title_idx + 1 < len(lines):
        next_line = lines[title_idx + 1]
        assert not next_line.startswith("_") or next_line == ""


def test_render_roadmap_starts_each_tier_with_heading():
    items = [
        SpecItem(tier=0, slug="a", title="t0"),
        SpecItem(tier=2, slug="b", title="t2"),
    ]
    output = render_roadmap(items)
    assert "## Tier 0" in output
    assert "## Tier 2" in output


def test_render_roadmap_deterministic():
    items = [
        SpecItem(tier=0, slug="a", title="x"),
        SpecItem(tier=0, slug="b", title="y"),
    ]
    out1 = render_roadmap(items)
    out2 = render_roadmap(items)
    assert out1 == out2


def test_render_roadmap_with_header():
    items = [SpecItem(tier=0, slug="a", title="x")]
    output = render_roadmap(items, header="# My Project Roadmap\n\nSome intro.")
    assert output.startswith("# My Project Roadmap")
    assert "## Tier 0" in output


def test_render_roadmap_preserves_existing_identifiers():
    """When merging into an existing roadmap, don't renumber existing items."""
    existing = """\
## Tier 0

**T0-1 · existing**
_already there_

**T0-3 · gap_skipped**
_T0-2 was deleted earlier_
"""
    new_items = [SpecItem(tier=0, slug="newone", title="new item")]
    output = render_roadmap(new_items, existing_roadmap=existing)
    # New item should claim T0-2 (the gap), not T0-1 (in use) or T0-3 (in use)
    assert "**T0-2 · new item**" in output


def test_render_roadmap_existing_full_no_gaps():
    existing = """\
## Tier 0

**T0-1 · a**
**T0-2 · b**
"""
    new_items = [SpecItem(tier=0, slug="c", title="c")]
    output = render_roadmap(new_items, existing_roadmap=existing)
    assert "**T0-3 · c**" in output


# ---------------------------------------------------------------------------
# ingest (high-level)
# ---------------------------------------------------------------------------


def test_ingest_returns_report():
    spec = """\
## Tier 0

- id: foo
  title: the foo
  subtitle: foo subtitle
"""
    report = ingest(spec)
    assert len(report.items) == 1
    assert "**T0-1 · the foo**" in report.output
    assert "_foo subtitle_" in report.output


def test_ingest_output_is_planner_compatible():
    """The rendered roadmap must be parseable by the planner."""
    from oxi_core.planner import parse_roadmap

    spec = """\
## Tier 0

- id: a
  title: alpha task
  subtitle: alpha sub

- id: b
  title: beta task

## Tier 1

- id: c
  title: charlie task
"""
    report = ingest(spec)
    items = parse_roadmap(report.output)
    assert len(items) == 3
    identifiers = [it.identifier for it in items]
    assert "T0-1" in identifiers
    assert "T0-2" in identifiers
    assert "T1-1" in identifiers


# ---------------------------------------------------------------------------
# ingest_to_file
# ---------------------------------------------------------------------------


def test_ingest_to_file_writes_output(tmp_path: Path):
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(
        "## Tier 0\n\n- id: foo\n  title: the foo\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "roadmap.md"
    report = ingest_to_file(spec_path, out_path)
    assert out_path.exists()
    assert "**T0-1 · the foo**" in out_path.read_text()
    assert "**T0-1 · the foo**" in report.output


def test_ingest_to_file_refuses_to_overwrite(tmp_path: Path):
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(
        "## Tier 0\n\n- id: foo\n  title: the foo\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "roadmap.md"
    out_path.write_text("# pre-existing content\n", encoding="utf-8")
    with pytest.raises(SpecIngestError, match="already exists"):
        ingest_to_file(spec_path, out_path)
    # Original content untouched
    assert out_path.read_text() == "# pre-existing content\n"


def test_ingest_to_file_with_force_preserves_existing_ids(tmp_path: Path):
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(
        "## Tier 0\n\n- id: newone\n  title: newcomer\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "roadmap.md"
    out_path.write_text(
        "## Tier 0\n\n**T0-1 · existing thing**\n_old_\n",
        encoding="utf-8",
    )
    ingest_to_file(spec_path, out_path, force=True)
    new_content = out_path.read_text()
    assert "**T0-2 · newcomer**" in new_content


def test_ingest_to_file_missing_spec_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        ingest_to_file(
            tmp_path / "no-such-spec.md",
            tmp_path / "roadmap.md",
        )
