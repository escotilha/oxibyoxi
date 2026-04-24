"""oxi v3 plan --dry-run — parse the roadmap and print what was found.

Does NOT touch the database. Intended as an operator-facing diagnostic:
when a fresh roadmap produces zero seeded tasks, running ``oxi v3 plan
--dry-run`` shows exactly what the parser *did* recognise, which makes
format mistakes (e.g. ``## T0-1 — title`` vs. the required ``## Tier 0``
/ ``**T0-1 · title**`` structure) immediately visible.

Public surface:

    dry_run(roadmap_path)  → DryRunReport
    format_report(report)  → str          (human-readable, goes to stdout)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..adapter import RoadmapItem
from ..planner import RoadmapParseError, parse_roadmap


@dataclass(frozen=True)
class DryRunReport:
    """Result of a roadmap dry-run parse.

    ``items`` contains every ``RoadmapItem`` the parser extracted, in
    roadmap order.  ``parse_error`` is non-None when the roadmap has a
    structural problem (e.g. an item before any ``## Tier N`` heading).
    """

    items: tuple[RoadmapItem, ...]
    parse_error: str | None = None

    @property
    def ok(self) -> bool:
        """True when parsing succeeded (no structural error)."""
        return self.parse_error is None


def dry_run(roadmap_path: Path) -> DryRunReport:
    """Parse *roadmap_path* and return a :class:`DryRunReport`.

    Never writes to the database.  Catches :class:`~oxi_core.planner.RoadmapParseError`
    and surfaces it via ``DryRunReport.parse_error`` so the caller can
    decide whether to exit non-zero.

    Raises :class:`FileNotFoundError` when the path does not exist —
    that is a configuration error, not a roadmap content error.
    """
    text = roadmap_path.read_text(encoding="utf-8")
    try:
        items = parse_roadmap(text)
    except RoadmapParseError as exc:
        return DryRunReport(items=(), parse_error=str(exc))

    return DryRunReport(items=tuple(items))


def format_report(report: DryRunReport, roadmap_path: Path) -> str:
    """Render *report* as a human-readable string for stdout.

    The format is stable enough to be useful in scripts but is not
    considered a machine-parseable API.  Use ``DryRunReport`` directly
    if you need programmatic access.
    """
    lines: list[str] = []
    lines.append(f"oxi plan --dry-run: {roadmap_path}")
    lines.append("")

    if not report.ok:
        lines.append(f"  ERROR: {report.parse_error}")
        lines.append("")
        lines.append("  No items could be parsed.  Fix the roadmap format and re-run.")
        lines.append("  Expected format:")
        lines.append("")
        lines.append("    ## Tier 0")
        lines.append("")
        lines.append("    **T0-1 · item title**")
        lines.append("    _optional subtitle_")
        return "\n".join(lines)

    count = len(report.items)
    lines.append(f"  found {count} item{'s' if count != 1 else ''}")
    lines.append("")

    if not report.items:
        lines.append("  (no items — check the roadmap format)")
        lines.append("")
        lines.append("  Expected format:")
        lines.append("")
        lines.append("    ## Tier 0")
        lines.append("")
        lines.append("    **T0-1 · item title**")
        lines.append("    _optional subtitle_")
        return "\n".join(lines)

    # Column header
    lines.append(f"  {'TIER':<6}  {'IDENTIFIER':<14}  {'TITLE'}")
    lines.append(f"  {'-'*6}  {'-'*14}  {'-'*40}")

    for item in report.items:
        subtitle_suffix = f"  ({item.subtitle})" if item.subtitle else ""
        lines.append(
            f"  {item.tier:<6}  {item.identifier:<14}  {item.title}{subtitle_suffix}"
        )

    lines.append("")
    lines.append("  DB not touched.  Run `oxi v3 tick` to seed tasks.")
    return "\n".join(lines)


__all__ = ["DryRunReport", "dry_run", "format_report"]
