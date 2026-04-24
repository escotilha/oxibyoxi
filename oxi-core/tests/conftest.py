"""Root conftest for oxi-core tests.

Timeout policy (enforced by pytest-timeout):
  - Default per-test timeout: 30 s  (set via ``timeout`` in pyproject.toml).
  - @pytest.mark.slow tests:  180 s (applied automatically below).

The hook runs after collection so it overrides the global default without
requiring each slow test to repeat ``@pytest.mark.timeout(180)``.
"""

from __future__ import annotations

import pytest

_SLOW_TIMEOUT = 180  # seconds


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Upgrade the timeout for tests decorated with @pytest.mark.slow."""
    for item in items:
        if item.get_closest_marker("slow"):
            # pytest-timeout respects the 'timeout' marker value; adding it
            # here is equivalent to @pytest.mark.timeout(180) on each test.
            item.add_marker(pytest.mark.timeout(_SLOW_TIMEOUT), append=False)
