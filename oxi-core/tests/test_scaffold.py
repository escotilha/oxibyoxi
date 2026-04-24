"""Phase 0 smoke test — the package imports and the CLI is wired up."""

from __future__ import annotations

import oxi_core
from oxi_core.cli import main


def test_package_imports() -> None:
    # __version__ is read from installed package metadata via
    # importlib.metadata; assert the shape (e.g. "0.1.0a2"), not a frozen
    # string that would have to move in lock-step with every release.
    assert isinstance(oxi_core.__version__, str)
    assert oxi_core.__version__ != ""


def test_cli_returns_zero_with_no_args(capsys) -> None:
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "oxi" in captured.out


def test_cli_returns_nonzero_on_unknown_command(capsys) -> None:
    """Argparse raises SystemExit(2) for unknown subcommands."""
    import pytest
    with pytest.raises(SystemExit) as exc:
        main(["not-a-command"])
    assert exc.value.code == 2
