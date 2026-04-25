"""FakeJudge — deterministic judge stub for unit tests.

Lives under ``tests/fixtures/`` so it is test-only infrastructure.
``FakeJudge`` satisfies the ``JudgeBackend`` protocol and returns
pre-configured ``JudgeVerdict`` objects keyed by ``candidate.id``.

Usage
-----

.. code-block:: python

    from tests.fixtures.fake_judge import FakeJudge
    from oxi_core.v3.judge import JudgeCandidate, JudgeScore, JudgeVerdict, EXTERNAL_PROPOSAL_ACCEPTED

    judge = FakeJudge(
        verdicts={
            "ext-001": JudgeVerdict(
                accepted=True,
                score=JudgeScore(
                    relevance=5, concreteness=4, suggested_tier=1,
                    duplicate=False, fabricated_module=False,
                    rationale="great idea",
                ),
                event_kind=EXTERNAL_PROPOSAL_ACCEPTED,
                rationale="great idea",
            ),
        }
    )
    verdict = judge.evaluate(JudgeCandidate(id="ext-001", title="Do the thing"), conn)

Design
------
- Verdicts are keyed by ``candidate.id``.
- If a candidate ID has no configured verdict, ``FakeJudge`` returns
  a default accept verdict (relevance=3, concreteness=3, tier=1) so
  tests that don't care about the verdict stay brief.
- The fake does NOT call ``budget.check()`` or emit any ledger events.
  Tests that care about ledger behaviour should use a real ``HaikuJudge``
  with a mocked Anthropic client.
- ``evaluate`` records every call in ``self.calls`` so tests can assert
  on the call history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from oxi_core.v3.judge import (
    EXTERNAL_PROPOSAL_ACCEPTED,
    EXTERNAL_PROPOSAL_REJECTED,
    EXTERNAL_PROPOSAL_REJECTED_FABRICATED,
    JudgeCandidate,
    JudgeScore,
    JudgeVerdict,
)

# ---------------------------------------------------------------------------
# Default verdicts for convenience
# ---------------------------------------------------------------------------

_DEFAULT_ACCEPT_SCORE = JudgeScore(
    relevance=3,
    concreteness=3,
    suggested_tier=1,
    duplicate=False,
    fabricated_module=False,
    rationale="fake-judge default accept",
)

_DEFAULT_REJECT_SCORE = JudgeScore(
    relevance=1,
    concreteness=1,
    suggested_tier=2,
    duplicate=False,
    fabricated_module=False,
    rationale="fake-judge default reject",
)

_DEFAULT_FABRICATED_SCORE = JudgeScore(
    relevance=3,
    concreteness=3,
    suggested_tier=1,
    duplicate=False,
    fabricated_module=True,
    rationale="fake-judge fabricated module",
)

DEFAULT_ACCEPT = JudgeVerdict(
    accepted=True,
    score=_DEFAULT_ACCEPT_SCORE,
    event_kind=EXTERNAL_PROPOSAL_ACCEPTED,
    rationale="fake-judge default accept",
)

DEFAULT_REJECT = JudgeVerdict(
    accepted=False,
    score=_DEFAULT_REJECT_SCORE,
    event_kind=EXTERNAL_PROPOSAL_REJECTED,
    rationale="fake-judge default reject",
)

DEFAULT_FABRICATED = JudgeVerdict(
    accepted=False,
    score=_DEFAULT_FABRICATED_SCORE,
    event_kind=EXTERNAL_PROPOSAL_REJECTED_FABRICATED,
    rationale="fake-judge fabricated module",
)


# ---------------------------------------------------------------------------
# FakeJudge
# ---------------------------------------------------------------------------


@dataclass
class FakeJudge:
    """Deterministic judge stub.

    Parameters
    ----------
    verdicts:
        Mapping from ``candidate.id`` to the ``JudgeVerdict`` to return.
        Any candidate whose ID is not in this map gets ``default_verdict``.
    default_verdict:
        Verdict returned for candidates not in ``verdicts``.
        Defaults to ``DEFAULT_ACCEPT``.
    """

    verdicts: dict[str, JudgeVerdict] = field(default_factory=dict)
    default_verdict: JudgeVerdict = field(default_factory=lambda: DEFAULT_ACCEPT)

    #: Record of (candidate, conn) for every ``evaluate`` call.
    calls: list[tuple[JudgeCandidate, sqlite3.Connection]] = field(
        default_factory=list, repr=False
    )

    def evaluate(
        self,
        candidate: JudgeCandidate,
        conn: sqlite3.Connection,
        *,
        now: datetime | None = None,  # noqa: ARG002  (unused in stub)
    ) -> JudgeVerdict:
        """Return the pre-configured verdict for *candidate.id*."""
        self.calls.append((candidate, conn))
        return self.verdicts.get(candidate.id, self.default_verdict)

    def reset_calls(self) -> None:
        """Clear the call history (useful in tests with multiple assertions)."""
        self.calls.clear()


__all__ = [
    "DEFAULT_ACCEPT",
    "DEFAULT_FABRICATED",
    "DEFAULT_REJECT",
    "FakeJudge",
]
