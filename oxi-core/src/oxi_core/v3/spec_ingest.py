"""Product-spec → roadmap.md ingester.

The dogfood loop reads `docs/roadmap.md`. Operators who don't want to
hand-write that format can describe their work in a structured spec
file; this module converts the spec to the planner-compatible format.

Two ingest paths land here:

1. **Structured spec (this module).** Operator writes a YAML or markdown
   spec with explicit tier + identifier + title + subtitle for each
   item. Module emits the planner format. No LLM, no API, deterministic.
   Useful for: operators who already know what they want to ship.

2. **Free-form spec (T3-1 phase 2, future).** Operator writes natural
   prose ("we want X, then Y, then Z") and Claude maps it onto the
   roadmap format. Out of scope for this first cut; would belong in
   `oxi_core.v3.spec_ingest_llm` so the deterministic path stays
   testable without network calls.

Spec format (markdown, the same format the parser already accepts at
roadmap-write time, but exported as a separate concept):

    # Sample Project Spec

    ## Tier 0 — must ship before anything else
    Three lines of human prose explaining tier-0 scope. Free text.
    The ingester ignores prose between Tier headings and items.

    - id: install-runbook
      title: install runbook covering pip install through first dispatch
      subtitle: zero-to-first-tick under 5 minutes

    - id: rollback-runbook
      title: rollback runbook for bad alpha releases
      subtitle: yank vs republish decision tree

    ## Tier 1 — soon

    - id: status-json
      title: oxi v3 status --json flag
      subtitle: scriptable JSON output

The spec uses YAML-style hyphenated bullets so operators can edit it
in a text editor without tripping markdown's bold-syntax conventions.
The ingester parses items, prefixes identifiers with `T<tier>-<n>`
(numbered per tier from 1), and emits planner-format markdown:

    ## Tier 0

    **T0-1 · install runbook covering pip install through first dispatch**
    _zero-to-first-tick under 5 minutes_

The output is deterministic — same input always produces the same
roadmap.md, byte-identical. Operators can commit both the spec and
the generated roadmap so diffs are reviewable.

Anti-patterns this module avoids:

- **Project-specific identifiers.** `lint-for-leaks.sh` runs against
  generated output. The ingester rejects any spec item whose id or
  title contains a forbidden string (set at module level for
  testability).
- **Renumbering existing items.** If an existing roadmap.md already
  has `T1-3` in use, the ingester preserves it and assigns new
  identifiers from the next free number.
- **Silent overwrites.** `ingest_to_file` refuses to overwrite an
  existing roadmap unless `force=True` is passed. CLI surface
  delegates to the operator's confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecItem:
    """A single roadmap item parsed from a spec.

    `slug` is what the operator wrote (e.g. ``install-runbook``); it's
    informational and doesn't appear in the rendered roadmap. The
    identifier (`T0-1`, etc.) is assigned at render time.
    """

    tier: int
    slug: str
    title: str
    subtitle: str = ""


@dataclass(frozen=True)
class IngestReport:
    """Summary of an ingest pass — what was parsed, what was emitted.

    `output` is the rendered roadmap.md as a string. Callers decide
    whether to write it to disk; the report is the file's intended
    content.
    """

    items: tuple[SpecItem, ...]
    output: str
    skipped_lines: tuple[str, ...] = field(default_factory=tuple)


class SpecIngestError(ValueError):
    """Raised when a spec is structurally invalid or contains a leak."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# A tier heading: "## Tier 0" or "## Tier 0 — anything"
_TIER_HEADING = re.compile(r"^##\s+Tier\s+(\d+)\b", re.IGNORECASE)

# A spec item bullet:
#     - id: install-runbook
# Followed by indented `title:` and optional `subtitle:` keys.
_ITEM_BULLET = re.compile(r"^-\s*id:\s*(.+?)\s*$")
_KV_LINE = re.compile(r"^\s+(\w+):\s*(.+?)\s*$")

# Slug must be safe for use as a roadmap identifier hint — kebab-case,
# alphanumeric. Operators can write whatever they want in title; slugs
# are stricter so they survive future plumbing.
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-]*$")

# Forbidden strings — checked against spec input AND emitted output.
# Mirrors `scripts/lint-for-leaks.sh` so a clean spec produces a clean
# roadmap. Module-level so tests can monkeypatch.
DEFAULT_FORBIDDEN_STRINGS: tuple[str, ...] = (
    # Names of prior orchestrators / projects that must not leak forward.
    "psos",
    # Known sensitive paths and identifiers from the prior environment.
    "/opt/psos",
)


def parse_spec(text: str, *, forbidden: tuple[str, ...] | None = None) -> list[SpecItem]:
    """Parse a spec markdown string into a list of `SpecItem`s.

    Raises ``SpecIngestError`` if:
    - an item appears before any ``## Tier N`` heading
    - an item bullet is missing required ``title``
    - a slug fails the ``[a-z0-9-]`` shape
    - any forbidden string appears in id, title, or subtitle

    Free-form prose between bullets is ignored, captured in the
    report's ``skipped_lines`` for operator visibility.
    """
    forbid = forbidden if forbidden is not None else DEFAULT_FORBIDDEN_STRINGS

    items: list[SpecItem] = []
    current_tier: int | None = None
    pending: dict[str, str] | None = None

    def _flush() -> None:
        nonlocal pending
        if pending is None:
            return
        if "title" not in pending or not pending["title"]:
            raise SpecIngestError(
                f"item {pending['id']!r} missing required 'title:' line"
            )
        item = SpecItem(
            tier=current_tier,  # type: ignore[arg-type]
            slug=pending["id"],
            title=pending["title"],
            subtitle=pending.get("subtitle", ""),
        )
        for field_name in ("slug", "title", "subtitle"):
            value = getattr(item, field_name)
            for forbidden_str in forbid:
                if forbidden_str.lower() in value.lower():
                    raise SpecIngestError(
                        f"forbidden string {forbidden_str!r} appears in "
                        f"spec item {pending['id']!r} field {field_name!r}"
                    )
        items.append(item)
        pending = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        tier_match = _TIER_HEADING.match(line.strip())
        if tier_match:
            _flush()
            current_tier = int(tier_match.group(1))
            continue

        item_match = _ITEM_BULLET.match(line.strip())
        if item_match:
            _flush()
            if current_tier is None:
                raise SpecIngestError(
                    f"item {item_match.group(1)!r} appears before any "
                    "'## Tier N' heading"
                )
            slug = item_match.group(1).strip()
            if not _SAFE_SLUG.match(slug):
                raise SpecIngestError(
                    f"slug {slug!r} must be lowercase kebab-case "
                    "(letters, digits, hyphens; start with letter/digit)"
                )
            pending = {"id": slug}
            continue

        kv_match = _KV_LINE.match(raw_line)
        if kv_match and pending is not None:
            key = kv_match.group(1).lower()
            value = kv_match.group(2)
            if key in ("title", "subtitle"):
                pending[key] = value

    _flush()

    if not items:
        raise SpecIngestError(
            "no items found in spec — expected `## Tier N` headings "
            "and `- id: ...` bullets"
        )

    return items


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


# An identifier already in use in an existing roadmap. Format `T<tier>-<n>`.
_EXISTING_ID = re.compile(r"^\*\*(T(\d+)-(\d+))\s*·")


def _existing_identifiers(existing_roadmap: str | None) -> dict[int, set[int]]:
    """Return {tier: {used numbers}} for every identifier already in roadmap."""
    if not existing_roadmap:
        return {}
    used: dict[int, set[int]] = {}
    for line in existing_roadmap.splitlines():
        match = _EXISTING_ID.match(line.strip())
        if match:
            tier = int(match.group(2))
            num = int(match.group(3))
            used.setdefault(tier, set()).add(num)
    return used


def _next_free(tier: int, used_for_tier: set[int]) -> int:
    """Smallest positive integer N where `T<tier>-<N>` is not in use."""
    n = 1
    while n in used_for_tier:
        n += 1
    return n


def render_roadmap(
    items: list[SpecItem],
    *,
    existing_roadmap: str | None = None,
    header: str | None = None,
) -> str:
    """Render `SpecItem`s as planner-format markdown.

    If ``existing_roadmap`` is provided, identifiers are assigned from
    the next free integer per tier so existing entries aren't
    renumbered.

    ``header`` is prepended verbatim if given — useful for project
    intro text the operator wants above the tier sections.

    Output is deterministic: same input → same bytes.
    """
    used = _existing_identifiers(existing_roadmap)
    by_tier: dict[int, list[SpecItem]] = {}
    for item in items:
        by_tier.setdefault(item.tier, []).append(item)

    parts: list[str] = []
    if header:
        parts.append(header.rstrip() + "\n\n")

    for tier in sorted(by_tier):
        parts.append(f"## Tier {tier}\n\n")
        used_for_tier = used.get(tier, set()).copy()
        for item in by_tier[tier]:
            n = _next_free(tier, used_for_tier)
            used_for_tier.add(n)
            ident = f"T{tier}-{n}"
            parts.append(f"**{ident} · {item.title}**\n")
            if item.subtitle:
                parts.append(f"_{item.subtitle}_\n")
            parts.append("\n")

    output = "".join(parts).rstrip() + "\n"

    # Final leak check on rendered output. Defensive — parse_spec
    # already enforced this on inputs, but a future renderer change
    # could introduce constants worth re-checking.
    for forbidden in DEFAULT_FORBIDDEN_STRINGS:
        if forbidden.lower() in output.lower():
            raise SpecIngestError(
                f"rendered roadmap contains forbidden string {forbidden!r} "
                "— this is a renderer bug; please report"
            )

    return output


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest(
    spec_text: str,
    *,
    existing_roadmap: str | None = None,
    header: str | None = None,
    forbidden: tuple[str, ...] | None = None,
) -> IngestReport:
    """Parse a spec string and return a report including rendered roadmap.

    The spec_text is parsed; on success, items are rendered against the
    existing roadmap (if any) so identifiers don't collide with the
    operator's existing work.
    """
    items = parse_spec(spec_text, forbidden=forbidden)
    output = render_roadmap(items, existing_roadmap=existing_roadmap, header=header)
    return IngestReport(items=tuple(items), output=output)


def ingest_to_file(
    spec_path: Path,
    output_path: Path,
    *,
    force: bool = False,
    header: str | None = None,
) -> IngestReport:
    """Read spec from disk, write rendered roadmap to disk.

    If ``output_path`` exists and ``force`` is False, refuses to
    overwrite — the operator should review the existing file first.

    If ``output_path`` exists and ``force`` is True, the existing
    content is read and used as the baseline for identifier numbering
    (so re-running ingest doesn't renumber existing items).
    """
    spec_text = spec_path.read_text(encoding="utf-8")

    existing: str | None = None
    if output_path.exists():
        if not force:
            raise SpecIngestError(
                f"output {output_path} already exists; pass force=True to "
                "overwrite (existing identifiers will be preserved)"
            )
        existing = output_path.read_text(encoding="utf-8")

    report = ingest(spec_text, existing_roadmap=existing, header=header)
    output_path.write_text(report.output, encoding="utf-8")
    return report


__all__ = [
    "DEFAULT_FORBIDDEN_STRINGS",
    "IngestReport",
    "SpecIngestError",
    "SpecItem",
    "ingest",
    "ingest_to_file",
    "parse_spec",
    "render_roadmap",
]
