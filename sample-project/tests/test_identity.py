"""Smoke tests for sample-project. Intentionally minimal."""

from sample_project import __version__
from sample_project.identity import SENTINEL


def test_package_has_version():
    assert __version__ == "0.0.0"


def test_sentinel_is_set():
    assert SENTINEL == "sample-project"
