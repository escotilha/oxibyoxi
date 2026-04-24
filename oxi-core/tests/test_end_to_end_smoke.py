"""Pytest wrapper around scripts/smoke/end-to-end.sh.

Runs the Phase 1 exit smoke test as a pytest test so CI's normal
``pytest -q`` invocation exercises the full loop.

The shell script is the authoritative smoke. This wrapper only
asserts its exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke" / "end-to-end.sh"


@pytest.mark.slow
def test_end_to_end_smoke_passes():
    """Run the smoke script; assert exit 0.

    Marked ``slow`` so users can opt out with ``pytest -m "not slow"``
    if they need a quick local run. CI always runs it.
    """
    if not SMOKE_SCRIPT.exists():
        pytest.skip(f"smoke script not found at {SMOKE_SCRIPT}")

    # Reuse the current venv so we don't re-pip-install packages in CI.
    env = {**os.environ, "SMOKE_VENV": sys.prefix}
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT)],
        capture_output=True, text=True, check=False, env=env,
    )
    if proc.returncode != 0:
        sys.stderr.write("--- smoke stdout ---\n" + proc.stdout)
        sys.stderr.write("--- smoke stderr ---\n" + proc.stderr)
    assert proc.returncode == 0, "smoke test failed (see captured stdout/stderr)"
    # Belt + suspenders: the expected pass line.
    assert "PASS" in proc.stdout
