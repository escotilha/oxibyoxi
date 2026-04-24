#!/usr/bin/env bash
# end-to-end.sh — Phase 1 exit smoke test.
#
# Wires every engine module together against the sample-project
# fixture, using the fake claude binary and a fake GitHub client.
# Starts from a clean DB, runs five ticks, asserts one task reaches
# `merged` status.
#
# This is the Phase 1 acceptance test. Runs in CI. Does not spawn
# real Claude. Does not hit real GitHub. Does not touch any path
# outside a tmp dir.
#
# Exit codes:
#   0 — one or more tasks reached 'merged'
#   1 — no merged tasks after five ticks
#   2 — script misuse / setup failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# Find a python interpreter. Prefer python3.12 (matches CI matrix top);
# fall back to python3.
if command -v python3.12 >/dev/null 2>&1; then
  PY=python3.12
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "smoke: no python3 on PATH" >&2
  exit 2
fi

# Ensure a venv exists with both packages installed. Callers can reuse
# an existing venv by setting SMOKE_VENV.
SMOKE_VENV="${SMOKE_VENV:-${REPO_ROOT}/.smoke-venv}"
if [[ ! -x "${SMOKE_VENV}/bin/python" ]]; then
  "${PY}" -m venv "${SMOKE_VENV}"
  "${SMOKE_VENV}/bin/pip" install --quiet --upgrade pip
  "${SMOKE_VENV}/bin/pip" install --quiet -e "./oxi-core[dev]" \
                                          -e "./adapters/_reference[dev]"
fi

# Run the Python driver (below) using the venv's Python.
"${SMOKE_VENV}/bin/python" "${SCRIPT_DIR}/end-to-end-driver.py"
