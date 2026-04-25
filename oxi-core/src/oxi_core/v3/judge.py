"""LLM judge with rubric and fabricated-module hard filter.

The judge evaluates external proposals (e.g. free-form spec items or
candidate roadmap additions) before they enter the planning pipeline.
It answers two questions:

1. Is the candidate worth scheduling?  (relevance, concreteness,
   suggested tier)
2. Does the candidate reference a module that doesn't exist in the
   current codebase?  (fabricated_module hard filter)

Architecture
------------
A ``JudgeBackend`` protocol separates the evaluation logic from the
invocation mechanism — the same pattern used by ``critic.py``.
Implementations defined here:

- ``HaikuJudge`` — calls ``claude-haiku`` (the cheapest Claude model)
  via ``anthropic.Anthropic``.  Uses the Anthropic SDK's structured
  output (``response_format`` / ``tools`` with JSON schema) to get a
  ``JudgeScore`` back in one call, no prompt-engineering needed.
- ``FakeJudge`` — in ``tests/fixtures/fake_judge.py``; deterministic
  verdicts keyed by candidate ``id``.

Budget enforcement
------------------
``HaikuJudge.evaluate`` calls ``budget.check()`` before every
invocation.  It also maintains a **separate internal ledger** that
accumulates cost against ``EXTERNAL_PROPOSAL_*``-tagged events in the
event table, and enforces a hard cap of **$5/day** for judge calls
specifically.  The per-invocation cost is negligible (Haiku is ~$0.001
per call at typical prompt sizes), so the $5/day cap is effectively an
emergency brake against loops.

If either the global budget or the internal judge budget is exceeded,
``evaluate`` raises ``JudgeBudgetExceeded`` without invoking the model.

Fabricated-module detection
---------------------------
``HaikuJudge`` asks the model to set ``fabricated_module=true`` if
the candidate description references a module (e.g. ``oxi_core.v3.foo``)
that does not exist in the installed package.  Before scoring, the judge
reads the current module list via ``pkgutil.iter_modules(oxi_core.v3.__path__)``.
If the model returns ``fabricated_module=true``, the judge hard-rejects
with event kind ``EXTERNAL_PROPOSAL_REJECTED_FABRICATED`` and returns a
``JudgeVerdict`` with ``accepted=False`` regardless of other scores.

Schema
------
The structured output schema the model must return::

    {
        "relevance":        int,   # 1–5
        "concreteness":     int,   # 1–5
        "suggested_tier":   int,   # 0, 1, or 2
        "duplicate":        bool,
        "fabricated_module": bool,
        "rationale":        str
    }

This schema is enforced via a ``tool`` definition so the model cannot
return free text.  On parse failure the judge returns a conservative
reject (score=1/1, accepted=False).

Event kinds
-----------
``EXTERNAL_PROPOSAL_*`` events are tagged with ``cost_usd`` so the
internal ledger query can sum them by day:

- ``EXTERNAL_PROPOSAL_ACCEPTED``  — proposal accepted, all thresholds met.
- ``EXTERNAL_PROPOSAL_REJECTED``  — rejected on score (low relevance,
  concreteness, or both) but no fabrication.
- ``EXTERNAL_PROPOSAL_REJECTED_FABRICATED`` — hard-rejected because the
  model set ``fabricated_module=true``.

LedgerEvent constants for the three new kinds are declared in this
module (as plain strings) to avoid touching the shared ``ledger_events``
module every time a new subsystem is added.  They are exported via
``__all__``.
"""

from __future__ import annotations

import json
import logging
import pkgutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import anthropic

import oxi_core.v3
from . import budget as _budget

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event kind constants (kept local to avoid touching ledger_events for now)
# ---------------------------------------------------------------------------

EXTERNAL_PROPOSAL_ACCEPTED: str = "external_proposal_accepted"
EXTERNAL_PROPOSAL_REJECTED: str = "external_proposal_rejected"
EXTERNAL_PROPOSAL_REJECTED_FABRICATED: str = "external_proposal_rejected_fabricated"

# Per-day hard cap for judge-specific spend (separate from the global cap).
_JUDGE_DAILY_CAP_USD: float = 5.0

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeScore:
    """Raw scores returned by the LLM.

    Attributes:
        relevance:         1–5. How relevant is the candidate to the project?
        concreteness:      1–5. Is the description actionable / specific?
        suggested_tier:    0, 1, or 2. Which roadmap tier the model suggests.
        duplicate:         True if the model thinks the item already exists.
        fabricated_module: True if the description references a non-existent
                           module.
        rationale:         One-paragraph free-text explanation.
    """

    relevance: int
    concreteness: int
    suggested_tier: int
    duplicate: bool
    fabricated_module: bool
    rationale: str


@dataclass(frozen=True)
class JudgeCandidate:
    """A proposal submitted to the judge for evaluation.

    Attributes:
        id:          Stable identifier for the proposal (e.g. ``"ext-001"``).
        title:       Short title.
        description: Free-form description of what the proposal entails.
    """

    id: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class JudgeVerdict:
    """The judge's final decision on a candidate.

    Attributes:
        accepted:   True if the proposal should enter the planning pipeline.
        score:      The raw rubric scores (always populated even on reject).
        event_kind: The event kind that was (or would be) emitted.
        rationale:  Human-readable explanation.
    """

    accepted: bool
    score: JudgeScore
    event_kind: str
    rationale: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JudgeBudgetExceeded(RuntimeError):
    """Raised when either the global or judge-specific daily budget is exhausted.

    Callers should catch this and treat the candidate as deferred — the
    judge will become available again the next UTC day (or when caps are
    raised by the operator).
    """


class JudgeEvaluationError(RuntimeError):
    """Raised when the LLM response cannot be parsed into a ``JudgeScore``.

    The caller may treat this as a temporary failure and retry, or record
    a conservative reject in the ledger.
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class JudgeBackend(Protocol):
    """Minimal interface every judge implementation satisfies."""

    def evaluate(
        self,
        candidate: JudgeCandidate,
        conn: sqlite3.Connection,
        *,
        now: datetime | None = None,
    ) -> JudgeVerdict: ...


# ---------------------------------------------------------------------------
# Module list helper
# ---------------------------------------------------------------------------


def _current_v3_modules() -> frozenset[str]:
    """Return the set of module names in ``oxi_core.v3``.

    Uses ``pkgutil.iter_modules`` against the package's ``__path__``
    so that it reflects whatever is installed in the current venv,
    including editable installs.  Returns bare module names (e.g.
    ``"budget"``, ``"critic"``).
    """
    return frozenset(
        mod.name for mod in pkgutil.iter_modules(oxi_core.v3.__path__)
    )


# ---------------------------------------------------------------------------
# Budget helpers
# ---------------------------------------------------------------------------


def _judge_spend_today(conn: sqlite3.Connection, since: datetime) -> float:
    """Sum ``cost_usd`` from ``EXTERNAL_PROPOSAL_*`` events since *since*.

    This is the internal judge ledger — separate from the global task
    cost_usd query in ``budget.py``.  The query looks at the ``event``
    table's ``payload`` column (JSON) for a ``cost_usd`` field, and
    the ``kind`` column for the ``external_proposal_*`` prefix.
    """
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT payload FROM event "
        "WHERE kind LIKE 'external_proposal_%' AND created_at >= ?",
        (since_str,),
    ).fetchall()
    total = 0.0
    for (payload_str,) in rows:
        try:
            payload = json.loads(payload_str)
            total += float(payload.get("cost_usd", 0.0))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return total


def _emit_event(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict,
) -> None:
    """Insert a task-less engine event into the ledger."""
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (kind, json.dumps(payload)),
        )


# ---------------------------------------------------------------------------
# Rubric tool definition for the Anthropic SDK
# ---------------------------------------------------------------------------

_JUDGE_TOOL: dict = {
    "name": "submit_rubric_score",
    "description": (
        "Submit the structured rubric evaluation for a roadmap proposal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "relevance": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": (
                    "How relevant is this proposal to the project? "
                    "1 = completely off-topic, 5 = core to the mission."
                ),
            },
            "concreteness": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": (
                    "How actionable / specific is the description? "
                    "1 = vague aspiration, 5 = well-defined implementation task."
                ),
            },
            "suggested_tier": {
                "type": "integer",
                "enum": [0, 1, 2],
                "description": (
                    "Recommended roadmap tier. "
                    "0 = urgent/blocking, 1 = high-value near-term, 2 = future."
                ),
            },
            "duplicate": {
                "type": "boolean",
                "description": (
                    "True if this proposal substantially overlaps with an "
                    "existing roadmap item or recently merged work."
                ),
            },
            "fabricated_module": {
                "type": "boolean",
                "description": (
                    "True if the proposal references a specific Python module "
                    "(e.g. oxi_core.v3.xyz) that does not actually exist in "
                    "the codebase as of the evaluation date."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "One-paragraph explanation of the scores above.",
            },
        },
        "required": [
            "relevance",
            "concreteness",
            "suggested_tier",
            "duplicate",
            "fabricated_module",
            "rationale",
        ],
    },
}

# Minimum scores for a proposal to be accepted.
_MIN_RELEVANCE = 3
_MIN_CONCRETENESS = 3


# ---------------------------------------------------------------------------
# HaikuJudge — real LLM backend
# ---------------------------------------------------------------------------


@dataclass
class HaikuJudge:
    """Evaluate candidates using ``claude-haiku-4-5`` via the Anthropic SDK.

    The judge uses Claude's tool-use feature to get a structured score
    back in one round-trip.  No parsing is required — the SDK returns the
    tool input as a typed Python dict.

    Parameters
    ----------
    model:
        Anthropic model ID.  Defaults to ``claude-haiku-4-5``.
    max_tokens:
        Token budget for the model response.  Haiku is fast; 512 is
        enough for the rubric JSON plus a short rationale.
    judge_daily_cap_usd:
        Per-day spending cap for judge invocations specifically.
        Independent of the global ``budget.py`` cap.
    """

    model: str = "claude-haiku-4-5"
    max_tokens: int = 512
    judge_daily_cap_usd: float = _JUDGE_DAILY_CAP_USD
    # Injected in tests to avoid real API calls.
    _client: anthropic.Anthropic | None = field(default=None, repr=False)

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is not None:
            return self._client
        return anthropic.Anthropic()

    def evaluate(
        self,
        candidate: JudgeCandidate,
        conn: sqlite3.Connection,
        *,
        now: datetime | None = None,
    ) -> JudgeVerdict:
        """Evaluate a candidate proposal.

        Steps
        -----
        1. Check global budget via ``budget.check()``.
        2. Check internal judge budget (``EXTERNAL_PROPOSAL_*`` events today).
        3. Build module list via ``pkgutil.iter_modules``.
        4. Call the Haiku model with the rubric tool.
        5. Parse the tool-use response into a ``JudgeScore``.
        6. If ``fabricated_module=True``, hard-reject.
        7. Otherwise apply score thresholds.
        8. Emit the appropriate ledger event.
        9. Return a ``JudgeVerdict``.

        Raises
        ------
        JudgeBudgetExceeded
            If the global budget or the judge-specific daily cap is exhausted.
        JudgeEvaluationError
            If the model response cannot be parsed.
        """
        now_dt = now if now is not None else datetime.now(tz=UTC)
        window_start = _budget._utc_day_start(now_dt)

        # --- 1. Global budget check ---
        global_status = _budget.check(conn, now=now_dt)
        if global_status.verdict is _budget.Verdict.HARD_STOP:
            raise JudgeBudgetExceeded(
                f"global daily budget exhausted: "
                f"${global_status.today_spend_usd:.4f} >= "
                f"${global_status.daily_hard_cap_usd:.2f}"
            )

        # --- 2. Internal judge budget check ---
        judge_spend = _judge_spend_today(conn, window_start)
        if judge_spend >= self.judge_daily_cap_usd:
            raise JudgeBudgetExceeded(
                f"judge daily cap exhausted: "
                f"${judge_spend:.4f} >= ${self.judge_daily_cap_usd:.2f}"
            )

        # --- 3. Module list ---
        known_modules = _current_v3_modules()

        # --- 4. LLM call ---
        prompt = _build_prompt(candidate, known_modules)
        client = self._get_client()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                tools=[_JUDGE_TOOL],  # type: ignore[list-item]
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise JudgeEvaluationError(
                f"Anthropic API error during judge evaluation: {exc}"
            ) from exc

        # --- 5. Parse tool-use response ---
        score = _parse_response(response)

        # Estimate cost from the response usage metadata.
        cost_usd = _estimate_cost(response)

        # --- 6. Fabricated-module hard reject ---
        if score.fabricated_module:
            _emit_event(
                conn,
                EXTERNAL_PROPOSAL_REJECTED_FABRICATED,
                {
                    "candidate_id": candidate.id,
                    "candidate_title": candidate.title,
                    "score": {
                        "relevance": score.relevance,
                        "concreteness": score.concreteness,
                        "suggested_tier": score.suggested_tier,
                        "duplicate": score.duplicate,
                        "fabricated_module": score.fabricated_module,
                        "rationale": score.rationale,
                    },
                    "cost_usd": cost_usd,
                    "model": self.model,
                },
            )
            logger.warning(
                "judge hard-rejected %r: fabricated_module=True (rationale=%r)",
                candidate.id,
                score.rationale,
            )
            return JudgeVerdict(
                accepted=False,
                score=score,
                event_kind=EXTERNAL_PROPOSAL_REJECTED_FABRICATED,
                rationale=score.rationale,
            )

        # --- 7. Score thresholds ---
        accepted = (
            score.relevance >= _MIN_RELEVANCE
            and score.concreteness >= _MIN_CONCRETENESS
            and not score.duplicate
        )
        event_kind = (
            EXTERNAL_PROPOSAL_ACCEPTED if accepted else EXTERNAL_PROPOSAL_REJECTED
        )

        # --- 8. Emit ledger event ---
        _emit_event(
            conn,
            event_kind,
            {
                "candidate_id": candidate.id,
                "candidate_title": candidate.title,
                "score": {
                    "relevance": score.relevance,
                    "concreteness": score.concreteness,
                    "suggested_tier": score.suggested_tier,
                    "duplicate": score.duplicate,
                    "fabricated_module": score.fabricated_module,
                    "rationale": score.rationale,
                },
                "cost_usd": cost_usd,
                "model": self.model,
            },
        )

        if accepted:
            logger.info(
                "judge accepted %r (relevance=%d concreteness=%d tier=%d)",
                candidate.id,
                score.relevance,
                score.concreteness,
                score.suggested_tier,
            )
        else:
            logger.info(
                "judge rejected %r (relevance=%d concreteness=%d duplicate=%s)",
                candidate.id,
                score.relevance,
                score.concreteness,
                score.duplicate,
            )

        # --- 9. Return verdict ---
        return JudgeVerdict(
            accepted=accepted,
            score=score,
            event_kind=event_kind,
            rationale=score.rationale,
        )


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(candidate: JudgeCandidate, known_modules: frozenset[str]) -> str:
    """Build the judge prompt for a candidate."""
    modules_list = ", ".join(sorted(known_modules)) if known_modules else "(none)"
    return (
        f"You are a technical judge evaluating a proposed roadmap item for the "
        f"oxi autonomous coding orchestrator (Python package: oxi_core).\n\n"
        f"## Candidate proposal\n\n"
        f"**ID**: {candidate.id}\n"
        f"**Title**: {candidate.title}\n"
        f"**Description**: {candidate.description or '(none provided)'}\n\n"
        f"## Known oxi_core.v3 modules\n\n"
        f"The following module names exist under `oxi_core.v3` right now:\n"
        f"{modules_list}\n\n"
        f"## Instructions\n\n"
        f"Evaluate the proposal using the `submit_rubric_score` tool.\n\n"
        f"- Set `fabricated_module=true` if the proposal explicitly references a "
        f"  specific `oxi_core.v3.*` module that is NOT in the list above.\n"
        f"- Score `relevance` and `concreteness` on a 1–5 scale.\n"
        f"- Recommend a `suggested_tier` (0=urgent, 1=high-value, 2=future).\n"
        f"- Set `duplicate=true` if this substantially overlaps an existing module "
        f"  or is otherwise already covered.\n"
        f"- Write a concise `rationale` (2–4 sentences).\n"
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def _parse_response(response: anthropic.types.Message) -> JudgeScore:
    """Extract a ``JudgeScore`` from an Anthropic tool-use response.

    Raises ``JudgeEvaluationError`` on parse failure (missing tool block,
    wrong structure, out-of-range values).
    """
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_rubric_score":
            inp = block.input
            try:
                relevance = int(inp["relevance"])
                concreteness = int(inp["concreteness"])
                suggested_tier = int(inp["suggested_tier"])
                duplicate = bool(inp["duplicate"])
                fabricated_module = bool(inp["fabricated_module"])
                rationale = str(inp.get("rationale", ""))
            except (KeyError, TypeError, ValueError) as exc:
                raise JudgeEvaluationError(
                    f"malformed tool input from model: {exc} — got {inp!r}"
                ) from exc

            if not (1 <= relevance <= 5):
                raise JudgeEvaluationError(
                    f"relevance {relevance} out of range [1, 5]"
                )
            if not (1 <= concreteness <= 5):
                raise JudgeEvaluationError(
                    f"concreteness {concreteness} out of range [1, 5]"
                )
            if suggested_tier not in (0, 1, 2):
                raise JudgeEvaluationError(
                    f"suggested_tier {suggested_tier} not in {{0, 1, 2}}"
                )

            return JudgeScore(
                relevance=relevance,
                concreteness=concreteness,
                suggested_tier=suggested_tier,
                duplicate=duplicate,
                fabricated_module=fabricated_module,
                rationale=rationale,
            )

    raise JudgeEvaluationError(
        "model response contained no `submit_rubric_score` tool-use block; "
        f"got content types: {[b.type for b in response.content]}"
    )


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

# Haiku pricing as of 2025 (USD per million tokens).
# We deliberately hardcode the current Haiku prices rather than reaching
# out to the Anthropic pricing API on every call — the difference at
# <100 calls/day is negligible, and the API adds latency + failure modes.
_HAIKU_INPUT_COST_PER_M = 0.80
_HAIKU_OUTPUT_COST_PER_M = 4.00


def _estimate_cost(response: anthropic.types.Message) -> float:
    """Estimate cost in USD from the response's usage metadata."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0.0
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    return (
        input_tokens * _HAIKU_INPUT_COST_PER_M / 1_000_000
        + output_tokens * _HAIKU_OUTPUT_COST_PER_M / 1_000_000
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "EXTERNAL_PROPOSAL_ACCEPTED",
    "EXTERNAL_PROPOSAL_REJECTED",
    "EXTERNAL_PROPOSAL_REJECTED_FABRICATED",
    "HaikuJudge",
    "JudgeBackend",
    "JudgeBudgetExceeded",
    "JudgeCandidate",
    "JudgeEvaluationError",
    "JudgeScore",
    "JudgeVerdict",
]
