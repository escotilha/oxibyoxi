#!/usr/bin/env bash
# release.sh — build and publish a subpackage to PyPI.
#
# Usage:
#   scripts/release.sh <package-path> [--test] [--no-sbom]
#
# Examples:
#   scripts/release.sh oxi-core                 # publish oxi-core to PyPI
#   scripts/release.sh adapters/_reference       # publish oxi-adapter-reference
#   scripts/release.sh oxi-core --test          # publish to TestPyPI
#   scripts/release.sh oxi-core --no-sbom       # skip SBOM generation
#
# Requirements:
# - Token stored in macOS Keychain under service "PYPI_API_TOKEN" (or
#   "TESTPYPI_API_TOKEN" when --test). Never pass tokens on the command
#   line; never commit them anywhere.
# - Python 3.11+ with `build`, `twine`, and `cyclonedx-bom` installed in
#   the active venv (cyclonedx-bom is used for SBOM generation).
#
# Anti-pattern notes:
# - Fails if the git working tree is dirty (avoid shipping untracked
#   artifacts).
# - Fails if the package's version already exists on the target index.
# - Cleans dist/ before building so stale wheels can't sneak in.
# - SBOM is generated from the smoke-test venv (isolated install of the
#   freshly built wheel) so the dependency snapshot reflects exactly what
#   a clean pip install pulls in — not the broader dev environment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# -------- args --------

if [[ $# -lt 1 ]]; then
  echo "usage: $(basename "$0") <package-path> [--test]" >&2
  exit 2
fi

PACKAGE_PATH="$1"
shift

TARGET="pypi"
KEYCHAIN_SERVICE="PYPI_API_TOKEN"
REPOSITORY_URL=""
GENERATE_SBOM="yes"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)
      TARGET="testpypi"
      KEYCHAIN_SERVICE="TESTPYPI_API_TOKEN"
      REPOSITORY_URL="https://test.pypi.org/legacy/"
      ;;
    --no-sbom)
      GENERATE_SBOM="no"
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
  shift
done

ABS_PACKAGE_PATH="${REPO_ROOT}/${PACKAGE_PATH}"
if [[ ! -f "${ABS_PACKAGE_PATH}/pyproject.toml" ]]; then
  echo "release: ${ABS_PACKAGE_PATH}/pyproject.toml not found" >&2
  exit 2
fi

# -------- preflight --------

cd "${REPO_ROOT}"

# Clean working tree required — no stray uncommitted files get bundled.
if [[ -n "$(git status --porcelain)" ]]; then
  echo "release: git working tree is dirty; commit or stash before releasing" >&2
  git status --short
  exit 1
fi

# Leak-lint before publishing — belt and suspenders beyond CI.
./scripts/lint-for-leaks.sh

# -------- build --------

cd "${ABS_PACKAGE_PATH}"
rm -rf dist/ build/ -- *.egg-info
python -m build --sdist --wheel

# -------- local install smoke test --------
# Install the freshly built wheel in a throwaway venv and import it.
# Catches packaging errors that TestPyPI would otherwise catch.
# The smoke venv is also the target for SBOM generation (see below) so
# it captures exactly what a clean pip install pulls in.

SMOKE_VENV_PARENT="$(mktemp -d)"
SMOKE_VENV="${SMOKE_VENV_PARENT}/smoke-venv"
python -m venv "${SMOKE_VENV}"
WHEEL="$(ls dist/*.whl | head -n 1)"
"${SMOKE_VENV}/bin/pip" install --quiet "${WHEEL}"

# Derive the import name from the pyproject [project] name.
PKG_NAME="$(python -c 'import tomllib,sys; print(tomllib.loads(open("pyproject.toml","rb").read().decode()).get("project",{}).get("name",""))')"
IMPORT_NAME="$(echo "${PKG_NAME}" | tr '-' '_')"

"${SMOKE_VENV}/bin/python" -c "import ${IMPORT_NAME}; print(f'imported ${IMPORT_NAME} {getattr(${IMPORT_NAME}, \"__version__\", \"?\")}')"

# -------- SBOM generation --------
# Run before removing the smoke venv — the SBOM reflects the clean install
# environment (just the wheel's declared dependencies, nothing extra from
# the dev environment). The SBOM lands in dist/ alongside the wheel and
# sdist so it can be attached to the GitHub release as a release artifact.

if [[ "${GENERATE_SBOM}" == "yes" ]]; then
  # Install cyclonedx-bom into the smoke venv so the SBOM tool introspects
  # exactly the same environment we just validated. This keeps the dep closure
  # in the SBOM identical to what end-users get from `pip install`.
  "${SMOKE_VENV}/bin/pip" install --quiet cyclonedx-bom

  # generate-sbom.sh prints status lines plus the SBOM path on the last
  # line. Capture the whole output, then take only the last non-empty
  # line — that's the path. (Earlier capture-the-whole-blob behavior made
  # the existence check spuriously fail.)
  SBOM_OUTPUT="$(
    "${REPO_ROOT}/scripts/generate-sbom.sh" \
      "${PACKAGE_PATH}" \
      "${SMOKE_VENV}/bin/python" \
      "dist/"
  )"
  SBOM_PATH="$(echo "${SBOM_OUTPUT}" | tail -n 1)"
  if [[ ! -f "${SBOM_PATH}" ]]; then
    echo "release: SBOM generation failed — dist/ may be incomplete" >&2
    echo "  generator output:" >&2
    echo "${SBOM_OUTPUT}" | sed 's/^/    /' >&2
    rm -rf "${SMOKE_VENV_PARENT}"
    exit 1
  fi
  echo "release: SBOM → ${SBOM_PATH}"
else
  echo "release: SBOM generation skipped (--no-sbom)"
fi

rm -rf "${SMOKE_VENV_PARENT}"

# -------- publish --------

TOKEN="$(security find-generic-password -s "${KEYCHAIN_SERVICE}" -a "${USER}" -w 2>/dev/null)"
if [[ -z "${TOKEN}" ]]; then
  echo "release: no Keychain entry for ${KEYCHAIN_SERVICE}; see docs/anti-patterns.md §Secrets" >&2
  exit 1
fi

# Twine reads TWINE_USERNAME/TWINE_PASSWORD from env; we pass the token
# inline so it never lands in shell history or a config file.
TWINE_ARGS=("upload" "--non-interactive")
if [[ "${TARGET}" == "testpypi" ]]; then
  TWINE_ARGS+=("--repository-url" "${REPOSITORY_URL}")
fi
TWINE_ARGS+=("dist/"*)

TWINE_USERNAME="__token__" TWINE_PASSWORD="${TOKEN}" \
  python -m twine "${TWINE_ARGS[@]}"

unset TOKEN

echo "release: ${PKG_NAME} uploaded to ${TARGET}"
