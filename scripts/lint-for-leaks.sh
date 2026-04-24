#!/usr/bin/env bash
# lint-for-leaks.sh — CI gate that fails if any forbidden identifier leaks
# into oxi's source or generic docs.
#
# Runs on every PR, every merge to main, and as a release gate.
# See docs/anti-patterns.md §Sanitization for the policy.
#
# Exit codes:
#   0 — clean, no forbidden strings found
#   1 — one or more forbidden strings found (file, line, and string reported)
#   2 — script misuse (e.g., run from wrong directory)

set -euo pipefail

# Resolve repo root from this script's location so it works in worktrees.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ ! -d "${REPO_ROOT}/oxi-core" ]]; then
  echo "lint-for-leaks: cannot locate oxi-core/ from ${REPO_ROOT}" >&2
  exit 2
fi

cd "${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Forbidden-string list. Append-only. See docs/anti-patterns.md §Sanitization.
#
# Each entry is a case-insensitive fixed string (grep -iF). If you need a
# regex exception, add it to the EXEMPT_REGEX section below instead of
# loosening the forbidden list.
# ---------------------------------------------------------------------------

FORBIDDEN=(
  # Project/company names from prior work — never appear in oxi source.
  "contably"
  "psos"
  "nuvini"
  "sourcerank"

  # Private filesystem paths.
  "/opt/contably-os"
  "/opt/psos"
  "/opt/oxi-shadow"
  ".contably-os"

  # Private network identifiers (Tailscale + VPS IPs the author has used).
  "100.77.51.51"
  "100.66.244.112"

  # Private email addresses — oxi commits should use a public-facing address.
  "escotilha@gmail.com"

  # Private repo slugs other than escotilha/oxi.
  "Contably/contably"
  "Contably/contably-os"
  "escotilha/psos"
)

# Paths to scan. Everything shipped to users, plus adapters, docs, the
# sample-project fixture (any leak in the fixture would also be visible),
# and the top-level skills directory (maintenance skills like origin-review
# must stay generic — a leak there would ship to forks).
SCAN_PATHS=(
  "oxi-core/src"
  "adapters/_reference"
  "adapters/_template"
  "docs"
  "sample-project"
  "skills"
)

# Paths explicitly exempt from scanning. These are the only places the
# forbidden strings may legitimately appear (meta-discussion of the policy).
EXEMPT_FILES=(
  "docs/PLAN.md"
  "docs/anti-patterns.md"
  "docs/post-mortems"       # reserved for future external-incident citations
  "docs/release-notes"      # reserved for CVE/upstream citations
  "oxi-core/src/oxi_core/v3/spec_ingest.py"  # contains the forbidden-string list to enforce against
  "oxi-core/tests/test_spec_ingest.py"       # exercises the forbidden-string enforcement
)

# Paths to ignore during scan (binary, generated, or vendored content).
EXCLUDE_DIRS=(
  "__pycache__"
  ".git"
  ".pytest_cache"
  ".ruff_cache"
  "node_modules"
  "dist"
  "build"
  "*.egg-info"
)

# ---------------------------------------------------------------------------
# Build the grep command.
# ---------------------------------------------------------------------------

# Filter SCAN_PATHS to those that actually exist (phase 0 scaffold may omit some).
EXISTING_PATHS=()
for p in "${SCAN_PATHS[@]}"; do
  [[ -e "${p}" ]] && EXISTING_PATHS+=("${p}")
done

if [[ ${#EXISTING_PATHS[@]} -eq 0 ]]; then
  echo "lint-for-leaks: no scan paths exist yet (phase 0 scaffold in progress). Pass."
  exit 0
fi

# Compose exclude-dir flags.
EXCLUDE_FLAGS=()
for d in "${EXCLUDE_DIRS[@]}"; do
  EXCLUDE_FLAGS+=(--exclude-dir="${d}")
done

# Compose exempt-file flags. grep -r doesn't support --exclude with paths,
# so we filter matches after the fact.

# ---------------------------------------------------------------------------
# Run the scan.
# ---------------------------------------------------------------------------

VIOLATIONS=0
TMP="$(mktemp)"
trap 'rm -f "${TMP}"' EXIT

for needle in "${FORBIDDEN[@]}"; do
  # grep -r with fixed strings, case-insensitive, line numbers.
  # `|| true` because grep exits 1 when it finds nothing.
  grep -rniI --fixed-strings "${EXCLUDE_FLAGS[@]}" -- "${needle}" "${EXISTING_PATHS[@]}" > "${TMP}" 2>/dev/null || true

  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue

    # Extract the filename (everything before the first colon).
    file="${line%%:*}"

    # Skip exempt files and directories.
    exempt=false
    for e in "${EXEMPT_FILES[@]}"; do
      if [[ "${file}" == "${e}" || "${file}" == "${e}/"* ]]; then
        exempt=true
        break
      fi
    done
    [[ "${exempt}" == "true" ]] && continue

    echo "FORBIDDEN STRING: '${needle}' in ${line}"
    VIOLATIONS=$((VIOLATIONS + 1))
  done < "${TMP}"
done

# ---------------------------------------------------------------------------
# Report.
# ---------------------------------------------------------------------------

if [[ ${VIOLATIONS} -gt 0 ]]; then
  echo ""
  echo "lint-for-leaks: ${VIOLATIONS} forbidden-string match(es) found."
  echo "See docs/anti-patterns.md §Sanitization for policy + remediation."
  exit 1
fi

echo "lint-for-leaks: clean. Scanned ${#EXISTING_PATHS[@]} path(s) against ${#FORBIDDEN[@]} forbidden string(s)."
exit 0
