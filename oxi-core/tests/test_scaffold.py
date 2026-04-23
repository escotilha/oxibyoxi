"""Phase 0 smoke test — the package imports and the CLI is wired up."""

from __future__ import annotations

import oxi_core
from oxi_core.cli import main


def test_package_imports() -> None:
    assert oxi_core.__version__ == "0.0.0"


def test_cli_returns_zero_with_no_args(capsys) -> None:
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "oxi" in captured.out


def test_cli_returns_nonzero_on_unknown_command(capsys) -> None:
    rc = main(["not-a-command"])
    assert rc == 2
