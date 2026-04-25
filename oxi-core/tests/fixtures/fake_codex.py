#!/usr/bin/env python3
"""Fake ``codex`` binary for ``CodexCliAdapter`` tests.

Usage::

    FAKE_CODEX_SCENARIO=happy fake_codex.py exec --json -- "some prompt"

Scenarios are selected via ``FAKE_CODEX_SCENARIO``.  Each scenario
emits synthetic JSONL events to stdout and exits with a specific code.
All scenarios accept (and ignore) the ``exec --json --`` prefix so the
real adapter's ``_build_argv`` output can be passed through unchanged.

This file mirrors the structure of ``fake_claude.py`` to keep test
infrastructure consistent across adapters.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

_VERSION = os.environ.get("FAKE_CODEX_VERSION", "0.1.0")

# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def _emit(event: dict) -> None:
    """Write one JSON event to stdout on its own line and flush."""
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _emit_raw(line: str, *, add_newline: bool = True) -> None:
    """Write a raw (possibly non-JSON) line to stdout."""
    sys.stdout.write(line + ("\n" if add_newline else ""))
    sys.stdout.flush()


def _start_event() -> dict:
    return {"type": "start", "version": _VERSION}


def _output_event(text: str) -> dict:
    return {"type": "output", "text": text}


def _result_event(exit_code: int = 0) -> dict:
    return {"type": "result", "exit_code": exit_code}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_happy() -> int:
    """Normal success: start → output → result, exit 0."""
    _emit(_start_event())
    _emit(_output_event("Task completed successfully."))
    _emit(_result_event(0))
    return 0


def scenario_nonzero_exit() -> int:
    """Codex exits 1 after emitting events — failure path."""
    _emit(_start_event())
    _emit(_output_event("Something went wrong."))
    _emit(_result_event(1))
    return 1


def scenario_truncated_json() -> int:
    """Emit valid events then a truncated final line (SIGKILL simulation)."""
    _emit(_start_event())
    _emit(_output_event("writing output"))
    _emit_raw('{"type": "result", "exit_co', add_newline=False)
    return 0


def scenario_large_output() -> int:
    """Emit a >100 KB output event to exercise StreamReader limit."""
    _emit(_start_event())
    _emit(_output_event("x" * 200_000))
    _emit(_result_event(0))
    return 0


def scenario_hung() -> int:
    """Block forever — exercises wall-clock timeout path."""
    _emit(_start_event())
    time.sleep(3600)
    return 0


def scenario_stderr_noise() -> int:
    """Write ~100 KB to stderr concurrently with stdout to verify concurrent drain."""
    _emit(_start_event())
    sys.stderr.write("codex stderr noise\n" * 5000)
    sys.stderr.flush()
    _emit(_output_event("done despite noise"))
    _emit(_result_event(0))
    return 0


def scenario_exit_sigterm() -> int:
    """Send SIGTERM to itself — exercises process-group kill path."""
    _emit(_start_event())
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGTERM)
    return 0


def scenario_echo_env() -> int:
    """Emit the process env as an output event — for whitelist tests."""
    _emit(_start_event())
    _emit(_output_event(json.dumps(dict(os.environ), sort_keys=True)))
    _emit(_result_event(0))
    return 0


SCENARIOS: dict[str, object] = {
    "happy": scenario_happy,
    "nonzero_exit": scenario_nonzero_exit,
    "truncated_json": scenario_truncated_json,
    "large_output": scenario_large_output,
    "hung": scenario_hung,
    "stderr_noise": scenario_stderr_noise,
    "exit_sigterm": scenario_exit_sigterm,
    "echo_env": scenario_echo_env,
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    scenario_name = os.environ.get("FAKE_CODEX_SCENARIO", "happy")
    scenario = SCENARIOS.get(scenario_name)
    if scenario is None:
        sys.stderr.write(
            f"fake_codex: unknown scenario {scenario_name!r}. "
            f"Known: {sorted(SCENARIOS)}\n"
        )
        return 2
    return scenario()


if __name__ == "__main__":
    raise SystemExit(main())
