#!/usr/bin/env python3
"""Fake `claude` binary for dispatch tests.

Usage:

    FAKE_CLAUDE_SCENARIO=happy fake_claude.py -p "..." --output-format stream-json ...

Scenarios are selected via the `FAKE_CLAUDE_SCENARIO` env var. Each
scenario emits synthetic stream-json events to stdout and exits with a
specific code. All scenarios accept (and ignore) the same flags the
real `claude -p` takes, so the dispatcher code can pass whatever flags
it needs without caring whether the binary is real or fake.

This file lives under `tests/fixtures/` so it's packaged with tests
but never shipped to users. Tests prepend a tmp dir containing a
symlink or shim to this file onto PATH.

The scenarios reproduce every bug-class signature documented in
~/.claude-setup/memory/auto/semantic/pattern_fake_binary_for_dispatch_tests.md
and pattern_subprocess_process_group_for_dispatch.md.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import uuid

# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def _emit(event: dict) -> None:
    """Write one JSON event to stdout on its own line and flush."""
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _emit_raw(line: str, *, add_newline: bool = True) -> None:
    """Write raw bytes to stdout (used for truncated-line scenarios)."""
    sys.stdout.write(line + ("\n" if add_newline else ""))
    sys.stdout.flush()


def _init_event(session_id: str) -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "model": os.environ.get("FAKE_CLAUDE_MODEL", "fake-model-1"),
        "tools": ["Bash", "Read", "Edit", "Write"],
    }


def _assistant_event(text: str, session_id: str) -> dict:
    return {
        "type": "assistant",
        "session_id": session_id,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _result_event(session_id: str, cost_usd: float = 0.02) -> dict:
    return {
        "type": "result",
        "session_id": session_id,
        "result": "done",
        "total_cost_usd": cost_usd,
        "duration_ms": 1000,
    }


def _retry_event(session_id: str, attempt: int) -> dict:
    return {
        "type": "system",
        "subtype": "api_retry",
        "session_id": session_id,
        "attempt": attempt,
        "max_retries": 3,
        "retry_delay_ms": 100,
        "error": "rate_limit",
        "error_status": 429,
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_happy(session_id: str) -> int:
    """Normal successful dispatch: init → assistant → result, exit 0."""
    _emit(_init_event(session_id))
    _emit(_assistant_event("I'll add a greet function.", session_id))
    _emit(_result_event(session_id))
    return 0


def scenario_rate_limit_retry_ok(session_id: str) -> int:
    """Transient rate limits that resolve: init → retry × 2 → result, exit 0."""
    _emit(_init_event(session_id))
    _emit(_retry_event(session_id, 1))
    time.sleep(0.05)
    _emit(_retry_event(session_id, 2))
    time.sleep(0.05)
    _emit(_assistant_event("finally through", session_id))
    _emit(_result_event(session_id))
    return 0


def scenario_rate_limit_exhausted(session_id: str) -> int:
    """Rate-limit exhausted: init → retry × max, exit 1."""
    _emit(_init_event(session_id))
    for attempt in (1, 2, 3):
        _emit(_retry_event(session_id, attempt))
        time.sleep(0.02)
    sys.stderr.write("fake_claude: rate limit exhausted\n")
    return 1


def scenario_exit_143(session_id: str) -> int:
    """Simulate the claude-code Bash-timeout process-group SIGTERM bug.

    Real claude exits 143 when a Bash tool timeout kills the process
    group including claude itself. We reproduce this by emitting some
    events, then raising SIGTERM to ourselves. The supervisor should
    observe exit code 143 (= 128 + SIGTERM) and classify it as
    retryable-transient.
    """
    _emit(_init_event(session_id))
    _emit(_assistant_event("started work, then bash timed out", session_id))
    # Flush before signaling so the supervisor sees the partial output.
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGTERM)
    # Unreachable — SIGTERM terminates the process.
    return 0


def scenario_truncated_json(session_id: str) -> int:
    """Emit valid events then a truncated final line.

    Exercises the supervisor's ability to swallow JSONDecodeError on
    the last line without crashing.
    """
    _emit(_init_event(session_id))
    _emit(_assistant_event("writing some events", session_id))
    # Valid object followed by a partial object with no closing brace.
    _emit_raw('{"type": "assistant", "session_id": "' + session_id + '"', add_newline=False)
    # Exit immediately — no newline, no closing brace.
    return 0


def scenario_large_event(session_id: str) -> int:
    """Emit one >100KB event to exercise StreamReader limit handling.

    A real claude can emit a giant `tool_use` result payload. Default
    asyncio StreamReader limit is 64KB per line; supervisor must pass
    `limit=1024*1024` to handle realistic payloads.
    """
    _emit(_init_event(session_id))
    big_text = "x" * 200_000
    _emit(_assistant_event(big_text, session_id))
    _emit(_result_event(session_id))
    return 0


def scenario_hung(session_id: str) -> int:
    """Sleep forever — exercises supervisor wall-clock timeout.

    Emits init so the supervisor has proof the process started, then
    blocks indefinitely. Supervisor must kill the process group.
    """
    _emit(_init_event(session_id))
    # Sleep for a long time — supervisor's wall-clock timeout should fire first.
    time.sleep(3600)
    return 0


def scenario_stderr_garbage(session_id: str) -> int:
    """Write non-JSON to stderr concurrently with valid stream-json on stdout.

    Supervisor must not try to parse stderr as stream-json, and must
    drain stderr concurrently or the child will block on a full stderr
    pipe (64KB OS buffer).
    """
    _emit(_init_event(session_id))
    # Dump ~100KB to stderr — exercises the concurrent-drain requirement.
    sys.stderr.write("stderr line\n" * 5000)
    sys.stderr.flush()
    _emit(_assistant_event("finished despite noisy stderr", session_id))
    _emit(_result_event(session_id))
    return 0


def scenario_echo_env(session_id: str) -> int:
    """Emit the process environment as a single assistant event.

    Used by env-whitelist tests: the supervisor spawns with a specific
    env dict; this scenario reports back what the child actually saw.
    """
    _emit(_init_event(session_id))
    env_text = json.dumps(dict(os.environ), sort_keys=True)
    _emit(_assistant_event(env_text, session_id))
    _emit(_result_event(session_id))
    return 0


def scenario_critic_approve(session_id: str) -> int:
    """Emit a critic APPROVE response shaped like a real review.

    Tests ClaudeCriticBackend end-to-end: assistant event text must be
    the exact two-line APPROVE format ``parse_critic_response``
    consumes.
    """
    _emit(_init_event(session_id))
    _emit(_assistant_event("APPROVE\nclean diff, tests pass", session_id))
    _emit(_result_event(session_id))
    return 0


def scenario_critic_reject(session_id: str) -> int:
    """Emit a critic REJECT response."""
    _emit(_init_event(session_id))
    _emit(_assistant_event("REJECT\ncommented-out tests", session_id))
    _emit(_result_event(session_id))
    return 0


def scenario_critic_garbage(session_id: str) -> int:
    """Emit an unparseable critic response — should default to REJECT."""
    _emit(_init_event(session_id))
    _emit(_assistant_event("I think this diff is fine.", session_id))
    _emit(_result_event(session_id))
    return 0


def scenario_exit_1_after_pr(session_id: str) -> int:
    """Simulate a post-success exit-1 (pre-commit hook tail or gh pr create quirk).

    The worker committed, pushed, and opened a PR successfully, but the
    process exited non-zero at the very end (e.g. a pre-commit hook's
    trailing stderr, or gh pr create returning 1 despite opening the PR).

    Dispatchers that only look at the exit code misclassify this as
    FAILED. The false-failure relaxation in dispatch.py should upgrade
    it to SUCCESS when the branch is ahead of origin and a PR exists.
    """
    _emit(_init_event(session_id))
    _emit(_assistant_event(
        "Committed, pushed, and opened PR. Exiting with pre-commit hook noise.",
        session_id,
    ))
    # No result event — consistent with real claude exiting non-zero before
    # the framework emits a result.
    sys.stderr.write("fake_claude: pre-commit hook exited 1 (noise)\n")
    return 1


SCENARIOS: dict[str, callable] = {
    "happy": scenario_happy,
    "rate_limit_retry_ok": scenario_rate_limit_retry_ok,
    "rate_limit_exhausted": scenario_rate_limit_exhausted,
    "exit_143": scenario_exit_143,
    "truncated_json": scenario_truncated_json,
    "large_event": scenario_large_event,
    "hung": scenario_hung,
    "stderr_garbage": scenario_stderr_garbage,
    "echo_env": scenario_echo_env,
    "critic_approve": scenario_critic_approve,
    "critic_reject": scenario_critic_reject,
    "critic_garbage": scenario_critic_garbage,
    "exit_1_after_pr": scenario_exit_1_after_pr,
}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def _extract_session_id(argv: list[str]) -> str:
    """Pull the session ID from argv if provided via --session-id, else generate."""
    for i, arg in enumerate(argv):
        if arg == "--session-id" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--session-id="):
            return arg.split("=", 1)[1]
    return str(uuid.uuid4())


def main() -> int:
    scenario_name = os.environ.get("FAKE_CLAUDE_SCENARIO", "happy")
    scenario = SCENARIOS.get(scenario_name)
    if scenario is None:
        sys.stderr.write(
            f"fake_claude: unknown scenario {scenario_name!r}. "
            f"Known: {sorted(SCENARIOS)}\n"
        )
        return 2

    session_id = _extract_session_id(sys.argv[1:])
    return scenario(session_id)


if __name__ == "__main__":
    raise SystemExit(main())
