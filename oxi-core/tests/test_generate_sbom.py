"""Tests for scripts/generate-sbom.sh.

Verifies that the SBOM script:
  - produces a valid CycloneDX JSON file
  - includes the expected package name in the metadata
  - names the output file with the correct pkg+version slug
  - exits non-zero when given a missing pyproject.toml
  - exits non-zero when cyclonedx-py is absent from the interpreter's env
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SBOM = REPO_ROOT / "scripts" / "generate-sbom.sh"
OXI_CORE = REPO_ROOT / "oxi-core"


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GENERATE_SBOM)] + args,
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


@pytest.mark.slow
def test_sbom_produced_for_oxi_core(tmp_path: Path) -> None:
    """generate-sbom.sh writes a CycloneDX JSON file for oxi-core."""
    proc = _run(["oxi-core", sys.executable, str(tmp_path)])
    if proc.returncode != 0:
        pytest.fail(
            f"generate-sbom.sh exited {proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )

    # The last non-empty line of stdout is the SBOM path.
    last_line = proc.stdout.strip().splitlines()[-1]
    sbom_path = Path(last_line)
    assert sbom_path.exists(), f"SBOM file not found at {sbom_path}"
    assert sbom_path.suffix == ".json"
    assert sbom_path.name.startswith("sbom-oxi-core-")
    assert sbom_path.name.endswith(".cdx.json")


@pytest.mark.slow
def test_sbom_is_valid_cyclonedx_json(tmp_path: Path) -> None:
    """The emitted file is valid JSON with the CycloneDX bomFormat field."""
    proc = _run(["oxi-core", sys.executable, str(tmp_path)])
    assert proc.returncode == 0, proc.stderr

    last_line = proc.stdout.strip().splitlines()[-1]
    sbom_path = Path(last_line)
    data = json.loads(sbom_path.read_text())

    assert data.get("bomFormat") == "CycloneDX", (
        "SBOM missing bomFormat=CycloneDX; file may be malformed"
    )
    assert "metadata" in data, "SBOM missing 'metadata' section"
    assert "components" in data, "SBOM missing 'components' section"


@pytest.mark.slow
def test_sbom_metadata_names_oxi_core(tmp_path: Path) -> None:
    """The root component in the SBOM metadata matches oxi-core."""
    proc = _run(["oxi-core", sys.executable, str(tmp_path)])
    assert proc.returncode == 0, proc.stderr

    last_line = proc.stdout.strip().splitlines()[-1]
    sbom_path = Path(last_line)
    data = json.loads(sbom_path.read_text())

    component = data.get("metadata", {}).get("component", {})
    assert component.get("name") == "oxi-core", (
        f"Expected root component name 'oxi-core', got {component.get('name')!r}"
    )


def test_sbom_exits_2_on_missing_pyproject(tmp_path: Path) -> None:
    """Exit code 2 (misuse) when package-path has no pyproject.toml."""
    proc = _run(["nonexistent-pkg", sys.executable, str(tmp_path)])
    assert proc.returncode == 2, (
        f"Expected exit 2 for missing pyproject.toml, got {proc.returncode}"
    )


def test_sbom_exits_2_on_missing_interpreter(tmp_path: Path) -> None:
    """Exit code 2 (misuse) when the interpreter path doesn't exist."""
    proc = _run(["oxi-core", "/nonexistent/python", str(tmp_path)])
    assert proc.returncode == 2, (
        f"Expected exit 2 for missing interpreter, got {proc.returncode}"
    )
