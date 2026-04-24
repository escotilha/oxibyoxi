"""Tests for oxi_core.v3.cto_verdict."""

from __future__ import annotations

import pytest

from oxi_core.v3.cto_verdict import (
    Concern,
    CtoVerdict,
    Severity,
    Verdict,
    parse,
)

# ---------------------------------------------------------------------------
# Severity.from_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,expected", [
    ("", Severity.NONE),
    ("none", Severity.NONE),
    ("N/A", Severity.NONE),
    ("INFO", Severity.INFO),
    ("informational", Severity.INFO),
    ("Low", Severity.LOW),
    ("medium", Severity.MEDIUM),
    ("med", Severity.MEDIUM),
    ("high", Severity.HIGH),
    ("hi", Severity.HIGH),
    ("CRITICAL", Severity.CRITICAL),
    ("crit", Severity.CRITICAL),
    ("blocker", Severity.CRITICAL),
    ("[high]", Severity.HIGH),
    ("  HIGH:  ", Severity.HIGH),
    ("unknown-label", Severity.NONE),
])
def test_severity_from_label(label, expected):
    assert Severity.from_label(label) is expected


def test_severity_is_ordered():
    """Severity values are comparable and in ascending order."""
    ordered = [
        Severity.NONE, Severity.INFO, Severity.LOW,
        Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL,
    ]
    values = [s.value for s in ordered]
    assert values == sorted(values)


# ---------------------------------------------------------------------------
# parse — empty / missing verdict
# ---------------------------------------------------------------------------


def test_empty_report_aborts():
    result = parse("")
    assert result.verdict is Verdict.ABORT
    assert result.reason == "empty report"


def test_whitespace_only_aborts():
    result = parse("   \n\n\t\n")
    assert result.verdict is Verdict.ABORT
    assert result.reason == "empty report"


def test_report_without_verdict_aborts():
    """Fail-closed: no verdict line → ABORT."""
    report = (
        "# CTO review\n\n"
        "Some analysis here.\n\n"
        "- [high] something bad\n"
    )
    result = parse(report)
    assert result.verdict is Verdict.ABORT
    assert result.reason == "no verdict line"
    # But concerns still get extracted so the caller sees them.
    assert len(result.concerns) == 1


# ---------------------------------------------------------------------------
# parse — verdict recognition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("report,expected", [
    ("**Verdict: proceed**", Verdict.PROCEED),
    ("## VERDICT: abort", Verdict.ABORT),
    ("Final verdict — PROCEED", Verdict.PROCEED),
    ("verdict: go", Verdict.PROCEED),
    ("Verdict: ship", Verdict.PROCEED),
    ("**Verdict — hold**", Verdict.ABORT),
    ("verdict: no-go", Verdict.ABORT),
])
def test_verdict_recognition(report, expected):
    result = parse(report)
    assert result.verdict is expected


def test_verdict_is_case_insensitive():
    assert parse("VERDICT: PROCEED").verdict is Verdict.PROCEED
    assert parse("verdict: Abort").verdict is Verdict.ABORT


# ---------------------------------------------------------------------------
# parse — concerns extraction
# ---------------------------------------------------------------------------


def test_concerns_extracted_with_bracket_severity():
    report = (
        "## Concerns\n"
        "- [HIGH] one\n"
        "- [MEDIUM] two\n"
        "- [LOW] three\n"
        "\n**Verdict: proceed**\n"
    )
    result = parse(report)
    assert [c.severity for c in result.concerns] == [
        Severity.HIGH, Severity.MEDIUM, Severity.LOW,
    ]
    assert [c.text for c in result.concerns] == ["one", "two", "three"]


def test_concerns_extracted_with_parens():
    report = (
        "- (critical) a security issue\n"
        "- (info) a note\n"
        "\nVerdict: abort\n"
    )
    result = parse(report)
    assert [c.severity for c in result.concerns] == [
        Severity.CRITICAL, Severity.INFO,
    ]


def test_concerns_extracted_with_kv_form():
    report = (
        "- severity: high - something\n"
        "- severity: low - something else\n"
        "\nVerdict: proceed\n"
    )
    result = parse(report)
    assert len(result.concerns) == 2
    assert result.concerns[0].severity is Severity.HIGH


def test_numbered_list_also_parsed():
    report = (
        "1. [high] first\n"
        "2. [low] second\n"
        "\nVerdict: proceed\n"
    )
    result = parse(report)
    assert len(result.concerns) == 2


def test_lines_without_bullets_are_not_concerns():
    report = (
        "Some prose referencing [high] risk in a sentence.\n"
        "\n**Verdict: proceed**\n"
    )
    result = parse(report)
    assert result.concerns == ()


def test_unknown_severity_labels_become_none():
    report = (
        "- [magenta] strange severity\n"
        "\nVerdict: proceed\n"
    )
    result = parse(report)
    assert len(result.concerns) == 1
    assert result.concerns[0].severity is Severity.NONE


# ---------------------------------------------------------------------------
# parse — severity_max
# ---------------------------------------------------------------------------


def test_severity_max_picks_worst():
    report = (
        "- [low] a\n"
        "- [critical] b\n"
        "- [medium] c\n"
        "\nVerdict: abort\n"
    )
    result = parse(report)
    assert result.severity_max is Severity.CRITICAL


def test_severity_max_is_none_when_no_concerns():
    result = parse("**Verdict: proceed**")
    assert result.severity_max is Severity.NONE


# ---------------------------------------------------------------------------
# Parser does NOT override a PROCEED verdict when concerns contain CRITICAL
# ---------------------------------------------------------------------------


def test_parser_returns_skills_verdict_even_with_critical_concern():
    """The parser is faithful — downstream rules add policy."""
    report = (
        "- [critical] this is bad\n"
        "\n**Verdict: proceed**\n"
    )
    result = parse(report)
    assert result.verdict is Verdict.PROCEED
    assert result.severity_max is Severity.CRITICAL


# ---------------------------------------------------------------------------
# CtoVerdict + Concern dataclass
# ---------------------------------------------------------------------------


def test_verdict_dataclass_is_frozen():
    from dataclasses import FrozenInstanceError
    v = CtoVerdict(
        verdict=Verdict.PROCEED,
        severity_max=Severity.NONE,
        concerns=(),
        reason="parsed",
    )
    with pytest.raises(FrozenInstanceError):
        v.verdict = Verdict.ABORT  # type: ignore[misc]


def test_concern_dataclass_is_frozen():
    from dataclasses import FrozenInstanceError
    c = Concern(severity=Severity.HIGH, text="x")
    with pytest.raises(FrozenInstanceError):
        c.text = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Realistic CTO report
# ---------------------------------------------------------------------------


def test_realistic_report_parses():
    report = """
# CTO review — feat/sa-auth-middleware

## Summary
The PR refactors auth middleware. Scope is reasonable, but there are
concerns about session expiry handling.

## Concerns
- [HIGH] Session cookie missing `Secure` flag in production config.
- [MEDIUM] Refresh token rotation not tested.
- [LOW] Docstring typo in `validate_session`.
- (INFO) Unused import in `middleware/__init__.py`.

## Recommendation
Fix the HIGH item before merge. MEDIUM can follow-up.

**Final verdict — PROCEED**
"""
    result = parse(report)
    assert result.verdict is Verdict.PROCEED
    assert result.severity_max is Severity.HIGH
    assert len(result.concerns) == 4
    severities = [c.severity for c in result.concerns]
    assert Severity.HIGH in severities
    assert Severity.MEDIUM in severities
    assert Severity.LOW in severities
    assert Severity.INFO in severities
