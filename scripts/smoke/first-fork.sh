#!/usr/bin/env bash
# first-fork.sh — install-path smoke test for the first-fork experience.
#
# Simulates what a fresh operator does after discovering oxi:
#
#   1. Build oxi-core from the current checkout into a wheel.
#   2. Install that wheel (not editable) into a clean venv — proving
#      the *distributed* package works, not just the source tree.
#   3. Run `oxi init` with scripted answers piped through stdin.
#   4. `pip install -e .` the scaffolded adapter inside the same venv.
#   5. Run a minimal driver that registers the adapter and calls
#      `oxi v3 tick` (reconciliation-only, no Claude, no GitHub).
#   6. Assert the tick exits 0 and prints the expected "tick done" line.
#
# This test catches regressions in:
#   - oxi-core packaging (missing files, broken __init__, bad metadata)
#   - the wizard scaffold (broken template substitution, missing files)
#   - the adapter importability (bad pyproject.toml, broken adapter.py)
#   - the reconciliation-only tick path (heartbeat + engine_state)
#
# What this test does NOT exercise (by design):
#   - Real Claude dispatch (--real-claude flag)
#   - Real GitHub API calls
#   - Budget spend
#
# See docs/first-fork-checklist.md §Proposed path #4.
#
# Exit codes:
#   0 — all steps passed (PASS printed to stdout)
#   1 — one or more assertions failed
#   2 — script misuse / setup failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Python interpreter — prefer python3.12 to match CI matrix; fall back to
# python3.11, then python3.
# ---------------------------------------------------------------------------

if command -v python3.12 >/dev/null 2>&1; then
  PY=python3.12
elif command -v python3.11 >/dev/null 2>&1; then
  PY=python3.11
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "first-fork-smoke: no python3 on PATH" >&2
  exit 2
fi

echo "first-fork-smoke: using ${PY} ($(${PY} --version))"

# ---------------------------------------------------------------------------
# Temp workspace — isolated from the repo, cleaned on exit unless KEEP_TMP=1.
# ---------------------------------------------------------------------------

WORK_DIR="$(mktemp -d)"
KEEP_TMP="${KEEP_TMP:-0}"

cleanup() {
  if [[ "${KEEP_TMP}" == "1" ]]; then
    echo "first-fork-smoke: tmp dir preserved at ${WORK_DIR}"
  else
    rm -rf "${WORK_DIR}"
  fi
}
trap cleanup EXIT

echo "first-fork-smoke: workspace = ${WORK_DIR}"

DIST_DIR="${WORK_DIR}/dist"
VENV_DIR="${WORK_DIR}/venv"
ADAPTER_DIR="${WORK_DIR}/oxi-adapter-smoketest"
FAKE_REPO_DIR="${WORK_DIR}/fake-repo"

# ---------------------------------------------------------------------------
# Step 1 — Build the oxi-core wheel from the current checkout.
# ---------------------------------------------------------------------------

echo ""
echo "step 1/6: build oxi-core wheel"

mkdir -p "${DIST_DIR}"

# Ensure build is available in the system Python (the one we use to create
# the venv). We install it ephemerally into --user to avoid touching the
# venv that doesn't exist yet.
"${PY}" -m pip install --quiet --upgrade pip
"${PY}" -m pip install --quiet build

"${PY}" -m build \
  --wheel \
  --outdir "${DIST_DIR}" \
  "${REPO_ROOT}/oxi-core"

WHEEL="$(ls "${DIST_DIR}"/*.whl 2>/dev/null | head -1)"
if [[ -z "${WHEEL}" ]]; then
  echo "first-fork-smoke: FAIL — no wheel produced in ${DIST_DIR}" >&2
  exit 1
fi
echo "  wheel: $(basename "${WHEEL}")"

# ---------------------------------------------------------------------------
# Step 2 — Fresh venv + install from wheel (not editable).
# ---------------------------------------------------------------------------

echo ""
echo "step 2/6: create venv and install from wheel"

"${PY}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet "${WHEEL}"

# Sanity-check: `oxi` entry-point exists and reports a version.
OXI="${VENV_DIR}/bin/oxi"
if [[ ! -x "${OXI}" ]]; then
  echo "first-fork-smoke: FAIL — oxi not found at ${OXI}" >&2
  exit 1
fi
OXI_VERSION="$("${OXI}" --version)"
echo "  installed: ${OXI_VERSION}"

# ---------------------------------------------------------------------------
# Step 3 — Run `oxi init` with scripted answers via stdin.
#
# The wizard reads from the builtin input(), which reads from stdin when
# the process is not connected to a terminal. We pipe one answer per line
# in the exact order the wizard asks (13 prompts total — the final
# review-and-confirm gate was added in T1-23).
#
# Prompt order:
#   1. project_name             → "smoketest"
#   2. adapter_slug             → "" (accept default derived from name)
#   3. github_repo              → "owner/smoketest"
#   4. repo_root                → FAKE_REPO_DIR (absolute, so validation passes)
#   5. roadmap_location         → "" (accept default = roadmap.md)
#   6. plan_tier                → "" (accept default = standard)
#   7. budget soft_warn         → "" (accept default = 5.0)
#   8. budget hard_cap          → "" (accept default = 20.0)
#   9. budget per-task opus     → "" (accept default = 2.0)
#  10. budget per-task sonnet   → "" (accept default = 0.50)
#  11. max_concurrent           → "" (accept default = 3)
#  12. auto_merge               → "n" (accept default = N)
#  13. review-and-confirm gate  → "y" (commit the scaffold)
# ---------------------------------------------------------------------------

echo ""
echo "step 3/6: oxi init (scripted answers)"

mkdir -p "${FAKE_REPO_DIR}"

# printf with \n as separator; variable expansion works unlike heredoc with quotes.
printf '%s\n' \
  "smoketest" \
  "" \
  "owner/smoketest" \
  "${FAKE_REPO_DIR}" \
  "" \
  "" \
  "" \
  "" \
  "" \
  "" \
  "" \
  "n" \
  "y" \
  | "${OXI}" init "${ADAPTER_DIR}"

if [[ ! -f "${ADAPTER_DIR}/pyproject.toml" ]]; then
  echo "first-fork-smoke: FAIL — scaffold did not produce pyproject.toml" >&2
  exit 1
fi
if [[ ! -f "${ADAPTER_DIR}/src/oxi_adapter_smoketest/adapter.py" ]]; then
  echo "first-fork-smoke: FAIL — scaffold did not produce adapter.py" >&2
  exit 1
fi
FILE_COUNT="$(find "${ADAPTER_DIR}" -type f | wc -l | tr -d ' ')"
echo "  scaffold: OK (${FILE_COUNT} files written)"

# ---------------------------------------------------------------------------
# Step 4 — pip install -e . the scaffolded adapter into the same venv.
# ---------------------------------------------------------------------------

echo ""
echo "step 4/6: pip install -e . (scaffolded adapter)"

"${VENV_DIR}/bin/pip" install --quiet -e "${ADAPTER_DIR}"

# Confirm the adapter package is importable.
"${VENV_DIR}/bin/python" -c \
  "import oxi_adapter_smoketest; print('  import: OK')"

# ---------------------------------------------------------------------------
# Step 5 — Run the reconciliation-only tick via a minimal driver.
#
# T0-101 (entry-point adapter auto-discovery) is not yet shipped, so we
# drive the tick through a small Python script that registers the adapter
# manually before calling the CLI's cmd_tick. This mirrors what
# scripts/dogfood-tick.py does for the self-adapter and is explicitly
# called out in docs/first-fork-checklist.md as the current required step.
#
# When T0-101 lands, this driver collapses to `OXI_ADAPTER=... oxi v3 tick`
# or just `oxi v3 tick` (entry-point discovery).
# ---------------------------------------------------------------------------

echo ""
echo "step 5/6: reconciliation-only tick"

DRIVER="${WORK_DIR}/tick-driver.py"
cat > "${DRIVER}" << 'DRIVER_SCRIPT'
"""Minimal tick driver for the first-fork smoke test.

Registers the scaffolded oxi_adapter_smoketest adapter then exercises
the CLI's tick path (reconciliation-only: heartbeat reaper only, no
Claude, no GitHub). This validates:

  - The adapter satisfies the oxi_core Adapter protocol
  - The DB layer initialises cleanly against a fresh database
  - The heartbeat/engine_state machinery completes without error

Exit 0 on success; non-zero on any exception or a non-zero return from
cmd_tick.
"""
from __future__ import annotations

import argparse
import sys

from oxi_adapter_smoketest.adapter import Adapter
from oxi_core.adapter import register_adapter
from oxi_core.cli import cmd_tick

adapter = Adapter()
register_adapter(adapter)

args = argparse.Namespace(times=1, real_claude=False)
rc = cmd_tick(args)
sys.exit(int(rc or 0))
DRIVER_SCRIPT

TICK_OUT="$("${VENV_DIR}/bin/python" "${DRIVER}" 2>&1)" || {
  RC=$?
  echo "first-fork-smoke: FAIL — tick driver exited ${RC}" >&2
  echo "${TICK_OUT}" >&2
  exit 1
}
echo "${TICK_OUT}"

# ---------------------------------------------------------------------------
# Step 6 — Assert the expected output landmarks are present.
# ---------------------------------------------------------------------------

echo ""
echo "step 6/6: assertions"

FAIL=0

if echo "${TICK_OUT}" | grep -q "tick done"; then
  echo "  [OK] 'tick done' found"
else
  echo "  [FAIL] 'tick done' not found in tick output" >&2
  FAIL=1
fi

if echo "${TICK_OUT}" | grep -q "reconciliation-only"; then
  echo "  [OK] 'reconciliation-only' found (no Claude dispatched)"
else
  echo "  [FAIL] 'reconciliation-only' not found" >&2
  FAIL=1
fi

if [[ "${FAIL}" -ne 0 ]]; then
  echo ""
  echo "first-fork-smoke: FAIL"
  echo "  actual tick output:"
  echo "${TICK_OUT}"
  exit 1
fi

echo ""
echo "first-fork-smoke: PASS"
exit 0
