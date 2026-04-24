#!/usr/bin/env bash
# generate-sbom.sh — produce a CycloneDX SBOM for an installed oxi package.
#
# Usage:
#   scripts/generate-sbom.sh <package-path> <python-interpreter> <output-dir>
#
# Examples:
#   scripts/generate-sbom.sh oxi-core /tmp/smoke-venv/bin/python dist/
#
# The script uses cyclonedx-py (from the cyclonedx-bom package) to build a
# JSON SBOM from the Python environment that has the target package installed.
# The SBOM is written to <output-dir>/sbom-<pkg-name>-<version>.cdx.json.
#
# Requirements:
# - cyclonedx-bom installed in the active environment (pip install cyclonedx-bom).
# - The target <python-interpreter> must have the package installed so
#   cyclonedx-py can introspect its dependency closure.
#
# Exit codes:
#   0 — SBOM written successfully; path printed to stdout
#   1 — generation error
#   2 — script misuse

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# -------- args --------

if [[ $# -lt 3 ]]; then
  echo "usage: $(basename "$0") <package-path> <python-interpreter> <output-dir>" >&2
  exit 2
fi

PACKAGE_PATH="$1"
PYTHON_INTERP="$2"
OUTPUT_DIR="$3"

ABS_PACKAGE_PATH="${REPO_ROOT}/${PACKAGE_PATH}"
if [[ ! -f "${ABS_PACKAGE_PATH}/pyproject.toml" ]]; then
  echo "generate-sbom: ${ABS_PACKAGE_PATH}/pyproject.toml not found" >&2
  exit 2
fi

if [[ ! -x "${PYTHON_INTERP}" ]]; then
  echo "generate-sbom: python interpreter not found or not executable: ${PYTHON_INTERP}" >&2
  exit 2
fi

# -------- resolve package metadata --------

PKG_NAME="$(python3 -c '
import tomllib, sys
data = tomllib.loads(open(sys.argv[1], "rb").read().decode())
print(data.get("project", {}).get("name", ""))
' "${ABS_PACKAGE_PATH}/pyproject.toml")"

PKG_VERSION="$(python3 -c '
import tomllib, sys
data = tomllib.loads(open(sys.argv[1], "rb").read().decode())
print(data.get("project", {}).get("version", ""))
' "${ABS_PACKAGE_PATH}/pyproject.toml")"

if [[ -z "${PKG_NAME}" || -z "${PKG_VERSION}" ]]; then
  echo "generate-sbom: could not read name/version from ${ABS_PACKAGE_PATH}/pyproject.toml" >&2
  exit 1
fi

# -------- locate cyclonedx-py --------

# Prefer the cyclonedx-py binary co-located with the python interpreter,
# then fall back to whichever cyclonedx-py is on PATH, then fall back to
# invoking the cyclonedx-py Python module directly (which works even when
# the console script landed in a location not on PATH — common on GitHub
# Actions where python ships in /opt/hostedtoolcache/...).
VENV_BIN="$(dirname "${PYTHON_INTERP}")"
if [[ -x "${VENV_BIN}/cyclonedx-py" ]]; then
  CDX=("${VENV_BIN}/cyclonedx-py")
elif command -v cyclonedx-py >/dev/null 2>&1; then
  CDX=("cyclonedx-py")
elif "${PYTHON_INTERP}" -c "import cyclonedx_py" >/dev/null 2>&1; then
  CDX=("${PYTHON_INTERP}" "-m" "cyclonedx_py")
else
  echo "generate-sbom: cyclonedx-py not found — install cyclonedx-bom into the environment" >&2
  exit 1
fi

# -------- generate --------

mkdir -p "${OUTPUT_DIR}"

SBOM_FILENAME="sbom-${PKG_NAME}-${PKG_VERSION}.cdx.json"
SBOM_PATH="${OUTPUT_DIR}/${SBOM_FILENAME}"

echo "generate-sbom: generating CycloneDX SBOM for ${PKG_NAME}==${PKG_VERSION}"
echo "generate-sbom:   interpreter : ${PYTHON_INTERP}"
echo "generate-sbom:   cyclonedx-py: ${CDX}"
echo "generate-sbom:   output      : ${SBOM_PATH}"

# cyclonedx-py environment <python> reads the site-packages of that interpreter
# and emits a JSON SBOM (CycloneDX 1.6 by default).
# --pyproject supplies the root-component metadata (name, version, license).
# --mc-type library is correct for a pip-installable library.
# --output-reproducible strips timestamps so identical inputs → identical SBOM.
"${CDX[@]}" environment \
  --pyproject "${ABS_PACKAGE_PATH}/pyproject.toml" \
  --mc-type library \
  --of JSON \
  --output-reproducible \
  -o "${SBOM_PATH}" \
  "${PYTHON_INTERP}"

if [[ ! -f "${SBOM_PATH}" ]]; then
  echo "generate-sbom: SBOM file not found after generation — cyclonedx-py may have failed" >&2
  exit 1
fi

SBOM_SIZE="$(wc -c < "${SBOM_PATH}" | tr -d ' ')"
echo "generate-sbom: SBOM written to ${SBOM_PATH} (${SBOM_SIZE} bytes)"

# Print the path on the last line so callers can capture it.
echo "${SBOM_PATH}"
