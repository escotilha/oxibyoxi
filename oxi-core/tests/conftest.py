"""Root conftest for oxi-core tests.

Timeout policy (enforced by pytest-timeout):
  - Default per-test timeout: 30 s  (set via ``timeout`` in pyproject.toml).
  - @pytest.mark.slow tests:  180 s (applied automatically below).

The hook runs after collection so it overrides the global default without
requiring each slow test to repeat ``@pytest.mark.timeout(180)``.

sys.path bootstrap
------------------
The installed ``oxi-core`` package may point to a *different* worktree (the
editable install is keyed to whatever worktree last ran ``pip install -e .``).
To ensure tests always use *this* worktree's source, we prepend the local
``src/`` directory to ``sys.path`` here — conftest.py is loaded before any
test module is imported, so the path is correct for all collection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Prepend the local src/ so oxi_core resolves to *this* worktree's source,
# even when a different editable install is on sys.path.
_OXI_CORE_SRC = Path(__file__).resolve().parents[1] / "src"
if _OXI_CORE_SRC.exists() and str(_OXI_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_OXI_CORE_SRC))

_SLOW_TIMEOUT = 180  # seconds


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Upgrade the timeout for tests decorated with @pytest.mark.slow."""
    for item in items:
        if item.get_closest_marker("slow"):
            # pytest-timeout respects the 'timeout' marker value; adding it
            # here is equivalent to @pytest.mark.timeout(180) on each test.
            item.add_marker(pytest.mark.timeout(_SLOW_TIMEOUT), append=False)
