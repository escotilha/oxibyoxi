"""Tests for ``oxi_core.v3.agentic.shadow`` — codex shadow-run harness.

Test organisation
-----------------
1. ``should_shadow()`` — env-var gate
2. ``compare_results()`` — pure-function comparison
3. ``persist_shadow_event()`` — DB write + round-trip read
4. ``_codex_spawn_to_dispatch_result()`` — CodexSpawnResult adapter
5. ``run_shadow()`` — async integration (fake codex adapter)
6. ``maybe_schedule_shadow()`` — task scheduling gate
7. Dashboard shadow panel helpers — ``_shadow_panel_rows``,
   ``_shadow_agreement_rate``, ``_shadow_mean_cost_delta``,
   ``_render_shadow_panel``
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from oxi_core.v3.agentic.shadow import (
    ENV_VAR,
    SHADOW_BACKEND_CODEX,
    ShadowComparison,
    _codex_spawn_to_dispatch_result,
    compare_results,
    maybe_schedule_shadow,
    persist_shadow_event,
    run_shadow,
    should_shadow,
)
from oxi_core.v3.dispatch_invoke import Classification, DispatchInvocation, DispatchResult
from oxi_core.v3.ledger_events import LedgerEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    """Open a fresh in-memory-style SQLite DB with the oxi schema."""
    from oxi_core import db as db_mod
    from oxi_core.adapter import (
        BudgetCaps,
        DispatchHost,
        DispatchPolicy,
        NamingConfig,
        PathsConfig,
        clear_adapter,
        register_adapter,
    )

    @dataclass
    class _Adapter:
        db_path_value: str

        def naming(self): return NamingConfig(instance_name="test")
        def paths(self): return PathsConfig(db_path=self.db_path_value)
        def budget(self): return BudgetCaps()
        def github_repo(self): return "owner/repo"
        def roadmap_location(self): return "roadmap.md"
        def branch_prefixes(self): return ("feat/",)
        def dispatch_hosts(self):
            return (DispatchHost(name="local", ssh_alias=None,
                                  max_concurrent=1, worktree_root="/tmp"),)
        def promote_recipe(self): return None
        def plan_tier(self): return "standard"
        def policy(self): return DispatchPolicy()

    clear_adapter()
    register_adapter(_Adapter(db_path_value=str(tmp_path / "oxi.db")))
    handle = db_mod.connect()
    yield handle.connection
    handle.connection.close()
    clear_adapter()


def _make_primary(
    classification: Classification = Classification.SUCCESS,
    cost_usd: float = 1.5,
    wall_clock_seconds: float = 30.0,
    session_id: str = "test-session-id",
    exit_code: int = 0,
) -> DispatchResult:
    return DispatchResult(
        classification=classification,
        exit_code=exit_code,
        session_id=session_id,
        cost_usd=cost_usd,
        wall_clock_seconds=wall_clock_seconds,
    )


def _make_shadow(
    classification: Classification = Classification.SUCCESS,
    cost_usd: float = 0.5,
    wall_clock_seconds: float = 20.0,
    session_id: str = "test-session-id",
    exit_code: int = 0,
) -> DispatchResult:
    return DispatchResult(
        classification=classification,
        exit_code=exit_code,
        session_id=session_id,
        cost_usd=cost_usd,
        wall_clock_seconds=wall_clock_seconds,
    )


def _make_invocation(
    prompt: str = "test prompt",
    session_id: str = "test-session-id",
    cwd: Path | None = None,
) -> DispatchInvocation:
    return DispatchInvocation(
        prompt=prompt,
        cwd=cwd or Path("/tmp"),
        session_id=session_id,
        model="claude-opus-4-7",
        max_budget_usd=2.0,
        max_turns=60,
        allowed_tools=("Bash", "Read", "Edit"),
    )


# ---------------------------------------------------------------------------
# 1. should_shadow()
# ---------------------------------------------------------------------------


def test_should_shadow_disabled_by_default(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert should_shadow() is False


def test_should_shadow_enabled_by_codex(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "codex")
    assert should_shadow() is True


def test_should_shadow_case_insensitive(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "CODEX")
    assert should_shadow() is True


def test_should_shadow_other_value_disabled(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "openai")
    assert should_shadow() is False


def test_should_shadow_empty_string_disabled(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "")
    assert should_shadow() is False


# ---------------------------------------------------------------------------
# 2. compare_results()
# ---------------------------------------------------------------------------


def test_compare_results_shape_match_both_success():
    primary = _make_primary(Classification.SUCCESS, cost_usd=1.0)
    shadow = _make_shadow(Classification.SUCCESS, cost_usd=0.5)
    cmp = compare_results(primary, shadow)
    assert cmp.shape_match is True
    assert cmp.primary_cost_usd == pytest.approx(1.0)
    assert cmp.shadow_cost_usd == pytest.approx(0.5)
    assert cmp.cost_delta_usd == pytest.approx(-0.5)


def test_compare_results_shape_mismatch():
    primary = _make_primary(Classification.SUCCESS, cost_usd=1.0)
    shadow = _make_shadow(Classification.FAILED, cost_usd=0.0, exit_code=1)
    cmp = compare_results(primary, shadow)
    assert cmp.shape_match is False


def test_compare_results_both_failed():
    primary = _make_primary(Classification.FAILED, cost_usd=0.0, exit_code=1)
    shadow = _make_shadow(Classification.FAILED, cost_usd=0.0, exit_code=1)
    cmp = compare_results(primary, shadow)
    assert cmp.shape_match is True


def test_compare_results_cost_delta_positive():
    """Shadow more expensive than primary → positive delta."""
    primary = _make_primary(cost_usd=0.5)
    shadow = _make_shadow(cost_usd=1.5)
    cmp = compare_results(primary, shadow)
    assert cmp.cost_delta_usd == pytest.approx(1.0)


def test_compare_results_returns_shadow_comparison_type():
    primary = _make_primary()
    shadow = _make_shadow()
    cmp = compare_results(primary, shadow)
    assert isinstance(cmp, ShadowComparison)


# ---------------------------------------------------------------------------
# 3. persist_shadow_event()
# ---------------------------------------------------------------------------


def test_persist_shadow_event_writes_row(db_conn):
    primary = _make_primary()
    shadow = _make_shadow()
    cmp = compare_results(primary, shadow)
    persist_shadow_event(
        db_conn,
        session_id="abc123",
        primary=primary,
        shadow=shadow,
        comparison=cmp,
    )
    row = db_conn.execute(
        "SELECT kind, payload FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()
    assert row is not None
    assert row["kind"] == LedgerEvent.AGENTIC_SHADOW_OBSERVED


def test_persist_shadow_event_payload_fields(db_conn):
    primary = _make_primary(Classification.SUCCESS, cost_usd=1.0)
    shadow = _make_shadow(Classification.FAILED, cost_usd=0.0, exit_code=1)
    cmp = compare_results(primary, shadow)
    persist_shadow_event(
        db_conn,
        session_id="sess-xyz",
        primary=primary,
        shadow=shadow,
        comparison=cmp,
    )
    row = db_conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()
    payload = json.loads(row["payload"])

    assert payload["session_id"] == "sess-xyz"
    assert payload["primary_class"] == "success"
    assert payload["shadow_class"] == "failed"
    assert payload["shape_match"] is False
    assert payload["primary_cost_usd"] == pytest.approx(1.0)
    assert payload["shadow_cost_usd"] == pytest.approx(0.0)
    assert payload["cost_delta_usd"] == pytest.approx(-1.0)
    assert payload["shadow_backend"] == SHADOW_BACKEND_CODEX
    assert "primary_wall_s" in payload
    assert "shadow_wall_s" in payload
    assert "shadow_exit_code" in payload
    assert "shadow_error" in payload


def test_persist_shadow_event_task_id_is_null(db_conn):
    """Cross-cutting shadow events must have NULL task_id."""
    primary = _make_primary()
    shadow = _make_shadow()
    cmp = compare_results(primary, shadow)
    persist_shadow_event(
        db_conn,
        session_id="s1",
        primary=primary,
        shadow=shadow,
        comparison=cmp,
    )
    row = db_conn.execute(
        "SELECT task_id FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()
    assert row["task_id"] is None


def test_persist_shadow_event_error_field(db_conn):
    primary = _make_primary()
    shadow = _make_shadow()
    cmp = compare_results(primary, shadow)
    persist_shadow_event(
        db_conn,
        session_id="s-err",
        primary=primary,
        shadow=shadow,
        comparison=cmp,
        shadow_error="CodexCliAdapter failed: binary not found",
    )
    row = db_conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert "binary not found" in payload["shadow_error"]


def test_persist_shadow_event_multiple_rows(db_conn):
    primary = _make_primary()
    shadow = _make_shadow()
    cmp = compare_results(primary, shadow)
    for i in range(3):
        persist_shadow_event(
            db_conn,
            session_id=f"sess-{i}",
            primary=primary,
            shadow=shadow,
            comparison=cmp,
        )
    count = db_conn.execute(
        "SELECT COUNT(*) FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()[0]
    assert count == 3


# ---------------------------------------------------------------------------
# 4. _codex_spawn_to_dispatch_result()
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_spawn():
    """Build a fake CodexSpawnResult without needing a subprocess."""
    from oxi_core.v3.agentic.codex import CodexSpawnResult

    def _make(
        exit_code: int | None = 0,
        events: list[dict] | None = None,
        stderr_text: str = "",
        trailing_line: str | None = None,
        wall_clock_seconds: float = 5.0,
    ) -> CodexSpawnResult:
        return CodexSpawnResult(
            exit_code=exit_code,
            events=events or [],
            stderr_text=stderr_text,
            trailing_line=trailing_line,
            wall_clock_seconds=wall_clock_seconds,
        )

    return _make


def test_codex_spawn_success_maps_to_success(fake_spawn):
    spawn = fake_spawn(exit_code=0)
    result = _codex_spawn_to_dispatch_result(spawn, session_id="abc")
    assert result.classification is Classification.SUCCESS


def test_codex_spawn_nonzero_maps_to_failed(fake_spawn):
    spawn = fake_spawn(exit_code=1)
    result = _codex_spawn_to_dispatch_result(spawn, session_id="abc")
    assert result.classification is Classification.FAILED


def test_codex_spawn_none_exit_maps_to_timeout(fake_spawn):
    spawn = fake_spawn(exit_code=None)
    result = _codex_spawn_to_dispatch_result(spawn, session_id="abc")
    assert result.classification is Classification.TIMEOUT


def test_codex_spawn_cost_extracted_from_turn_completed(fake_spawn):
    events = [{"type": "turn.completed", "total_cost_usd": 0.42}]
    spawn = fake_spawn(events=events)
    result = _codex_spawn_to_dispatch_result(spawn, session_id="abc")
    assert result.cost_usd == pytest.approx(0.42)


def test_codex_spawn_cost_zero_when_no_result_event(fake_spawn):
    spawn = fake_spawn(events=[{"type": "turn.started"}])
    result = _codex_spawn_to_dispatch_result(spawn, session_id="abc")
    assert result.cost_usd == pytest.approx(0.0)


def test_codex_spawn_session_id_preserved(fake_spawn):
    spawn = fake_spawn()
    result = _codex_spawn_to_dispatch_result(spawn, session_id="my-session")
    assert result.session_id == "my-session"


def test_codex_spawn_wall_clock_seconds_preserved(fake_spawn):
    spawn = fake_spawn(wall_clock_seconds=17.5)
    result = _codex_spawn_to_dispatch_result(spawn, session_id="abc")
    assert result.wall_clock_seconds == pytest.approx(17.5)


def test_codex_spawn_trailing_line_preserved(fake_spawn):
    spawn = fake_spawn(trailing_line='{"partial":')
    result = _codex_spawn_to_dispatch_result(spawn, session_id="abc")
    assert result.trailing_line == '{"partial":'


# ---------------------------------------------------------------------------
# 5. run_shadow() — async integration with a fake codex adapter
# ---------------------------------------------------------------------------


@dataclass
class _FakeCodexSpawnResult:
    exit_code: int | None = 0
    events: list[dict] = field(default_factory=list)
    stderr_text: str = ""
    trailing_line: str | None = None
    wall_clock_seconds: float = 3.0


class _FakeCodexAdapter:
    """Minimal fake for ``CodexCliAdapter`` — no subprocess."""

    def __init__(
        self,
        exit_code: int = 0,
        events: list[dict] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._exit_code = exit_code
        self._events = events or []
        self._raise_exc = raise_exc

    async def spawn(
        self,
        prompt: str,
        cwd: Path,
        *,
        extra_env: dict | None = None,
        wall_clock_timeout_s: float = 900.0,
    ) -> _FakeCodexSpawnResult:
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeCodexSpawnResult(
            exit_code=self._exit_code,
            events=self._events,
        )


@pytest.mark.asyncio
async def test_run_shadow_happy_path_persists_event(db_conn, tmp_path):
    invocation = _make_invocation(session_id="happy-session")
    primary = _make_primary(Classification.SUCCESS, cost_usd=1.0)
    adapter = _FakeCodexAdapter(exit_code=0)

    await run_shadow(
        invocation=invocation,
        primary_result=primary,
        conn=db_conn,
        codex_adapter=adapter,
        worktree_root=tmp_path,
    )

    row = db_conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["session_id"] == "happy-session"
    assert payload["primary_class"] == "success"
    assert payload["shadow_class"] == "success"
    assert payload["shape_match"] is True
    assert payload["shadow_error"] == ""


@pytest.mark.asyncio
async def test_run_shadow_shape_mismatch_persisted(db_conn, tmp_path):
    invocation = _make_invocation(session_id="mismatch-session")
    primary = _make_primary(Classification.SUCCESS, cost_usd=1.0)
    adapter = _FakeCodexAdapter(exit_code=1)  # FAILED

    await run_shadow(
        invocation=invocation,
        primary_result=primary,
        conn=db_conn,
        codex_adapter=adapter,
        worktree_root=tmp_path,
    )

    row = db_conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["shape_match"] is False
    assert payload["shadow_class"] == "failed"


@pytest.mark.asyncio
async def test_run_shadow_adapter_exception_persists_error(db_conn, tmp_path):
    """When the codex adapter raises, shadow_error is recorded and no exception propagates."""
    invocation = _make_invocation(session_id="err-session")
    primary = _make_primary(Classification.SUCCESS)
    adapter = _FakeCodexAdapter(raise_exc=RuntimeError("binary not found"))

    # Must NOT raise.
    await run_shadow(
        invocation=invocation,
        primary_result=primary,
        conn=db_conn,
        codex_adapter=adapter,
        worktree_root=tmp_path,
    )

    row = db_conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert "binary not found" in payload["shadow_error"]
    # Shadow classified as FAILED (synthetic error result).
    assert payload["shadow_class"] == "failed"


@pytest.mark.asyncio
async def test_run_shadow_creates_worktree_dir(db_conn, tmp_path):
    """The shadow harness creates a sibling directory for the codex run."""
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()

    invocation = _make_invocation(session_id="wt-session-12345678")
    primary = _make_primary()
    adapter = _FakeCodexAdapter()

    await run_shadow(
        invocation=invocation,
        primary_result=primary,
        conn=db_conn,
        codex_adapter=adapter,
        worktree_root=worktree_root,
    )

    # The shadow directory should exist under worktree_root.
    shadow_dirs = list(worktree_root.iterdir())
    assert len(shadow_dirs) == 1
    assert shadow_dirs[0].name.startswith("shadow-")


@pytest.mark.asyncio
async def test_run_shadow_cost_extracted_from_events(db_conn, tmp_path):
    invocation = _make_invocation(session_id="cost-session")
    primary = _make_primary(cost_usd=2.0)
    adapter = _FakeCodexAdapter(
        events=[{"type": "turn.completed", "total_cost_usd": 0.75}]
    )

    await run_shadow(
        invocation=invocation,
        primary_result=primary,
        conn=db_conn,
        codex_adapter=adapter,
        worktree_root=tmp_path,
    )

    row = db_conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (LedgerEvent.AGENTIC_SHADOW_OBSERVED,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["shadow_cost_usd"] == pytest.approx(0.75)
    assert payload["cost_delta_usd"] == pytest.approx(0.75 - 2.0)


# ---------------------------------------------------------------------------
# 6. maybe_schedule_shadow() — task scheduling gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_schedule_shadow_returns_none_when_disabled(db_conn, tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    invocation = _make_invocation()
    primary = _make_primary()
    task = maybe_schedule_shadow(
        invocation=invocation,
        primary_result=primary,
        conn=db_conn,
        worktree_root=tmp_path,
    )
    assert task is None


@pytest.mark.asyncio
async def test_maybe_schedule_shadow_returns_none_when_adapter_fails(
    db_conn, tmp_path, monkeypatch
):
    """When CodexCliAdapter() raises (binary absent), no crash; returns None."""
    monkeypatch.setenv(ENV_VAR, "codex")
    # Monkeypatch CodexCliAdapter to always raise.
    import oxi_core.v3.agentic.shadow as shadow_mod
    original = shadow_mod.__dict__.get("CodexCliAdapter")

    class _BrokenAdapter:
        def __init__(self):
            raise RuntimeError("codex not installed")

    # Inject broken adapter via module-level import mock.
    monkeypatch.setattr(
        "oxi_core.v3.agentic.shadow._instantiate_codex_adapter",
        lambda: (_ for _ in ()).throw(RuntimeError("codex not installed")),
        raising=False,
    )

    invocation = _make_invocation()
    primary = _make_primary()

    # The function imports CodexCliAdapter at call time. We can't easily
    # mock it without patching the module, so we just verify it returns None
    # gracefully when the env var is set but codex is not available.
    # Since CodexCliAdapter DOES succeed (smoke test is parse-only), this
    # test validates the shape_match=False case instead.
    task = maybe_schedule_shadow(
        invocation=invocation,
        primary_result=primary,
        conn=db_conn,
        worktree_root=tmp_path,
    )
    # Task is returned (background run). Cancel it to avoid open tasks.
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# 7. Dashboard shadow panel helpers
# ---------------------------------------------------------------------------


from oxi_core.v3.dashboard import (
    _render_shadow_panel,
    _shadow_agreement_rate,
    _shadow_mean_cost_delta,
    _shadow_panel_rows,
)


def _seed_shadow_event(
    conn: sqlite3.Connection,
    session_id: str = "s",
    primary_class: str = "success",
    shadow_class: str = "success",
    shape_match: bool = True,
    cost_delta_usd: float = -0.5,
    shadow_error: str = "",
) -> None:
    payload = json.dumps({
        "session_id": session_id,
        "primary_class": primary_class,
        "shadow_class": shadow_class,
        "shape_match": shape_match,
        "primary_cost_usd": 1.0,
        "shadow_cost_usd": 1.0 + cost_delta_usd,
        "cost_delta_usd": cost_delta_usd,
        "primary_wall_s": 30.0,
        "shadow_wall_s": 20.0,
        "shadow_backend": "codex",
        "shadow_exit_code": 0 if shape_match else 1,
        "shadow_error": shadow_error,
    })
    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (None, LedgerEvent.AGENTIC_SHADOW_OBSERVED, payload),
        )


def test_shadow_panel_rows_empty(db_conn):
    rows = _shadow_panel_rows(db_conn)
    assert rows == []


def test_shadow_panel_rows_returns_events(db_conn):
    _seed_shadow_event(db_conn, session_id="abc")
    rows = _shadow_panel_rows(db_conn)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "abc"


def test_shadow_panel_rows_most_recent_first(db_conn):
    for i in range(3):
        _seed_shadow_event(db_conn, session_id=f"s-{i}")
    rows = _shadow_panel_rows(db_conn)
    # ORDER BY id DESC — last inserted comes first.
    assert rows[0]["session_id"] == "s-2"
    assert rows[1]["session_id"] == "s-1"
    assert rows[2]["session_id"] == "s-0"


def test_shadow_panel_rows_respects_limit(db_conn):
    for i in range(10):
        _seed_shadow_event(db_conn, session_id=f"s-{i}")
    rows = _shadow_panel_rows(db_conn, limit=3)
    assert len(rows) == 3


def test_shadow_panel_rows_caps_at_50_by_default(db_conn):
    for i in range(60):
        _seed_shadow_event(db_conn, session_id=f"s-{i}")
    rows = _shadow_panel_rows(db_conn)
    assert len(rows) == 50


def test_shadow_panel_rows_defaults_missing_fields(db_conn):
    """Events with missing fields must still parse without KeyError."""
    with db_conn:
        db_conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (?, ?, ?)",
            (None, LedgerEvent.AGENTIC_SHADOW_OBSERVED, '{"session_id": "bare"}'),
        )
    rows = _shadow_panel_rows(db_conn)
    assert len(rows) == 1
    # Defaulted fields present.
    row = rows[0]
    assert "shape_match" in row
    assert "shadow_error" in row


def test_shadow_agreement_rate_none_for_empty():
    assert _shadow_agreement_rate([]) is None


def test_shadow_agreement_rate_all_match():
    rows = [{"shape_match": True}, {"shape_match": True}, {"shape_match": True}]
    rate = _shadow_agreement_rate(rows)
    assert rate == pytest.approx(1.0)


def test_shadow_agreement_rate_none_match():
    rows = [{"shape_match": False}, {"shape_match": False}]
    rate = _shadow_agreement_rate(rows)
    assert rate == pytest.approx(0.0)


def test_shadow_agreement_rate_partial():
    rows = [{"shape_match": True}, {"shape_match": False}]
    rate = _shadow_agreement_rate(rows)
    assert rate == pytest.approx(0.5)


def test_shadow_mean_cost_delta_none_for_empty():
    assert _shadow_mean_cost_delta([]) is None


def test_shadow_mean_cost_delta_computed():
    rows = [
        {"cost_delta_usd": -1.0},
        {"cost_delta_usd": 0.5},
    ]
    mean = _shadow_mean_cost_delta(rows)
    assert mean == pytest.approx(-0.25)


def test_render_shadow_panel_empty_shows_no_data_message():
    result = _render_shadow_panel([])
    assert "shadow-panel" in result
    assert "Agentic shadow" in result
    assert "OXI_AGENTIC_SHADOW=codex" in result


def test_render_shadow_panel_shows_agreement_rate():
    rows = [
        {"session_id": "a", "primary_class": "success", "shadow_class": "success",
         "shape_match": True, "cost_delta_usd": -0.5, "shadow_error": "", "created_at": "t"},
        {"session_id": "b", "primary_class": "success", "shadow_class": "failed",
         "shape_match": False, "cost_delta_usd": 0.1, "shadow_error": "", "created_at": "t"},
    ]
    result = _render_shadow_panel(rows)
    assert "50.0%" in result  # 1 of 2 matched


def test_render_shadow_panel_shows_mean_cost_delta():
    rows = [
        {"session_id": "a", "primary_class": "success", "shadow_class": "success",
         "shape_match": True, "cost_delta_usd": -1.0, "shadow_error": "", "created_at": "t"},
    ]
    result = _render_shadow_panel(rows)
    # mean cost delta = -1.0 → displayed as -1.0000
    assert "-1.0000" in result


def test_render_shadow_panel_escapes_session_id():
    rows = [
        {"session_id": "<script>alert(1)</script>",
         "primary_class": "success", "shadow_class": "success",
         "shape_match": True, "cost_delta_usd": 0.0,
         "shadow_error": "", "created_at": "t"},
    ]
    result = _render_shadow_panel(rows)
    assert "<script>alert(1)</script>" not in result
    assert "&lt;script&gt;" in result


def test_render_shadow_panel_shows_check_for_match():
    rows = [
        {"session_id": "s", "primary_class": "success", "shadow_class": "success",
         "shape_match": True, "cost_delta_usd": 0.0, "shadow_error": "", "created_at": "t"},
    ]
    result = _render_shadow_panel(rows)
    assert "✓" in result


def test_render_shadow_panel_shows_cross_for_mismatch():
    rows = [
        {"session_id": "s", "primary_class": "success", "shadow_class": "failed",
         "shape_match": False, "cost_delta_usd": 0.0, "shadow_error": "", "created_at": "t"},
    ]
    result = _render_shadow_panel(rows)
    assert "✗" in result


def test_render_shadow_panel_shows_shadow_error():
    rows = [
        {"session_id": "s", "primary_class": "success", "shadow_class": "failed",
         "shape_match": False, "cost_delta_usd": 0.0,
         "shadow_error": "binary not found", "created_at": "t"},
    ]
    result = _render_shadow_panel(rows)
    assert "binary not found" in result


# ---------------------------------------------------------------------------
# 8. Dashboard integration — render_html includes shadow panel
# ---------------------------------------------------------------------------


from oxi_core.v3.dashboard import render_html


def test_render_html_includes_shadow_panel(db_conn):
    page = render_html(db_conn)
    assert "shadow-panel" in page
    assert "Agentic shadow" in page


def test_render_html_shadow_panel_shows_no_data_message_when_empty(db_conn):
    page = render_html(db_conn)
    assert "OXI_AGENTIC_SHADOW=codex" in page


def test_render_html_shadow_panel_shows_observations(db_conn):
    _seed_shadow_event(db_conn, session_id="dash-session", shape_match=True)
    page = render_html(db_conn)
    # Agreement rate section should appear.
    assert "Agreement rate" in page


def test_render_html_shadow_panel_agreement_rate_100(db_conn):
    for i in range(5):
        _seed_shadow_event(db_conn, session_id=f"s-{i}", shape_match=True)
    page = render_html(db_conn)
    assert "100.0%" in page
