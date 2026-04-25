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
# Upload only the wheel and sdist — twine rejects anything else as
# "Unknown distribution format". The SBOM JSON in dist/ is a release
# artifact for GitHub Releases, not for PyPI.
TWINE_ARGS+=("dist/"*.whl "dist/"*.tar.gz)

TWINE_USERNAME="__token__" TWINE_PASSWORD="${TOKEN}" \
  python -m twine "${TWINE_ARGS[@]}"

unset TOKEN

echo "release: ${PKG_NAME} uploaded to ${TARGET}"

# -------- attach SBOM to GitHub Release --------
# SBOM is a release artifact, not a PyPI artifact. Auto-attach to the
# matching GitHub Release if (a) we have an SBOM, (b) target is real
# PyPI (not TestPyPI — those don't get GH Releases), (c) gh CLI is
# available, and (d) a release tagged v<version> already exists.
#
# Skipping this step is non-fatal — operators can attach manually with
# `gh release upload v<version> dist/*.cdx.json`.

if [[ "${TARGET}" != "pypi" ]] || [[ "${GENERATE_SBOM}" != "yes" ]]; then
  exit 0
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "release: gh CLI not installed — skipping SBOM attach to GH Release"
  echo "        attach manually: gh release upload v<version> dist/*.cdx.json"
  exit 0
fi

# Read version straight from the freshly built dist artifacts so we
# match exactly what twine just uploaded — no stale pyproject reads.
PKG_VERSION="$(
  python -c 'import tomllib,sys; print(tomllib.loads(open("pyproject.toml","rb").read().decode()).get("project",{}).get("version",""))'
)"
if [[ -z "${PKG_VERSION}" ]]; then
  echo "release: could not read version from pyproject.toml — skipping SBOM attach"
  exit 0
fi

RELEASE_TAG="v${PKG_VERSION}"

# Only attach if the tag exists. The release script does not create
# GH Releases — that's a separate, deliberate operator step. If the
# release isn't there yet, surface guidance and exit cleanly.
if ! gh release view "${RELEASE_TAG}" >/dev/null 2>&1; then
  echo "release: GH Release ${RELEASE_TAG} not found — skipping SBOM attach"
  echo "        once you create the release, run:"
  echo "        gh release upload ${RELEASE_TAG} ${ABS_PACKAGE_PATH}/dist/*.cdx.json"
  exit 0
fi

# --clobber is safe here: if a previous release.sh run partially
# uploaded an SBOM, replace it with the freshly generated one. The
# wheel/sdist on PyPI are immutable, but a re-run can only happen on
# the same package+version combo, which means the dependency tree —
# and hence the SBOM — is identical.
SBOM_FILES=("${ABS_PACKAGE_PATH}/dist/"*.cdx.json)
if [[ ! -f "${SBOM_FILES[0]}" ]]; then
  echo "release: no .cdx.json files in dist/ — skipping SBOM attach"
  exit 0
fi

echo "release: attaching SBOM to GH Release ${RELEASE_TAG}"
gh release upload "${RELEASE_TAG}" "${SBOM_FILES[@]}" --clobber
echo "release: SBOM attached → https://github.com/escotilha/oxi/releases/tag/${RELEASE_TAG}"
