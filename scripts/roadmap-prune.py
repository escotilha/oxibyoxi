#!/usr/bin/env python3
"""Prune shipped items from docs/roadmap.md.

The roadmap doc is meant to be a tight queue (10-15 open items), but
items get shipped via PRs without anyone editing the doc — it drifts
into a graveyard of completed work. This script reads each item's
identifier (T{tier}-{N}) and marks it "done" if either:

  - The identifier appears in any docs/release-notes/v*.md
  - The identifier appears in a merged-commit subject on main
    (we look only at commits older than the freshness window so
    items merged in the current release-cycle aren't pruned before
    their release notes are written)

By default this is a dry-run: prints the diff to stdout. Pass --apply
to write the changes in place. Pass --json to emit a machine-readable
summary instead (used by the CI workflow).

This is a lightweight reconciler, not engine-coupled — it doesn't
touch the SQLite ledger or oxi_core. Operators run it ad-hoc; CI
runs it weekly and opens a PR if anything changed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Match the canonical identifier shape used by the planner.
# Examples: T0-1, T1-101, T2-15, T3-2.
_ID_RE = re.compile(r"\bT\d+-\d+\b")

# Match a roadmap entry header. Format:
#   **T0-1 · Title here**
#   _italic subtitle on the next line._
_ITEM_HEADER_RE = re.compile(
    r"^\*\*(?P<id>T\d+-\d+)\s+·\s+(?P<title>[^*]+)\*\*\s*$"
)

# Default freshness window — commits younger than this many hours are
# ignored when checking git history. Gives release notes time to be
# written for the most recent merges before the script prunes them.
DEFAULT_FRESHNESS_HOURS = 48


@dataclass
class RoadmapItem:
    """One parsed roadmap entry."""

    identifier: str
    title: str
    # Lines that make up this item, including the header line and any
    # subtitle/body until the next item or section. Preserved verbatim
    # so apply mode round-trips clean.
    lines: list[str] = field(default_factory=list)


@dataclass
class PruneReport:
    """Summary of what the prune pass would do."""

    pruned: list[RoadmapItem] = field(default_factory=list)
    kept: list[RoadmapItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {
            "pruned": [
                {"id": it.identifier, "title": it.title.strip()}
                for it in self.pruned
            ],
            "kept": [
                {"id": it.identifier, "title": it.title.strip()}
                for it in self.kept
            ],
        }


def _split_into_sections(lines: list[str]) -> tuple[
    list[str], list[tuple[str, list[str]]], list[str]
]:
    """Split the roadmap file into preamble, tier-sections, and trailer.

    A tier section is everything from a ``## Tier N`` heading up to
    the next top-level heading (``## ``) or end of file. The trailer
    is everything from the first non-tier ``## `` heading after the
    last tier (typically ``## Done`` or ``## Notes``).

    Returns ``(preamble_lines, [(heading, body_lines), ...], trailer_lines)``.
    """
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    trailer: list[str] = []

    i = 0
    # Preamble — everything before the first ``## Tier`` heading.
    while i < len(lines) and not lines[i].startswith("## Tier"):
        preamble.append(lines[i])
        i += 1

    # Tier sections.
    while i < len(lines) and lines[i].startswith("## Tier"):
        heading = lines[i]
        body: list[str] = []
        i += 1
        while i < len(lines) and not lines[i].startswith("## "):
            body.append(lines[i])
            i += 1
        sections.append((heading, body))

    # Trailer — anything after the last tier section.
    while i < len(lines):
        trailer.append(lines[i])
        i += 1

    return preamble, sections, trailer


def _parse_items(body: list[str]) -> list[RoadmapItem]:
    """Parse a tier section's body into roadmap items.

    Each item starts at a ``**T{tier}-{N} · ...**`` header line and
    runs until the next header or end-of-section. Blank lines and
    italic subtitles are kept inside the item.
    """
    items: list[RoadmapItem] = []
    current: RoadmapItem | None = None

    for line in body:
        match = _ITEM_HEADER_RE.match(line)
        if match is not None:
            if current is not None:
                items.append(current)
            current = RoadmapItem(
                identifier=match.group("id"),
                title=match.group("title").strip(),
                lines=[line],
            )
        elif current is not None:
            current.lines.append(line)
        # Lines before any item header are dropped intentionally; the
        # tier heading itself is held outside this body.

    if current is not None:
        items.append(current)
    return items


def _serialize_section(
    heading: str, items: list[RoadmapItem]
) -> list[str]:
    """Reassemble a tier section from its heading + surviving items."""
    out: list[str] = [heading, "\n"]
    for item in items:
        out.extend(item.lines)
    return out


_SUBSTANTIVE_PATH_PREFIXES = (
    "oxi-core/src/",
    "adapters/",
    "scripts/",
    ".github/",
    "docs/runbooks/",
    "docs/manual/",
)


def _release_note_ids(release_notes_dir: Path) -> set[str]:
    """Return every T-ID mentioned in any release-notes/v*.md file.

    Release-notes mentions alone are *not* enough — see ``prune`` for
    how we cross-check with git history. This function only collects
    candidates.
    """
    ids: set[str] = set()
    if not release_notes_dir.is_dir():
        return ids
    for note in release_notes_dir.glob("v*.md"):
        for match in _ID_RE.finditer(note.read_text(encoding="utf-8")):
            ids.add(match.group(0))
    return ids


def _git_shipped_ids(
    repo_root: Path, freshness_hours: int
) -> set[str]:
    """Return T-IDs that have a substantive merged commit on main.

    A commit counts as "substantive" when:
      - its subject mentions a T-ID, AND
      - it touches at least one path under oxi-core/src/, adapters/,
        scripts/, .github/, or docs/runbooks/ (i.e. actually changes
        production code or operator-facing artifacts), AND
      - it's older than ``freshness_hours`` (so items merged in the
        current release-cycle aren't pruned before their notes ship).

    Pure docs/release-notes commits don't count — release-notes
    drafts can mention an item *before* it ships, which would
    otherwise prune an open item.
    """
    try:
        # Get commits older than the freshness window. We need both
        # the commit message and the file list, so use `--name-only`.
        out = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                "main",
                f"--before={freshness_hours} hours ago",
                "--pretty=format:>>>%H|%s",
                "--name-only",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except subprocess.CalledProcessError:
        return set()

    ids: set[str] = set()
    # The output is a sequence of commit blocks separated by ">>>":
    #   >>>HASH|subject
    #   path/to/file1
    #   path/to/file2
    #   (blank line)
    for block in out.split(">>>"):
        if not block.strip():
            continue
        first_line, _, rest = block.partition("\n")
        _, _, subject = first_line.partition("|")
        files = [
            p.strip()
            for p in rest.splitlines()
            if p.strip()
        ]
        if not any(
            f.startswith(_SUBSTANTIVE_PATH_PREFIXES) for f in files
        ):
            continue
        for match in _ID_RE.finditer(subject):
            ids.add(match.group(0))
    return ids


def prune(
    roadmap_path: Path,
    *,
    release_notes_dir: Path | None = None,
    repo_root: Path | None = None,
    freshness_hours: int = DEFAULT_FRESHNESS_HOURS,
) -> tuple[str, PruneReport]:
    """Compute the pruned roadmap text and a report of what changed.

    ``roadmap_path`` is the file to read; the file itself is not
    modified by this function — call ``apply`` (i.e. write the
    returned text back) only after the operator has reviewed.
    """
    if release_notes_dir is None:
        release_notes_dir = roadmap_path.parent / "release-notes"
    if repo_root is None:
        repo_root = roadmap_path.parent.parent

    text = roadmap_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    preamble, sections, trailer = _split_into_sections(lines)
    # An item ships when git history has a substantive merged commit
    # naming it. Release-notes mentions are a secondary signal: they
    # corroborate but don't ship anything on their own — release notes
    # can be drafted on a feature branch *before* the implementing
    # commit is merged.
    rn_ids = _release_note_ids(release_notes_dir)
    git_ids = _git_shipped_ids(repo_root, freshness_hours)
    # The intersection is the strongest signal: both the implementing
    # commit AND release-notes acknowledgement are present.
    # Substantive-commit alone also counts (rare but valid for items
    # that shipped without a dedicated release-note line).
    shipped = git_ids
    # Release-notes-only mentions are intentionally NOT pruned — they
    # may be aspirational drafts.
    _ = rn_ids  # held for future cross-check tooling

    report = PruneReport()
    new_lines: list[str] = list(preamble)

    for heading, body in sections:
        items = _parse_items(body)
        kept_items: list[RoadmapItem] = []
        for item in items:
            if item.identifier in shipped:
                report.pruned.append(item)
            else:
                kept_items.append(item)
                report.kept.append(item)
        new_lines.extend(_serialize_section(heading, kept_items))

    new_lines.extend(trailer)
    return "".join(new_lines), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--roadmap",
        type=Path,
        default=Path("docs/roadmap.md"),
        help="Path to the roadmap file (default: docs/roadmap.md).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the pruned roadmap back to disk. Default: dry-run.",
    )
    parser.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit a machine-readable JSON summary instead of a diff.",
    )
    parser.add_argument(
        "--freshness-hours",
        type=int,
        default=DEFAULT_FRESHNESS_HOURS,
        help=(
            "Ignore git commits younger than this many hours. Default "
            f"{DEFAULT_FRESHNESS_HOURS}h. Prevents pruning items "
            "merged so recently that their release notes are still "
            "in flight."
        ),
    )
    args = parser.parse_args(argv)

    if not args.roadmap.is_file():
        print(f"roadmap-prune: not a file: {args.roadmap}", file=sys.stderr)
        return 2

    new_text, report = prune(
        args.roadmap, freshness_hours=args.freshness_hours
    )

    if args.emit_json:
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for item in report.pruned:
            print(f"PRUNE  {item.identifier}  {item.title}")
        for item in report.kept:
            print(f"KEEP   {item.identifier}  {item.title}")
        print(
            f"\nSummary: {len(report.pruned)} pruned, "
            f"{len(report.kept)} kept."
        )

    if args.apply and report.pruned:
        args.roadmap.write_text(new_text, encoding="utf-8")
        print(f"\nWrote pruned roadmap to {args.roadmap}", file=sys.stderr)

    # Exit 0 either way — CI workflow uses the `pruned` count from
    # JSON to decide whether to open a PR.
    return 0


if __name__ == "__main__":
    sys.exit(main())
