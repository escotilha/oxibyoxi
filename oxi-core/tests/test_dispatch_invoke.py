"""Tests for oxi_core.v3.dispatch_invoke.

Every test runs against the fake claude binary under tests/fixtures.
No real Claude is invoked. The fake binary is a Python script that
reproduces the same stream-json + exit-code behavior as real claude
under the scenarios documented in the file itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from oxi_core.v3.dispatch_invoke import (
    Classification,
    DispatchInvocation,
    build_argv,
    build_env,
    generate_session_id,
    invoke,
)

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invocation(
    scenario: str,
    *,
    prompt: str = "do the thing",
    wall_clock_timeout_s: float = 30.0,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[DispatchInvocation, dict[str, str]]:
    """Build a DispatchInvocation that runs the fake claude with the given scenario.

    Returns (invocation, env-overrides). The env-overrides must be
    applied to the test process before running, because build_env
    reads from os.environ.
    """
    env_overrides = {"FAKE_CLAUDE_SCENARIO": scenario}
    if extra_env:
        env_overrides.update(extra_env)

    # The binary is the fake python script, invoked via the current python.
    invocation = DispatchInvocation(
        prompt=prompt,
        cwd=cwd or Path.cwd(),
        session_id=generate_session_id(),
        model="fake-model-1",
        max_budget_usd=1.0,
        max_turns=10,
        allowed_tools=("Bash", "Read"),
        extra_env=env_overrides,
        wall_clock_timeout_s=wall_clock_timeout_s,
        binary=str(FAKE_CLAUDE),
    )
    return invocation, env_overrides


async def _run(inv: DispatchInvocation, env_overrides: dict[str, str]):
    """Apply env overrides (including the PATH to find python) and invoke."""
    # Ensure PATH is in the whitelist so the fake python shebang resolves.
    # The fake uses #!/usr/bin/env python3.
    original = dict(os.environ)
    try:
        os.environ.update(env_overrides)
        return await invoke(inv)
    finally:
        os.environ.clear()
        os.environ.update(original)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def test_generate_session_id_returns_uuid4():
    sid = generate_session_id()
    assert len(sid) == 36
    assert sid.count("-") == 4


def test_build_argv_contains_required_flags():
    inv = DispatchInvocation(
        prompt="hello",
        cwd=Path("/tmp"),
        session_id="test-sid",
        model="fake-model",
        max_budget_usd=0.50,
        max_turns=5,
        allowed_tools=("Bash", "Read"),
    )
    argv = build_argv(inv)
    assert argv[0] == "claude"
    assert "--bare" in argv
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--session-id" in argv
    assert "test-sid" in argv
    assert "--max-budget-usd" in argv
    assert "0.50" in argv
    assert "--max-turns" in argv
    assert "5" in argv
    assert "--model" in argv
    assert "fake-model" in argv
    assert "-p" in argv
    assert "hello" in argv
    # Allowed tools joined by comma.
    assert "Bash,Read" in argv


def test_build_argv_respects_binary_override():
    inv = DispatchInvocation(
        prompt="x",
        cwd=Path("/tmp"),
        session_id="s",
        model="m",
        max_budget_usd=0.1,
        max_turns=1,
        allowed_tools=(),
        binary="/opt/custom/claude",
    )
    argv = build_argv(inv)
    assert argv[0] == "/opt/custom/claude"


def test_build_env_only_whitelists_known_vars(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/user")
    monkeypatch.setenv("SECRET_AWS_KEY", "leak-if-passed")
    env = build_env()
    assert env.get("PATH") == "/usr/bin"
    assert env.get("HOME") == "/home/user"
    assert "SECRET_AWS_KEY" not in env


def test_build_env_passes_through_extra(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_env(extra={"FOO": "bar"})
    assert env["FOO"] == "bar"


def test_build_env_respects_explicit_whitelist(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("BASH_DEFAULT_TIMEOUT_MS", "300000")
    env = build_env(whitelist=["BASH_DEFAULT_TIMEOUT_MS"])
    assert env.get("BASH_DEFAULT_TIMEOUT_MS") == "300000"


def test_build_env_injects_api_key_when_given():
    env = build_env(anthropic_api_key="sk-test-key")
    assert env["ANTHROPIC_API_KEY"] == "sk-test-key"


def test_build_env_api_key_not_injected_when_none():
    env = build_env(anthropic_api_key=None)
    assert "ANTHROPIC_API_KEY" not in env


# ---------------------------------------------------------------------------
# Scenario: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_success(tmp_path: Path):
    inv, env = _invocation("happy", cwd=tmp_path)
    result = await _run(inv, env)
    assert result.classification is Classification.SUCCESS
    assert result.exit_code == 0
    assert result.trailing_line is None
    assert result.cost_usd == 0.02
    # Events include init + assistant + result.
    types = [e.get("type") for e in result.events]
    assert "system" in types
    assert "assistant" in types
    assert "result" in types


@pytest.mark.asyncio
async def test_happy_path_session_id_echoes(tmp_path: Path):
    inv, env = _invocation("happy", cwd=tmp_path)
    result = await _run(inv, env)
    assert result.session_id == inv.session_id
    # All events carry the same session_id.
    for evt in result.events:
        assert evt.get("session_id") == inv.session_id


# ---------------------------------------------------------------------------
# Scenario: rate-limit retry that succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_retry_ok_classifies_success(tmp_path: Path):
    inv, env = _invocation("rate_limit_retry_ok", cwd=tmp_path)
    result = await _run(inv, env)
    assert result.classification is Classification.SUCCESS
    assert result.exit_code == 0
    # Retry events are present.
    retries = [
        e for e in result.events
        if e.get("type") == "system" and e.get("subtype") == "api_retry"
    ]
    assert len(retries) >= 2


# ---------------------------------------------------------------------------
# Scenario: rate-limit exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_exhausted_is_retryable_transient(tmp_path: Path):
    inv, env = _invocation("rate_limit_exhausted", cwd=tmp_path)
    result = await _run(inv, env)
    # Exit code is non-zero, but classified as retryable because we
    # saw >=3 rate-limit retries in the event stream.
    assert result.exit_code == 1
    assert result.classification is Classification.RETRYABLE_TRANSIENT


# ---------------------------------------------------------------------------
# Scenario: exit 143 (Bash-timeout process-group SIGTERM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_143_is_retryable_transient(tmp_path: Path):
    """Exit 143 = 128 + SIGTERM. The claude-code Bash-timeout bug.

    Supervisor must treat it as retryable, not failed.
    """
    inv, env = _invocation("exit_143", cwd=tmp_path)
    result = await _run(inv, env)
    # The fake signals SIGTERM to itself; exit code is -15 (signal) or
    # 143 (128 + 15), depending on shell interpretation. Python's
    # asyncio reports negative-signal for signaled subprocesses.
    assert result.exit_code in (-signal_num_for_sigterm(), 143)
    assert result.classification is Classification.RETRYABLE_TRANSIENT


def signal_num_for_sigterm() -> int:
    import signal
    return signal.SIGTERM


# ---------------------------------------------------------------------------
# Scenario: truncated JSON on last line
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncated_json_does_not_crash(tmp_path: Path):
    """A partial final line (no closing brace) must not raise."""
    inv, env = _invocation("truncated_json", cwd=tmp_path)
    result = await _run(inv, env)
    # The earlier complete events were parsed.
    assert len(result.events) >= 2
    # The trailing partial line is surfaced, not parsed.
    assert result.trailing_line is not None
    assert result.trailing_line.startswith("{")


# ---------------------------------------------------------------------------
# Scenario: large event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_event_is_handled(tmp_path: Path):
    """A 200KB event must not raise LimitOverrunError (limit=1MB)."""
    inv, env = _invocation("large_event", cwd=tmp_path)
    result = await _run(inv, env)
    assert result.classification is Classification.SUCCESS
    # Confirm the large event actually landed.
    assistants = [e for e in result.events if e.get("type") == "assistant"]
    assert any(
        len(e.get("message", {}).get("content", [{}])[0].get("text", "")) > 100_000
        for e in assistants
    )


# ---------------------------------------------------------------------------
# Scenario: hung process + wall-clock timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hung_process_hits_timeout_and_is_killed(tmp_path: Path):
    """Wall-clock timeout must kill the process group."""
    inv, env = _invocation("hung", cwd=tmp_path, wall_clock_timeout_s=2.0)
    result = await _run(inv, env)
    assert result.classification is Classification.TIMEOUT
    # Elapsed is bounded by timeout + cleanup grace (~5s max).
    assert result.wall_clock_seconds < 10.0
    # Process group was killed.
    assert result.exit_code is not None


# ---------------------------------------------------------------------------
# Scenario: noisy stderr, concurrent drain works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stderr_garbage_does_not_deadlock(tmp_path: Path):
    """~100KB of stderr + normal stream-json must not deadlock.

    If the supervisor drained stdout only, the child would block on
    stderr after ~64KB (OS pipe buffer).
    """
    inv, env = _invocation("stderr_garbage", cwd=tmp_path, wall_clock_timeout_s=10.0)
    result = await _run(inv, env)
    assert result.classification is Classification.SUCCESS
    # Stderr was captured (but capped at 64KB per the drain logic).
    assert len(result.stderr_text) > 1000


# ---------------------------------------------------------------------------
# Env whitelist: parent leak prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_whitelist_excludes_parent_secrets(tmp_path: Path, monkeypatch):
    """A parent SECRET_* env var must not appear in the child."""
    monkeypatch.setenv("SECRET_DATABASE_URL", "postgres://leak")
    inv, env = _invocation("echo_env", cwd=tmp_path)
    result = await _run(inv, env)

    # The fake echoes its env as the assistant message.
    assistant = next(
        (e for e in result.events if e.get("type") == "assistant"),
        None,
    )
    assert assistant is not None
    env_blob = assistant["message"]["content"][0]["text"]
    assert "SECRET_DATABASE_URL" not in env_blob
    assert "postgres://leak" not in env_blob


@pytest.mark.asyncio
async def test_env_whitelist_passes_path_and_extra(tmp_path: Path, monkeypatch):
    """PATH is whitelisted; extra_env values reach the child."""
    inv, env = _invocation(
        "echo_env",
        cwd=tmp_path,
        extra_env={"OXI_TASK_ID": "t0-1"},
    )
    result = await _run(inv, env)

    assistant = next(
        (e for e in result.events if e.get("type") == "assistant"),
        None,
    )
    assert assistant is not None
    env_blob = assistant["message"]["content"][0]["text"]
    assert "OXI_TASK_ID" in env_blob
    assert "t0-1" in env_blob
    assert "PATH" in env_blob


# ---------------------------------------------------------------------------
# Process-group isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawned_subprocess_is_in_its_own_session(tmp_path: Path):
    """The spawned process must be a session leader (pgid == pid)."""
    # We can't easily verify this from inside the test — the process is
    # already exited by the time we'd query. Instead, verify indirectly:
    # a hung process must be killable via the PGID without affecting
    # the test runner. The hung-timeout test above covers this path,
    # since it requires os.killpg to work correctly on a different PGID
    # than our test process.
    inv, env = _invocation("hung", cwd=tmp_path, wall_clock_timeout_s=1.0)
    # Record our own PID before; if killpg affected us we'd crash.
    own_pid = os.getpid()
    result = await _run(inv, env)
    assert result.classification is Classification.TIMEOUT
    # Test process still running.
    assert os.getpid() == own_pid


# ---------------------------------------------------------------------------
# result_event helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_event_helper(tmp_path: Path):
    inv, env = _invocation("happy", cwd=tmp_path)
    result = await _run(inv, env)
    evt = result.result_event()
    assert evt is not None
    assert evt["type"] == "result"
    assert evt["total_cost_usd"] == 0.02


@pytest.mark.asyncio
async def test_result_event_helper_returns_none_when_absent(tmp_path: Path):
    inv, env = _invocation("rate_limit_exhausted", cwd=tmp_path)
    result = await _run(inv, env)
    # Exhausted scenario never emits a result event.
    assert result.result_event() is None


# ---------------------------------------------------------------------------
# Python version guard
# ---------------------------------------------------------------------------


def test_python_version_supports_async():
    """Sanity: we need Python 3.11+ for asyncio.TaskGroup + TimeoutError alias."""
    assert sys.version_info >= (3, 11)
