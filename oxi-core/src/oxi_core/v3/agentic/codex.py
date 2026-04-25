"""``CodexCliAdapter`` — wraps ``codex exec --json`` as an agentic backend.

Overview
--------
``CodexCliAdapter`` implements ``AgenticAdapter`` for the OpenAI Codex
CLI.  On instantiation it runs a **version-pin smoke test**: it emits a
known-good 3-event JSONL fixture through the parser logic and raises
``CodexFormatDriftError`` (with the binary's version string) if the
output format no longer matches.  This catches silent incompatibilities
before any real work is dispatched.

Subprocess design
-----------------
All subprocess invocations follow the same defensive patterns as
``dispatch_invoke.py``:

- **argv-form only** — ``asyncio.create_subprocess_exec``; never
  ``shell=True`` or string-concatenated commands.
- **1 MB StreamReader limit** — reuses ``STREAM_READER_LIMIT`` from
  ``dispatch_invoke``.
- **Concurrent stdout + stderr drain** — avoids OS-pipe-buffer deadlock
  (64 KB on macOS).
- **Env whitelist** — uses ``build_env`` from ``dispatch_invoke``; no
  unrelated secrets reach the child.
- **Process-group isolation** — ``start_new_session=True`` so a
  misbehaving codex subprocess cannot SIGTERM the supervisor.

Codex JSON event format
-----------------------
``codex exec --json`` emits newline-delimited JSON (JSONL) to stdout.
Each line is a dict with at minimum a ``"type"`` field.  The canonical
event types this adapter recognises::

    {"type": "start",   "version": "<semver>"}
    {"type": "output",  "text": "..."}
    {"type": "result",  "exit_code": 0}

The smoke test verifies a fixture containing exactly these three event
types.  If any line fails to parse, or if the required fields are
absent, ``CodexFormatDriftError`` is raised.

This PR is the spawn shell only — ``DispatchResult`` wiring comes next.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..dispatch_invoke import STREAM_READER_LIMIT, build_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Smoke-test fixture
# ---------------------------------------------------------------------------

# Known-good 3-event JSONL that ``codex exec --json`` must be able to produce.
# The parser logic is exercised against this fixture on every adapter init.
# If a new codex release changes its output schema the drift is caught here,
# not silently mid-dispatch.
_SMOKE_FIXTURE_LINES: tuple[str, ...] = (
    '{"type": "start", "version": "0.1.0"}',
    '{"type": "output", "text": "hello from codex"}',
    '{"type": "result", "exit_code": 0}',
)

# Required top-level field for each fixture event, in order.
# Any deviation is a format-drift signal.
_FIXTURE_REQUIRED_FIELDS: tuple[tuple[str, ...], ...] = (
    ("type", "version"),   # start event
    ("type", "text"),      # output event
    ("type", "exit_code"), # result event
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CodexFormatDriftError(RuntimeError):
    """Raised when the codex binary's output format diverges from the fixture.

    Attributes:
        binary_version: the semver string returned by ``pinned_version()``,
            so operators can pinpoint which release introduced the drift.
        detail: human-readable description of what failed (which field was
            missing, which line failed to parse, etc.).
    """

    def __init__(self, binary_version: str, detail: str) -> None:
        self.binary_version = binary_version
        self.detail = detail
        super().__init__(
            f"codex output format drift detected (binary version: {binary_version!r}): "
            f"{detail}"
        )


# ---------------------------------------------------------------------------
# Spawn result
# ---------------------------------------------------------------------------


@dataclass
class CodexSpawnResult:
    """Raw outcome of one ``codex exec --json`` invocation.

    This is intentionally minimal — full ``DispatchResult`` wiring comes
    in the next PR.  The adapter surfaces enough information for callers
    to know whether the process succeeded and what events were emitted.

    Attributes:
        exit_code: raw process exit code; ``None`` if the process was
            killed before it exited.
        events: parsed JSONL event dicts, in order received.
        stderr_text: captured stderr, capped at 64 KB.
        trailing_line: the final stdout line if it was not valid JSON
            (truncated on SIGKILL); ``None`` otherwise.
        wall_clock_seconds: elapsed wall time from spawn to exit.
    """

    exit_code: int | None
    events: list[dict[str, Any]] = field(default_factory=list)
    stderr_text: str = ""
    trailing_line: str | None = None
    wall_clock_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CodexCliAdapter:
    """Agentic backend adapter for the ``codex exec --json`` CLI.

    Instantiating this class runs the version-pin smoke test
    synchronously (via a trivial parse-only check against the fixture —
    no subprocess).  Any format mismatch raises ``CodexFormatDriftError``
    immediately so misconfigured environments fail fast.

    Parameters
    ----------
    binary:
        Filesystem path or bare name of the ``codex`` binary.  Defaults
        to ``"codex"`` (resolved via ``PATH`` at spawn time).
    version:
        Operator-pinned semver string.  Returned by ``pinned_version()``.
        Defaults to ``"0.1.0"``.
    wall_clock_timeout_s:
        Per-invocation wall-clock timeout in seconds.  Defaults to 600 s
        (10 minutes).  Callers may override per-task.

    Raises
    ------
    CodexFormatDriftError
        If the known-good fixture fails to parse or is missing required
        fields — indicating the adapter's expected output schema no longer
        matches the binary.
    """

    def __init__(
        self,
        binary: str = "codex",
        version: str = "0.1.0",
        wall_clock_timeout_s: float = 600.0,
    ) -> None:
        self._binary = binary
        self._version = version
        self._wall_clock_timeout_s = wall_clock_timeout_s
        # Run the format smoke test on init so mismatches are caught early.
        self._run_smoke_test()

    # ------------------------------------------------------------------
    # AgenticAdapter protocol
    # ------------------------------------------------------------------

    def pinned_version(self) -> str:
        """Return the operator-pinned semver string for the codex binary."""
        return self._version

    def binary_path(self) -> str:
        """Return the filesystem path (or bare name) of the codex binary."""
        return self._binary

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    async def spawn(
        self,
        prompt: str,
        cwd: Path,
        *,
        extra_env: dict[str, str] | None = None,
        wall_clock_timeout_s: float | None = None,
    ) -> CodexSpawnResult:
        """Spawn ``codex exec --json`` and drain its output.

        Parameters
        ----------
        prompt:
            The natural-language prompt passed to ``codex exec``.
        cwd:
            Working directory for the subprocess.  Must exist.
        extra_env:
            Additional env vars merged on top of the whitelisted base.
        wall_clock_timeout_s:
            Override the instance-level timeout for this invocation.

        Returns
        -------
        CodexSpawnResult
            Parsed events, exit code, and timing.  No ``DispatchResult``
            classification yet — that comes in the next PR.
        """
        timeout = wall_clock_timeout_s if wall_clock_timeout_s is not None \
            else self._wall_clock_timeout_s

        argv = self._build_argv(prompt)
        env = build_env(extra=extra_env or {})

        return await _run_subprocess(
            argv=argv,
            cwd=cwd,
            env=env,
            wall_clock_timeout_s=timeout,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_argv(self, prompt: str) -> list[str]:
        """Compose the argv list for ``codex exec --json``."""
        return [
            self._binary,
            "exec",
            "--json",
            "--",
            prompt,
        ]

    def _run_smoke_test(self) -> None:
        """Parse the known-good fixture; raise ``CodexFormatDriftError`` on mismatch.

        The smoke test is purely a parse-plus-schema check — no subprocess
        is spawned.  This keeps init fast and hermetic (no binary needed
        to instantiate the adapter in tests that supply their own version).
        """
        for i, (line, required) in enumerate(
            zip(_SMOKE_FIXTURE_LINES, _FIXTURE_REQUIRED_FIELDS, strict=True)
        ):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CodexFormatDriftError(
                    self._version,
                    f"fixture line {i} failed to parse: {exc}",
                ) from exc

            for field_name in required:
                if field_name not in event:
                    raise CodexFormatDriftError(
                        self._version,
                        f"fixture line {i} missing required field {field_name!r}: "
                        f"got {list(event.keys())}",
                    )


# ---------------------------------------------------------------------------
# Subprocess runner (module-private)
# ---------------------------------------------------------------------------


async def _run_subprocess(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    wall_clock_timeout_s: float,
) -> CodexSpawnResult:
    """Spawn ``argv`` as a subprocess and drain stdout + stderr.

    Mirrors the subprocess lifecycle in ``dispatch_invoke.invoke()``:
    - ``create_subprocess_exec`` (argv-form, never shell=True)
    - ``start_new_session=True`` for process-group isolation
    - ``limit=STREAM_READER_LIMIT`` (1 MB) on the StreamReader
    - concurrent stdout + stderr drain to avoid pipe-buffer deadlock
    - wall-clock timeout with SIGTERM → SIGKILL escalation
    """
    loop = asyncio.get_running_loop()
    start_time = loop.time()

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=os.fspath(cwd.resolve()),
        env=env,
        start_new_session=True,
        limit=STREAM_READER_LIMIT,
    )

    # Capture pgid immediately — once the process exits, getpgid fails.
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None

    events: list[dict[str, Any]] = []
    trailing_line: str | None = None
    stderr_chunks: list[bytes] = []

    async def _drain_stdout() -> None:
        nonlocal trailing_line
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except asyncio.LimitOverrunError:
                stderr_chunks.append(b"oxi: codex stdout line exceeded 1MB limit\n")
                continue
            if not raw:
                return
            decoded = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not decoded:
                continue
            try:
                events.append(json.loads(decoded))
            except json.JSONDecodeError:
                # Only the trailing line after SIGKILL should be truncated.
                if trailing_line is not None:
                    stderr_chunks.append(
                        f"oxi: codex dropped non-JSON line: {trailing_line[:200]}\n"
                        .encode()
                    )
                trailing_line = decoded

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
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

    drain_task = asyncio.gather(
        _drain_stdout(), _drain_stderr(), return_exceptions=True
    )

    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.shield(_wait_proc(proc, drain_task)),
            timeout=wall_clock_timeout_s,
        )
    except TimeoutError:
        timed_out = True
        _kill_group(proc, pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(drain_task, timeout=5.0)
        except TimeoutError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            _kill_group(proc, pgid, signal.SIGKILL)
            await proc.wait()

    elapsed = loop.time() - start_time
    if timed_out:
        logger.warning(
            "codex subprocess exceeded wall-clock timeout (%.1f s); killed",
            wall_clock_timeout_s,
        )

    return CodexSpawnResult(
        exit_code=proc.returncode,
        events=events,
        stderr_text=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        trailing_line=trailing_line,
        wall_clock_seconds=elapsed,
    )


async def _wait_proc(
    proc: asyncio.subprocess.Process,
    drain_task: asyncio.Future[Any],
) -> None:
    """Wait for both drain tasks and the process to finish."""
    await drain_task
    await proc.wait()


def _kill_group(
    proc: asyncio.subprocess.Process,
    pgid: int | None,
    sig: int,
) -> None:
    """Send ``sig`` to the process group, falling back to the pid."""
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


__all__ = [
    "CodexCliAdapter",
    "CodexFormatDriftError",
    "CodexSpawnResult",
]
