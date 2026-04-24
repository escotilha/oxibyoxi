"""Killswitch file — bridges a file-on-disk into ``EngineState``.

The engine's in-memory ``EngineState.is_stopping()`` is the runtime
signal every loop respects. The killswitch file is how an operator
(from a terminal, not from inside the process) tells the engine to
stop: create the file and all loops exit on their next iteration;
remove the file and the engine resumes.

The file lives at ``adapter.paths().release_lock_path`` by
convention. Tests can point it anywhere.

Not a daemon; not a signal handler. Just a file that ``poll()`` turns
into a boolean. A supervisor thread (CLI or systemd timer) calls
``poll()`` periodically and calls ``engine_state.request_stop()`` when
it flips to True.
"""

from __future__ import annotations

from pathlib import Path

from ..adapter import get_active_adapter


def path() -> Path:
    """Return the absolute path to the killswitch file.

    Reads ``adapter.paths().release_lock_path``. Callers should catch
    ``AdapterNotRegisteredError`` themselves if they might run before
    registration.
    """
    paths = get_active_adapter().paths()
    if paths.release_lock_path is None:
        # Fallback: put it under the repo root's .oxi/ dir.
        repo_root = paths.repo_root or "."
        return Path(repo_root) / paths.oxi_dir / "KILLSWITCH"
    return Path(paths.release_lock_path)


def is_set(killswitch_path: Path | None = None) -> bool:
    """Return True if the killswitch file exists.

    ``killswitch_path`` override is intended for tests and for CLI
    commands that accept an explicit path.
    """
    p = killswitch_path if killswitch_path is not None else path()
    return p.exists()


def create(killswitch_path: Path | None = None, reason: str = "") -> Path:
    """Create the killswitch file. Returns the path it was written to.

    ``reason`` is stored in the file as a single line so operators can
    see why a previous run was halted. Empty string is fine.
    """
    p = killswitch_path if killswitch_path is not None else path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reason + "\n" if reason else "")
    return p


def remove(killswitch_path: Path | None = None) -> bool:
    """Delete the killswitch file. Returns True if something was removed."""
    p = killswitch_path if killswitch_path is not None else path()
    if p.exists():
        p.unlink()
        return True
    return False


__all__ = ["create", "is_set", "path", "remove"]
