"""Subprocess wrapper for ``claude -p``.

This module is the one place in oxi that reaches into asyncio-subprocess
primitives. Every other module treats dispatch as "give me a
DispatchResult for this task" and never touches process groups, pipe
buffers, or env whitelists directly.

Design goals, each grounded in a documented prior incident:

1.  **Process-group isolation** via ``start_new_session=True``. Required
    to survive claude-code issue #45717 (Bash-tool timeout sends
    SIGTERM to the process group, killing claude itself). Without
    isolation, the supervisor can be taken down too.

2.  **Concurrent stdout + stderr drain.** OS pipe buffer is 64KB on
    macOS; a single assistant message with a large tool result easily
    exceeds that. Draining only stdout deadlocks the child on stderr
    write.

3.  **StreamReader limit of 1MB.** A single stream-json event can be
    large (tool results with file contents). Default 64KB triggers
    ``LimitOverrunError`` in realistic workloads.

4.  **Env whitelist.** Nested claude sessions inherit ``CLAUDE_*`` /
    ``ANTHROPIC_*`` env vars from the outer process, which silently
    overrides per-dispatch settings. Also a security boundary:
    compromised tool calls should not see unrelated secrets.

5.  **Truncated-JSON tolerance.** On SIGKILL the last stdout line may
    be partial. The stream reader swallows ``JSONDecodeError`` on the
    trailing line only and reports the raw bytes in the result.

6.  **Exit code classification.** 0 = success, 143 = retryable
    transient (the known Bash-timeout bug), everything else = failed.
    The supervisor reads ``DispatchResult.classification`` and decides
    whether to retry or mark the task failed.

7.  **Wall-clock timeout, not inter-event timeout.** There is no
    heartbeat event in stream-json; long tool executions produce
    silence. The supervisor sets a total wall-clock budget; if
    exceeded, process group is killed and result is TIMEOUT.

Subprocess invocation uses argv-form (``create_subprocess_exec``) —
never ``shell=True`` or string-concatenated commands — so claude's
prompt argument cannot be interpreted as shell syntax even if it
contains backticks, semicolons, or other metacharacters.

The module does NOT:

- Read or write the task database (that is ``dispatch.py``'s job).
- Compose prompts (that is ``prompts.py``).
- Decide which host runs the subprocess (that is ``dispatch_pool.py``).
- Know what a task is. It just runs a subprocess with a prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# StreamReader buffer size for reading stream-json events. Large enough
# to hold a realistic tool-use result payload without raising
# LimitOverrunError; small enough to bound per-process memory.
STREAM_READER_LIMIT = 1024 * 1024  # 1 MB

# Exit codes with specific meaning.
EXIT_SUCCESS = 0
EXIT_SIGTERM = 143  # 128 + SIGTERM; claude-code issue #45717

# Env vars always passed through. Anything outside this list is
# stripped unless the caller explicitly adds it.
_BASE_ENV_WHITELIST: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
})


class Classification(Enum):
    """How the supervisor should interpret a dispatch outcome.

    - SUCCESS: claude exited 0 and emitted a result event.
    - RETRYABLE_TRANSIENT: exit 143 (SIGTERM bug), or rate-limit exhaustion.
      Task stays in a recoverable state; retry on next tick.
    - FAILED: hard failure. Task goes to failed/blocked.
    - TIMEOUT: supervisor wall-clock budget exceeded; process killed.
    """

    SUCCESS = "success"
    RETRYABLE_TRANSIENT = "retryable_transient"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class DispatchInvocation:
    """Everything needed to spawn one ``claude -p`` process.

    All fields are explicit inputs — no hidden defaults, no adapter
    reads. Callers (``dispatch.py``) compose this from task state +
    adapter + runtime config.
    """

    prompt: str
    cwd: Path
    session_id: str
    model: str
    max_budget_usd: float
    max_turns: int
    allowed_tools: Sequence[str]
    extra_env: dict[str, str] = field(default_factory=dict)
    anthropic_api_key: str | None = None  # injected into child env; bypass whitelist
    wall_clock_timeout_s: float = 1800.0  # 30 min default
    binary: str = "claude"  # overridable for tests
    # When set, the subprocess is invoked over SSH against this alias
    # (must resolve in the operator's ~/.ssh/config). cwd is interpreted
    # on the remote host. extra_env + anthropic_api_key are passed
    # via shell assignment in the remote command, not via the local
    # subprocess env (the env whitelist still applies on the local side).
    ssh_alias: str | None = None
    # Path to the ssh binary on the local machine. Overridable for tests.
    ssh_binary: str = "ssh"


@dataclass
class DispatchResult:
    """Outcome of one ``claude -p`` invocation.

    Attributes:
        classification: what the supervisor should do next.
        exit_code: raw exit code from the subprocess.
        session_id: echo of the invocation session_id, for cross-referencing.
        events: parsed stream-json events, in order received.
        trailing_line: the final stdout line if it was unparseable (partial
            JSON from SIGKILL); None otherwise.
        stderr_text: captured stderr, truncated to 64KB.
        cost_usd: total cost as reported by the result event; 0.0 if none.
        wall_clock_seconds: elapsed time from spawn to exit.
    """

    classification: Classification
    exit_code: int | None
    session_id: str
    events: list[dict] = field(default_factory=list)
    trailing_line: str | None = None
    stderr_text: str = ""
    cost_usd: float = 0.0
    wall_clock_seconds: float = 0.0
    #: True when classification is RETRYABLE_TRANSIENT specifically because
    #: the worker exhausted Claude's rate-limit retry loop. Lets the
    #: dispatcher walk the model fallback chain on retry instead of
    #: blindly trying the same model that just got rate-limited.
    rate_limit_exhausted: bool = False

    def result_event(self) -> dict | None:
        """Return the first stream-json event with type='result', or None."""
        for evt in self.events:
            if evt.get("type") == "result":
                return evt
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_session_id() -> str:
    """Return a fresh UUID4 suitable for ``claude --session-id``."""
    return str(uuid.uuid4())


def build_env(
    extra: dict[str, str] | None = None,
    whitelist: Sequence[str] = (),
    anthropic_api_key: str | None = None,
) -> dict[str, str]:
    """Compose a whitelisted env dict for the subprocess.

    Starts from ``_BASE_ENV_WHITELIST`` intersected with the parent's
    env, adds any names in ``whitelist`` (also from parent), overlays
    ``extra``, and injects ``ANTHROPIC_API_KEY`` if supplied.

    Callers that need to pass through additional vars (e.g.
    ``BASH_DEFAULT_TIMEOUT_MS``) should include their names in
    ``whitelist``.
    """
    extended = set(_BASE_ENV_WHITELIST) | set(whitelist)
    env = {k: v for k, v in os.environ.items() if k in extended}
    if extra:
        env.update(extra)
    if anthropic_api_key is not None:
        env["ANTHROPIC_API_KEY"] = anthropic_api_key
    return env


def build_argv(invocation: DispatchInvocation) -> list[str]:
    """Turn a DispatchInvocation into the exact argv handed to the binary.

    Exposed as a public function so tests can assert on flags without
    running the subprocess.
    """
    allowed_tools_str = ",".join(invocation.allowed_tools)
    return [
        invocation.binary,
        "--bare",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", allowed_tools_str,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--max-turns", str(invocation.max_turns),
        "--max-budget-usd", f"{invocation.max_budget_usd:.2f}",
        "--no-session-persistence",
        "--session-id", invocation.session_id,
        "--exclude-dynamic-system-prompt-sections",
        "--model", invocation.model,
        "-p", invocation.prompt,
    ]


def wrap_with_ssh(
    invocation: DispatchInvocation,
    local_argv: list[str],
    env: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Wrap a local argv + env into an ssh-invocable argv + minimal local env.

    When ``invocation.ssh_alias`` is set, the work needs to run on a
    remote host that the operator has set up in ``~/.ssh/config``.
    The remote command is built as a single shell-quoted string so we
    can prefix env-var assignments + ``cd`` to the remote cwd before
    invoking ``claude``.

    Returns the new (argv, env) pair: argv invokes ssh; env contains
    only what ssh itself needs locally (e.g. SSH_AUTH_SOCK from the
    extended env whitelist).

    The caller is responsible for ensuring the remote host has:
    - the same ``claude`` binary on PATH (or invocation.binary spells
      out an absolute path that exists remotely);
    - any required directory at ``invocation.cwd`` (already
      provisioned by ``worktree_provision`` if remote worktrees are
      managed by the same operator workflow);
    - access to the ANTHROPIC_API_KEY value passed via the env arg.

    Argv-form is preserved end-to-end: the local invocation is
    ``ssh <alias> <single-quoted-shell-string>``; the remote shell
    parses the string. No shell metacharacter from invocation fields
    can escape the shlex.quote because every field is quoted
    individually.
    """
    if invocation.ssh_alias is None:
        return local_argv, env

    # Build the remote command as a sequence of shell tokens, then
    # join with single spaces. shlex.quote handles every field's
    # metacharacters.
    parts: list[str] = []

    # Env-var assignments — prepended to the command so they're set
    # only for this process.
    for key, value in sorted(env.items()):
        # Skip purely-local vars that don't make sense on the remote.
        if key in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
            continue
        parts.append(f"{key}={shlex.quote(value)}")

    # cd to the remote cwd, then exec the binary.
    cwd_quoted = shlex.quote(str(invocation.cwd))
    parts.append(f"cd {cwd_quoted}")
    parts.append("&&")
    parts.extend(shlex.quote(a) for a in local_argv)

    remote_command = " ".join(parts)

    # Local argv: just ssh + alias + the remote command.
    # -T disables pseudo-terminal allocation (we want clean stream-json).
    # -o BatchMode=yes refuses interactive password prompts (fail fast
    # if key auth is broken instead of hanging).
    new_argv = [
        invocation.ssh_binary,
        "-T",
        "-o", "BatchMode=yes",
        invocation.ssh_alias,
        remote_command,
    ]

    # Local env: keep only what ssh needs locally to find the agent
    # and config. Everything else was already inlined into the remote
    # command above.
    local_env: dict[str, str] = {}
    for key in ("PATH", "HOME", "USER", "LANG", "TMPDIR",
                "SSH_AUTH_SOCK", "SSH_AGENT_PID"):
        if key in env:
            local_env[key] = env[key]
        elif key in os.environ:
            local_env[key] = os.environ[key]

    return new_argv, local_env


async def invoke(invocation: DispatchInvocation) -> DispatchResult:
    """Spawn ``claude -p``, stream events, classify the outcome.

    This is the whole subprocess lifecycle in one async function:
    spawn in a new process group, drain stdout + stderr concurrently,
    enforce a wall-clock timeout, classify the exit.
    """
    argv = build_argv(invocation)
    env = build_env(
        extra=invocation.extra_env,
        anthropic_api_key=invocation.anthropic_api_key,
    )

    # If this invocation targets a remote host, wrap argv with ssh and
    # narrow env to the SSH-locally-relevant subset. cwd on the local
    # side becomes irrelevant (None) because cwd is enforced inside
    # the remote command via shell `cd`.
    cwd_arg: str | None = os.fspath(invocation.cwd.resolve())
    if invocation.ssh_alias is not None:
        argv, env = wrap_with_ssh(invocation, argv, env)
        cwd_arg = None

    loop = asyncio.get_running_loop()
    start_time = loop.time()

    # Argv-form subprocess (no shell=True, no string interpolation);
    # ssh wrapping is also argv-form with shlex-quoted remote command.
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd_arg,
        env=env,
        start_new_session=True,
        limit=STREAM_READER_LIMIT,
    )

    # Capture pgid immediately — once the process exits, getpgid fails.
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        # Process died before we could read its PGID. Rare but possible.
        pgid = None

    events: list[dict] = []
    trailing_line: str | None = None
    stderr_chunks: list[bytes] = []

    async def drain_stdout() -> None:
        nonlocal trailing_line
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except asyncio.LimitOverrunError:
                # Single event exceeded STREAM_READER_LIMIT. Drop it
                # loudly — a 1MB line is already pathological.
                stderr_chunks.append(b"oxi: stream-json line exceeded 1MB limit\n")
                continue
            if not line:  # EOF
                return
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            if not decoded:
                continue
            try:
                events.append(json.loads(decoded))
            except json.JSONDecodeError:
                # Only the last line should ever be truncated (SIGKILL
                # mid-write). Hold it; if another line follows, it was
                # not actually trailing and we drop it.
                if trailing_line is not None:
                    # Previous "trailing" was really garbage mid-stream.
                    # Log via stderr bucket and move on.
                    stderr_chunks.append(
                        f"oxi: dropped non-JSON line: {trailing_line[:200]}\n".encode()
                    )
                trailing_line = decoded

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        # Cap total stderr capture at 64KB to bound memory on pathological children.
        cap = 64 * 1024
        collected = 0
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            if collected < cap:
                room = cap - collected
                stderr_chunks.append(chunk[:room])
                collected += min(len(chunk), room)

    # Drain both streams concurrently. If either task crashes, the
    # other still drains so the child is not blocked on a full pipe.
    drain_task = asyncio.gather(
        drain_stdout(), drain_stderr(), return_exceptions=True
    )

    try:
        await asyncio.wait_for(
            asyncio.shield(_wait_for_exit(proc, drain_task)),
            timeout=invocation.wall_clock_timeout_s,
        )
        timed_out = False
    except TimeoutError:
        timed_out = True
        _kill_process_group(proc, pgid)
        # Give drain tasks a moment to catch EOF from the killed proc.
        try:
            await asyncio.wait_for(drain_task, timeout=5.0)
        except TimeoutError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            # Process-group kill failed; escalate to SIGKILL.
            _kill_process_group(proc, pgid, signal.SIGKILL)
            await proc.wait()

    elapsed = loop.time() - start_time
    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    # Classify.
    if timed_out:
        classification = Classification.TIMEOUT
    elif proc.returncode == EXIT_SUCCESS:
        classification = Classification.SUCCESS
    elif proc.returncode in (EXIT_SIGTERM, -signal.SIGTERM):
        # asyncio.subprocess reports signal-terminated processes as
        # negative of the signal number; shell-launched processes use
        # 128+sig. Accept both.
        classification = Classification.RETRYABLE_TRANSIENT
    else:
        classification = Classification.FAILED

    # Retryable also applies when the rate-limit retry loop exhausted.
    rate_limit_exhausted = False
    if classification == Classification.FAILED and _has_rate_limit_exhaustion(
        events, stderr_text
    ):
        classification = Classification.RETRYABLE_TRANSIENT
        rate_limit_exhausted = True

    cost_usd = 0.0
    for evt in events:
        if evt.get("type") == "result":
            cost_usd = float(evt.get("total_cost_usd", 0.0))
            break

    return DispatchResult(
        classification=classification,
        exit_code=proc.returncode,
        session_id=invocation.session_id,
        events=events,
        trailing_line=trailing_line,
        stderr_text=stderr_text,
        cost_usd=cost_usd,
        wall_clock_seconds=elapsed,
        rate_limit_exhausted=rate_limit_exhausted,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _wait_for_exit(
    proc: asyncio.subprocess.Process,
    drain_task: asyncio.Future,
) -> None:
    """Wait for both drain and process to finish."""
    await drain_task
    await proc.wait()


def _kill_process_group(
    proc: asyncio.subprocess.Process,
    pgid: int | None,
    sig: int = signal.SIGTERM,
) -> None:
    """Send ``sig`` to the process group, falling back to the pid if pgid is lost.

    Swallows ``ProcessLookupError`` — the process may have exited between
    the timeout check and this call.
    """
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        # Process or group gone already; nothing to do.
        pass


def _has_rate_limit_exhaustion(events: list[dict], stderr_text: str) -> bool:
    """Heuristic: did we exhaust rate-limit retries?"""
    # The real claude emits api_retry events with error='rate_limit' and
    # exits non-zero after max_retries. The fake reproduces this.
    retry_count = sum(
        1
        for e in events
        if e.get("type") == "system"
        and e.get("subtype") == "api_retry"
        and e.get("error") == "rate_limit"
    )
    if retry_count >= 3:
        return True
    return "rate limit exhausted" in stderr_text.lower()


__all__ = [
    "Classification",
    "DispatchInvocation",
    "DispatchResult",
    "EXIT_SIGTERM",
    "EXIT_SUCCESS",
    "STREAM_READER_LIMIT",
    "build_argv",
    "build_env",
    "generate_session_id",
    "invoke",
    "wrap_with_ssh",
]
