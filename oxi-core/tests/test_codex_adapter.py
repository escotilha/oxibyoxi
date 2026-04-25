"""Tests for ``oxi_core.v3.agentic.codex`` (``CodexCliAdapter``).

All tests run against the fake codex binary at ``tests/fixtures/fake_codex.py``.
No real ``codex`` binary is invoked.  The fake reproduces the same
JSONL stream behaviour documented in the fixture file itself.

Test organisation
-----------------
1. Protocol conformance
2. Version-pin smoke test (init-time format check)
3. ``_build_argv`` — pure-function assertions
4. ``spawn()`` subprocess scenarios (async)
5. Env whitelist passthrough
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from oxi_core.v3.agentic import AgenticAdapter, CodexCliAdapter, CodexFormatDriftError
from oxi_core.v3.agentic.codex import (
    _FIXTURE_REQUIRED_FIELDS,
    _SMOKE_FIXTURE_LINES,
    CodexSpawnResult,
)

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CODEX = FIXTURES / "fake_codex.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**kwargs: object) -> CodexCliAdapter:
    """Create a CodexCliAdapter pointing at the fake binary."""
    defaults: dict[str, object] = dict(binary=str(FAKE_CODEX), version="0.1.0")
    defaults.update(kwargs)
    return CodexCliAdapter(**defaults)  # type: ignore[arg-type]


async def _run_spawn(
    adapter: CodexCliAdapter,
    scenario: str,
    *,
    tmp_path: Path,
    wall_clock_timeout_s: float = 30.0,
    extra_env: dict[str, str] | None = None,
) -> CodexSpawnResult:
    """Run a spawn scenario and return the result.

    ``FAKE_CODEX_SCENARIO`` is forwarded to the child via ``extra_env``
    so it survives the env whitelist applied by ``build_env``.
    """
    child_env: dict[str, str] = {"FAKE_CODEX_SCENARIO": scenario}
    if extra_env:
        child_env.update(extra_env)
    return await adapter.spawn(
        "test prompt",
        tmp_path,
        extra_env=child_env,
        wall_clock_timeout_s=wall_clock_timeout_s,
    )


# ---------------------------------------------------------------------------
# 1. Protocol conformance
# ---------------------------------------------------------------------------


def test_codex_adapter_implements_agentic_adapter_protocol():
    """``CodexCliAdapter`` must satisfy the ``AgenticAdapter`` protocol."""
    adapter = _make_adapter()
    assert isinstance(adapter, AgenticAdapter)


def test_pinned_version_returns_configured_string():
    adapter = _make_adapter(version="0.2.5")
    assert adapter.pinned_version() == "0.2.5"


def test_binary_path_returns_configured_path():
    adapter = _make_adapter()
    assert adapter.binary_path() == str(FAKE_CODEX)


# ---------------------------------------------------------------------------
# 2. Version-pin smoke test
# ---------------------------------------------------------------------------


def test_smoke_test_passes_with_valid_fixture():
    """Default init must not raise; the fixture is known-good."""
    # No exception → smoke test passed.
    adapter = _make_adapter()
    assert adapter is not None


def test_smoke_test_raises_on_invalid_json(monkeypatch):
    """A non-JSON fixture line must raise ``CodexFormatDriftError``."""
    bad_lines = (
        'not json at all',
        '{"type": "output", "text": "ok"}',
        '{"type": "result", "exit_code": 0}',
    )
    monkeypatch.setattr(
        "oxi_core.v3.agentic.codex._SMOKE_FIXTURE_LINES", bad_lines
    )
    with pytest.raises(CodexFormatDriftError) as exc_info:
        _make_adapter(version="1.9.9")
    assert "1.9.9" in str(exc_info.value)
    assert "parse" in str(exc_info.value).lower()


def test_smoke_test_raises_on_missing_field(monkeypatch):
    """A fixture line that drops a required field must raise ``CodexFormatDriftError``."""
    # Remove the "version" field from the start event.
    bad_lines = (
        '{"type": "start"}',  # missing "version"
        '{"type": "output", "text": "hello"}',
        '{"type": "result", "exit_code": 0}',
    )
    monkeypatch.setattr(
        "oxi_core.v3.agentic.codex._SMOKE_FIXTURE_LINES", bad_lines
    )
    with pytest.raises(CodexFormatDriftError) as exc_info:
        _make_adapter(version="2.0.0")
    err = str(exc_info.value)
    assert "2.0.0" in err
    assert "version" in err


def test_codex_format_drift_error_exposes_binary_version():
    err = CodexFormatDriftError("3.1.4", "some detail")
    assert err.binary_version == "3.1.4"
    assert "3.1.4" in str(err)
    assert "some detail" in str(err)


def test_smoke_fixture_and_required_fields_have_same_length():
    """Internal consistency: fixture lines and required-fields tuple must be paired."""
    assert len(_SMOKE_FIXTURE_LINES) == len(_FIXTURE_REQUIRED_FIELDS)


def test_smoke_fixture_lines_are_valid_json():
    """Every fixture line must be valid JSON (sanity check)."""
    import json
    for i, line in enumerate(_SMOKE_FIXTURE_LINES):
        parsed = json.loads(line)
        assert isinstance(parsed, dict), f"Line {i} must parse to a dict"


def test_smoke_fixture_covers_required_event_types():
    """Fixture must include start, output, and result events."""
    import json
    types = {json.loads(line)["type"] for line in _SMOKE_FIXTURE_LINES}
    assert "start" in types
    assert "output" in types
    assert "result" in types


# ---------------------------------------------------------------------------
# 3. ``_build_argv`` — pure-function checks
# ---------------------------------------------------------------------------


def test_build_argv_uses_exec_json_subcommand():
    adapter = _make_adapter()
    argv = adapter._build_argv("write a test")
    assert argv[0] == str(FAKE_CODEX)
    assert "exec" in argv
    assert "--json" in argv


def test_build_argv_passes_prompt_after_separator():
    adapter = _make_adapter()
    prompt = "add a greet function"
    argv = adapter._build_argv(prompt)
    # Prompt must appear after the "--" separator.
    sep_idx = argv.index("--")
    assert argv[sep_idx + 1] == prompt


def test_build_argv_never_uses_shell_metacharacters():
    """Argv must be a list, not a string — shell=True guard."""
    adapter = _make_adapter()
    argv = adapter._build_argv("some prompt; rm -rf /")
    assert isinstance(argv, list)
    # The dangerous prompt is one element, not split by the shell.
    assert argv[-1] == "some prompt; rm -rf /"


# ---------------------------------------------------------------------------
# 4. ``spawn()`` subprocess scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_exits_zero(tmp_path: Path):
    adapter = _make_adapter()
    result = await _run_spawn(adapter, "happy", tmp_path=tmp_path)
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_happy_path_emits_three_events(tmp_path: Path):
    adapter = _make_adapter()
    result = await _run_spawn(adapter, "happy", tmp_path=tmp_path)
    types = [e.get("type") for e in result.events]
    assert "start" in types
    assert "output" in types
    assert "result" in types


@pytest.mark.asyncio
async def test_happy_path_no_trailing_line(tmp_path: Path):
    adapter = _make_adapter()
    result = await _run_spawn(adapter, "happy", tmp_path=tmp_path)
    assert result.trailing_line is None


@pytest.mark.asyncio
async def test_nonzero_exit_code_surfaced(tmp_path: Path):
    adapter = _make_adapter()
    result = await _run_spawn(adapter, "nonzero_exit", tmp_path=tmp_path)
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_truncated_json_does_not_crash(tmp_path: Path):
    """A partial final line must not raise — it is stored as ``trailing_line``."""
    adapter = _make_adapter()
    result = await _run_spawn(adapter, "truncated_json", tmp_path=tmp_path)
    # Earlier complete events were parsed.
    assert len(result.events) >= 1
    # Trailing partial line is preserved.
    assert result.trailing_line is not None
    assert result.trailing_line.startswith("{")


@pytest.mark.asyncio
async def test_large_output_handled_without_error(tmp_path: Path):
    """A 200 KB output event must not raise ``LimitOverrunError``."""
    adapter = _make_adapter()
    result = await _run_spawn(adapter, "large_output", tmp_path=tmp_path)
    assert result.exit_code == 0
    outputs = [e for e in result.events if e.get("type") == "output"]
    assert any(len(e.get("text", "")) > 100_000 for e in outputs)


@pytest.mark.asyncio
async def test_hung_process_times_out(tmp_path: Path):
    """A hung codex process must be killed when the wall-clock timeout fires."""
    adapter = _make_adapter()
    result = await _run_spawn(
        adapter, "hung", tmp_path=tmp_path, wall_clock_timeout_s=2.0
    )
    # Process was killed — exit code is non-None (either negative signal
    # or 143 depending on the OS).
    assert result.exit_code is not None
    # Elapsed time is bounded by timeout + cleanup grace.
    assert result.wall_clock_seconds < 12.0


@pytest.mark.asyncio
async def test_stderr_noise_does_not_deadlock(tmp_path: Path):
    """~100 KB of stderr + normal stdout must not deadlock."""
    adapter = _make_adapter()
    result = await _run_spawn(
        adapter, "stderr_noise", tmp_path=tmp_path, wall_clock_timeout_s=15.0
    )
    assert result.exit_code == 0
    assert len(result.stderr_text) > 1000


@pytest.mark.asyncio
async def test_wall_clock_time_is_recorded(tmp_path: Path):
    adapter = _make_adapter()
    result = await _run_spawn(adapter, "happy", tmp_path=tmp_path)
    assert result.wall_clock_seconds > 0.0


# ---------------------------------------------------------------------------
# 5. Env whitelist passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_whitelist_blocks_parent_secrets(tmp_path: Path, monkeypatch):
    """A parent ``SECRET_*`` var must not reach the codex child process."""
    monkeypatch.setenv("SECRET_LEAK", "should-not-appear")
    adapter = _make_adapter()
    result = await _run_spawn(adapter, "echo_env", tmp_path=tmp_path)
    output_events = [e for e in result.events if e.get("type") == "output"]
    assert output_events, "echo_env must emit an output event"
    env_blob = output_events[0]["text"]
    assert "SECRET_LEAK" not in env_blob
    assert "should-not-appear" not in env_blob


@pytest.mark.asyncio
async def test_extra_env_reaches_child(tmp_path: Path):
    """Values in ``extra_env`` must appear in the child's environment."""
    adapter = _make_adapter()
    result = await _run_spawn(
        adapter,
        "echo_env",
        tmp_path=tmp_path,
        extra_env={"OXI_CODEX_TEST_KEY": "sentinel-value-xyz"},
    )
    output_events = [e for e in result.events if e.get("type") == "output"]
    assert output_events
    env_blob = output_events[0]["text"]
    assert "OXI_CODEX_TEST_KEY" in env_blob
    assert "sentinel-value-xyz" in env_blob


@pytest.mark.asyncio
async def test_process_group_isolation(tmp_path: Path):
    """A hung codex subprocess must be killable without affecting the test runner."""
    own_pid = os.getpid()
    adapter = _make_adapter()
    result = await _run_spawn(
        adapter, "hung", tmp_path=tmp_path, wall_clock_timeout_s=1.0
    )
    # Test process is still alive.
    assert os.getpid() == own_pid
    # Codex was killed.
    assert result.exit_code is not None
