"""Tests for oxi_core.v3.judge and tests/fixtures/fake_judge.py.

Coverage
--------
- ``JudgeScore`` / ``JudgeCandidate`` / ``JudgeVerdict`` dataclasses.
- ``HaikuJudge.evaluate`` with a mocked Anthropic client (no real API calls).
- Fabricated-module hard filter and the event it emits.
- Score-threshold accept/reject logic.
- Budget enforcement (global and judge-specific).
- ``_current_v3_modules`` returns a frozenset of real module names.
- ``FakeJudge`` deterministic verdict lookup and call recording.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from oxi_core import db
from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.v3.judge import (
    EXTERNAL_PROPOSAL_ACCEPTED,
    EXTERNAL_PROPOSAL_REJECTED,
    EXTERNAL_PROPOSAL_REJECTED_FABRICATED,
    HaikuJudge,
    JudgeBudgetExceeded,
    JudgeCandidate,
    JudgeEvaluationError,
    JudgeScore,
    JudgeVerdict,
    _build_prompt,
    _current_v3_modules,
    _estimate_cost,
    _judge_spend_today,
    _parse_response,
)


# ---------------------------------------------------------------------------
# Adapter fixture (HaikuJudge needs global budget.check → adapter.budget())
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    db_path_value: str
    hard: float = 5.0
    soft: float = 1.0

    def naming(self): return NamingConfig()
    def paths(self): return PathsConfig(db_path=self.db_path_value)
    def budget(self):
        return BudgetCaps(
            daily_soft_warn=self.soft,
            daily_hard_cap=self.hard,
            per_task_opus=0.5,
            per_task_sonnet=0.2,
        )
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (DispatchHost(name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"),)
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset_adapter():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def conn(tmp_path: Path):
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db.connect()
    yield handle.connection
    handle.connection.close()


# ---------------------------------------------------------------------------
# Helpers — build fake Anthropic responses
# ---------------------------------------------------------------------------


def _tool_response(
    relevance: int = 4,
    concreteness: int = 4,
    suggested_tier: int = 1,
    duplicate: bool = False,
    fabricated_module: bool = False,
    rationale: str = "looks good",
) -> MagicMock:
    """Build a fake ``anthropic.types.Message`` with a tool-use block."""
    tool_block = SimpleNamespace(
        type="tool_use",
        name="submit_rubric_score",
        input={
            "relevance": relevance,
            "concreteness": concreteness,
            "suggested_tier": suggested_tier,
            "duplicate": duplicate,
            "fabricated_module": fabricated_module,
            "rationale": rationale,
        },
    )
    usage = SimpleNamespace(input_tokens=100, output_tokens=50)
    response = SimpleNamespace(content=[tool_block], usage=usage)
    return response  # type: ignore[return-value]


def _no_tool_response() -> MagicMock:
    """A response with no tool-use block — triggers parse error."""
    text_block = SimpleNamespace(type="text")
    return SimpleNamespace(content=[text_block], usage=SimpleNamespace(input_tokens=10, output_tokens=5))  # type: ignore[return-value]


def _judge_with_mock(response) -> HaikuJudge:
    """Return a ``HaikuJudge`` whose Anthropic client returns *response*."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = response
    return HaikuJudge(_client=mock_client)


# ---------------------------------------------------------------------------
# JudgeScore dataclass
# ---------------------------------------------------------------------------


def test_judge_score_is_frozen():
    from dataclasses import FrozenInstanceError
    score = JudgeScore(
        relevance=3, concreteness=3, suggested_tier=1,
        duplicate=False, fabricated_module=False, rationale="x",
    )
    with pytest.raises(FrozenInstanceError):
        score.relevance = 5  # type: ignore[misc]


def test_judge_score_fields():
    score = JudgeScore(
        relevance=5, concreteness=4, suggested_tier=0,
        duplicate=True, fabricated_module=False, rationale="hello",
    )
    assert score.relevance == 5
    assert score.concreteness == 4
    assert score.suggested_tier == 0
    assert score.duplicate is True
    assert score.fabricated_module is False
    assert score.rationale == "hello"


# ---------------------------------------------------------------------------
# JudgeCandidate dataclass
# ---------------------------------------------------------------------------


def test_judge_candidate_defaults():
    c = JudgeCandidate(id="ext-001", title="Do a thing")
    assert c.description == ""


def test_judge_candidate_is_frozen():
    from dataclasses import FrozenInstanceError
    c = JudgeCandidate(id="x", title="y")
    with pytest.raises(FrozenInstanceError):
        c.title = "z"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# JudgeVerdict dataclass
# ---------------------------------------------------------------------------


def test_judge_verdict_is_frozen():
    from dataclasses import FrozenInstanceError
    score = JudgeScore(3, 3, 1, False, False, "r")
    v = JudgeVerdict(
        accepted=True, score=score,
        event_kind=EXTERNAL_PROPOSAL_ACCEPTED, rationale="r",
    )
    with pytest.raises(FrozenInstanceError):
        v.accepted = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _current_v3_modules
# ---------------------------------------------------------------------------


def test_current_v3_modules_returns_frozenset():
    modules = _current_v3_modules()
    assert isinstance(modules, frozenset)


def test_current_v3_modules_contains_known_modules():
    modules = _current_v3_modules()
    # These modules exist in the package and must always appear.
    for expected in ("budget", "critic", "dispatch", "judge"):
        assert expected in modules, (
            f"expected module {expected!r} in v3, got: {sorted(modules)}"
        )


def test_current_v3_modules_does_not_contain_nonexistent():
    modules = _current_v3_modules()
    assert "completely_nonexistent_xyzzy" not in modules


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


def test_parse_response_valid():
    response = _tool_response(relevance=5, concreteness=4, suggested_tier=0,
                              duplicate=False, fabricated_module=True, rationale="ok")
    score = _parse_response(response)
    assert score.relevance == 5
    assert score.concreteness == 4
    assert score.suggested_tier == 0
    assert score.duplicate is False
    assert score.fabricated_module is True
    assert score.rationale == "ok"


def test_parse_response_no_tool_block_raises():
    response = _no_tool_response()
    with pytest.raises(JudgeEvaluationError, match="no.*tool"):
        _parse_response(response)


def test_parse_response_relevance_out_of_range_raises():
    response = _tool_response(relevance=6)
    with pytest.raises(JudgeEvaluationError, match="relevance"):
        _parse_response(response)


def test_parse_response_concreteness_out_of_range_raises():
    response = _tool_response(concreteness=0)
    with pytest.raises(JudgeEvaluationError, match="concreteness"):
        _parse_response(response)


def test_parse_response_bad_tier_raises():
    response = _tool_response(suggested_tier=3)
    with pytest.raises(JudgeEvaluationError, match="suggested_tier"):
        _parse_response(response)


# ---------------------------------------------------------------------------
# _estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_uses_usage():
    response = _tool_response()  # 100 input, 50 output
    cost = _estimate_cost(response)
    # Haiku: $0.80/M input, $4.00/M output
    expected = 100 * 0.80 / 1_000_000 + 50 * 4.00 / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_estimate_cost_zero_when_no_usage():
    response = SimpleNamespace(content=[], usage=None)
    assert _estimate_cost(response) == 0.0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HaikuJudge.evaluate — accept path
# ---------------------------------------------------------------------------


def test_evaluate_accept_emits_accepted_event(conn):
    judge = _judge_with_mock(_tool_response(relevance=4, concreteness=4))
    candidate = JudgeCandidate(id="ext-001", title="Add feature X")
    verdict = judge.evaluate(candidate, conn)
    assert verdict.accepted is True
    assert verdict.event_kind == EXTERNAL_PROPOSAL_ACCEPTED
    # Check ledger
    row = conn.execute(
        "SELECT kind FROM event WHERE kind = ?", (EXTERNAL_PROPOSAL_ACCEPTED,)
    ).fetchone()
    assert row is not None


def test_evaluate_accept_verdict_contains_score(conn):
    judge = _judge_with_mock(_tool_response(relevance=5, concreteness=3, suggested_tier=0))
    verdict = judge.evaluate(JudgeCandidate(id="x", title="y"), conn)
    assert verdict.score.relevance == 5
    assert verdict.score.suggested_tier == 0


# ---------------------------------------------------------------------------
# HaikuJudge.evaluate — reject paths
# ---------------------------------------------------------------------------


def test_evaluate_low_relevance_rejects(conn):
    judge = _judge_with_mock(_tool_response(relevance=2, concreteness=4))
    verdict = judge.evaluate(JudgeCandidate(id="x", title="y"), conn)
    assert verdict.accepted is False
    assert verdict.event_kind == EXTERNAL_PROPOSAL_REJECTED


def test_evaluate_low_concreteness_rejects(conn):
    judge = _judge_with_mock(_tool_response(relevance=4, concreteness=2))
    verdict = judge.evaluate(JudgeCandidate(id="x", title="y"), conn)
    assert verdict.accepted is False
    assert verdict.event_kind == EXTERNAL_PROPOSAL_REJECTED


def test_evaluate_duplicate_rejects(conn):
    judge = _judge_with_mock(
        _tool_response(relevance=5, concreteness=5, duplicate=True)
    )
    verdict = judge.evaluate(JudgeCandidate(id="x", title="y"), conn)
    assert verdict.accepted is False
    assert verdict.event_kind == EXTERNAL_PROPOSAL_REJECTED


def test_evaluate_rejected_emits_event(conn):
    judge = _judge_with_mock(_tool_response(relevance=1, concreteness=1))
    judge.evaluate(JudgeCandidate(id="ext-bad", title="z"), conn)
    row = conn.execute(
        "SELECT kind FROM event WHERE kind = ?", (EXTERNAL_PROPOSAL_REJECTED,)
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# HaikuJudge.evaluate — fabricated_module hard filter
# ---------------------------------------------------------------------------


def test_evaluate_fabricated_module_hard_rejects(conn):
    judge = _judge_with_mock(
        _tool_response(relevance=5, concreteness=5, fabricated_module=True)
    )
    verdict = judge.evaluate(JudgeCandidate(id="ext-fab", title="Use oxi_core.v3.nonexistent"), conn)
    assert verdict.accepted is False
    assert verdict.event_kind == EXTERNAL_PROPOSAL_REJECTED_FABRICATED


def test_evaluate_fabricated_module_emits_correct_event_kind(conn):
    judge = _judge_with_mock(
        _tool_response(fabricated_module=True)
    )
    judge.evaluate(JudgeCandidate(id="ext-fab", title="t"), conn)
    row = conn.execute(
        "SELECT kind, payload FROM event WHERE kind = ?",
        (EXTERNAL_PROPOSAL_REJECTED_FABRICATED,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[1])
    assert payload["candidate_id"] == "ext-fab"
    assert payload["score"]["fabricated_module"] is True


def test_evaluate_fabricated_overrides_high_scores(conn):
    """Even a 5/5 score is rejected when fabricated_module=True."""
    judge = _judge_with_mock(
        _tool_response(relevance=5, concreteness=5, fabricated_module=True)
    )
    verdict = judge.evaluate(JudgeCandidate(id="x", title="t"), conn)
    assert verdict.accepted is False
    assert verdict.event_kind == EXTERNAL_PROPOSAL_REJECTED_FABRICATED


# ---------------------------------------------------------------------------
# HaikuJudge.evaluate — event payload structure
# ---------------------------------------------------------------------------


def test_evaluate_payload_includes_cost_usd(conn):
    judge = _judge_with_mock(_tool_response())
    judge.evaluate(JudgeCandidate(id="ext-p", title="t"), conn)
    row = conn.execute(
        "SELECT payload FROM event WHERE kind IN (?, ?, ?)",
        (EXTERNAL_PROPOSAL_ACCEPTED, EXTERNAL_PROPOSAL_REJECTED, EXTERNAL_PROPOSAL_REJECTED_FABRICATED),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert "cost_usd" in payload
    assert isinstance(payload["cost_usd"], float)


def test_evaluate_payload_includes_candidate_id(conn):
    judge = _judge_with_mock(_tool_response())
    judge.evaluate(JudgeCandidate(id="my-candidate", title="t"), conn)
    row = conn.execute(
        "SELECT payload FROM event WHERE kind = ?", (EXTERNAL_PROPOSAL_ACCEPTED,)
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["candidate_id"] == "my-candidate"


# ---------------------------------------------------------------------------
# HaikuJudge.evaluate — budget enforcement
# ---------------------------------------------------------------------------


def test_evaluate_raises_on_global_hard_stop(conn):
    """When the global budget is exhausted, evaluate should raise JudgeBudgetExceeded."""
    # Insert a task that exceeds the global hard cap ($5).
    conn.execute(
        "INSERT INTO task (identifier, tier, title, cost_usd, updated_at) "
        "VALUES ('T0-1', 0, 't', 10.0, datetime('now'))"
    )
    conn.commit()
    judge = _judge_with_mock(_tool_response())
    with pytest.raises(JudgeBudgetExceeded, match="global"):
        judge.evaluate(JudgeCandidate(id="x", title="y"), conn)


def test_evaluate_raises_on_judge_daily_cap(conn):
    """When judge-specific spend exceeds the per-day cap, evaluate raises."""
    # Seed a fake accepted event with a cost that exceeds the judge cap.
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (
                EXTERNAL_PROPOSAL_ACCEPTED,
                json.dumps({"candidate_id": "old", "cost_usd": 6.0}),
            ),
        )
    judge = HaikuJudge(judge_daily_cap_usd=5.0)  # no mock needed — raises before API call
    with pytest.raises(JudgeBudgetExceeded, match="judge"):
        judge.evaluate(JudgeCandidate(id="new", title="t"), conn)


def test_evaluate_does_not_call_api_when_budget_exhausted(conn, tmp_path):
    """The Anthropic client must not be called when either budget is exceeded."""
    # Exhaust the global budget.
    conn.execute(
        "INSERT INTO task (identifier, tier, title, cost_usd, updated_at) "
        "VALUES ('T0-1', 0, 't', 10.0, datetime('now'))"
    )
    conn.commit()
    mock_client = MagicMock()
    judge = HaikuJudge(_client=mock_client)
    with pytest.raises(JudgeBudgetExceeded):
        judge.evaluate(JudgeCandidate(id="x", title="y"), conn)
    mock_client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# _judge_spend_today
# ---------------------------------------------------------------------------


def test_judge_spend_today_sums_external_events(conn):
    now = datetime.now(tz=UTC)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for kind, cost in [
        (EXTERNAL_PROPOSAL_ACCEPTED, 0.001),
        (EXTERNAL_PROPOSAL_REJECTED, 0.002),
        (EXTERNAL_PROPOSAL_REJECTED_FABRICATED, 0.0005),
    ]:
        with conn:
            conn.execute(
                "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
                (kind, json.dumps({"cost_usd": cost})),
            )
    spend = _judge_spend_today(conn, window_start)
    assert abs(spend - 0.0035) < 1e-9


def test_judge_spend_today_ignores_non_external_events(conn):
    now = datetime.now(tz=UTC)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            ("budget_hard_stop", json.dumps({"cost_usd": 100.0})),
        )
    spend = _judge_spend_today(conn, window_start)
    assert spend == 0.0


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_candidate_id():
    candidate = JudgeCandidate(id="ext-007", title="Improve planner")
    prompt = _build_prompt(candidate, frozenset({"budget", "critic"}))
    assert "ext-007" in prompt
    assert "Improve planner" in prompt


def test_build_prompt_lists_known_modules():
    prompt = _build_prompt(
        JudgeCandidate(id="x", title="t"),
        frozenset({"budget", "dispatch", "judge"}),
    )
    assert "budget" in prompt
    assert "dispatch" in prompt
    assert "judge" in prompt


def test_build_prompt_handles_empty_module_list():
    prompt = _build_prompt(JudgeCandidate(id="x", title="t"), frozenset())
    assert "(none)" in prompt


# ---------------------------------------------------------------------------
# HaikuJudge.evaluate — API error path
# ---------------------------------------------------------------------------


def test_evaluate_api_error_raises_judge_evaluation_error(conn):
    import anthropic as _anthropic
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _anthropic.APIError(
        message="service unavailable",
        request=MagicMock(),
        body=None,
    )
    judge = HaikuJudge(_client=mock_client)
    with pytest.raises(JudgeEvaluationError, match="API error"):
        judge.evaluate(JudgeCandidate(id="x", title="y"), conn)


# ---------------------------------------------------------------------------
# FakeJudge
# ---------------------------------------------------------------------------


def test_fake_judge_returns_configured_verdict(conn):
    from tests.fixtures.fake_judge import FakeJudge, DEFAULT_REJECT

    fake = FakeJudge(verdicts={"ext-001": DEFAULT_REJECT})
    candidate = JudgeCandidate(id="ext-001", title="t")
    verdict = fake.evaluate(candidate, conn)
    assert verdict.accepted is False
    assert verdict.event_kind == EXTERNAL_PROPOSAL_REJECTED


def test_fake_judge_returns_default_for_unknown_id(conn):
    from tests.fixtures.fake_judge import FakeJudge, DEFAULT_ACCEPT

    fake = FakeJudge()
    verdict = fake.evaluate(JudgeCandidate(id="unknown", title="t"), conn)
    assert verdict == DEFAULT_ACCEPT


def test_fake_judge_records_calls(conn):
    from tests.fixtures.fake_judge import FakeJudge

    fake = FakeJudge()
    c1 = JudgeCandidate(id="a", title="A")
    c2 = JudgeCandidate(id="b", title="B")
    fake.evaluate(c1, conn)
    fake.evaluate(c2, conn)
    assert len(fake.calls) == 2
    assert fake.calls[0][0] == c1
    assert fake.calls[1][0] == c2


def test_fake_judge_reset_calls(conn):
    from tests.fixtures.fake_judge import FakeJudge

    fake = FakeJudge()
    fake.evaluate(JudgeCandidate(id="x", title="t"), conn)
    fake.reset_calls()
    assert fake.calls == []


def test_fake_judge_fabricated_verdict(conn):
    from tests.fixtures.fake_judge import FakeJudge, DEFAULT_FABRICATED

    fake = FakeJudge(verdicts={"bad": DEFAULT_FABRICATED})
    verdict = fake.evaluate(JudgeCandidate(id="bad", title="t"), conn)
    assert verdict.accepted is False
    assert verdict.event_kind == EXTERNAL_PROPOSAL_REJECTED_FABRICATED
    assert verdict.score.fabricated_module is True


def test_fake_judge_custom_default_verdict(conn):
    from tests.fixtures.fake_judge import FakeJudge, DEFAULT_REJECT

    fake = FakeJudge(default_verdict=DEFAULT_REJECT)
    verdict = fake.evaluate(JudgeCandidate(id="anything", title="t"), conn)
    assert verdict.accepted is False


def test_fake_judge_satisfies_backend_protocol():
    """FakeJudge should satisfy the JudgeBackend structural protocol."""
    from oxi_core.v3.judge import JudgeBackend
    from tests.fixtures.fake_judge import FakeJudge

    # Protocol is structural (not runtime_checkable), so we use isinstance
    # check on the concrete backend protocol method.
    fake = FakeJudge()
    assert hasattr(fake, "evaluate")
    # Quick duck-type check that the signature is compatible.
    import inspect
    sig = inspect.signature(fake.evaluate)
    params = list(sig.parameters)
    assert "candidate" in params
    assert "conn" in params
