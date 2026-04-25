# Rollback runbook

How to recover from a bad PyPI release of `oxi-core` or `oxi-adapter-reference`.

---

## Decision tree — yank or republish?

```text
Did the bad release cause data loss, secret exposure, or a security
regression (e.g., removed path-traversal guard, broken env whitelist)?
│
├─ YES → yank immediately, then open a security advisory.
│        See § Emergency yank.
│
└─ NO ─→ Is the bug user-visible in normal use
          (CLI crash, wrong output, broken install)?
          │
          ├─ YES → bump the patch/pre-release suffix, republish.
          │        See § Standard republish.
          │
          └─ NO ─→ Is it a cosmetic or edge-case issue
                    that won't confuse users who already installed?
                    │
                    ├─ YES → republish is optional; weigh signal-to-noise.
                    │        A "soft" yank (release notes errata) may be enough.
                    │
                    └─ NO ─→ Escalate: open a GitHub issue, decide with the team.
```

**Default rule for alpha (`0.x.ya*`) releases:** prefer republish over yank.
PyPI yanks are permanent — the version is gone from search results but
not reusable. Alpha users are told to stay on the latest; bumping is cheaper
and cleaner than a yank.

---

## Worked example — 0.1.0a1 → 0.1.0a2

### What happened

`oxi-core 0.1.0a1` shipped on 2026-04-24. The bug: both packages contained a
hardcoded `__version__ = "0.0.0"` in their `__init__.py` files, causing
`oxi --version` to print `0.0.0` regardless of the installed version.

Root cause: two source-of-truth locations for the version string (the code
literal and `pyproject.toml`). Only `pyproject.toml` was bumped during the
release; the code literal was not.

### Decision made

**Republish, do not yank.**

Rationale:

1. No data loss, no security regression, no broken installs. The bug is
   cosmetic — the wrong banner string only.
2. Users who installed `0.1.0a1` could identify it by `pip show oxi-core`
   even though `oxi --version` reported incorrectly.
3. Yanking would remove `0.1.0a1` from PyPI search results permanently,
   creating confusion if anyone had already pinned it.
4. The alpha contract already says "stay on latest"; bumping to `0.1.0a2`
   was the natural signal.

### Fix applied

1. Replaced the hardcoded `__version__ = "0.0.0"` in both `__init__.py`
   files with a dynamic lookup via `importlib.metadata.version()`:

   ```python
   from importlib.metadata import PackageNotFoundError, version

   try:
       __version__ = version("oxi-core")
   except PackageNotFoundError:
       __version__ = "0.0.0+unknown"
   ```

2. Updated `oxi-adapter-reference` to pin `oxi-core==0.1.0a2`.

3. Updated `test_scaffold.py` to assert `__version__` matches the version
   *shape* (`\d+\.\d+\.\d+.*`) rather than a frozen literal — preventing the
   same class of bug from regressing silently in CI.

4. Released `oxi-core 0.1.0a2` and `oxi-adapter-reference 0.1.0a2` via the
   standard `scripts/release.sh` flow.

5. Documented the change in `docs/release-notes/v0.1.0a2.md`.

**`0.1.0a1` was left on PyPI with a status note in its release notes.**
It remains installable and functionally correct for everything except
the version banner. No yank was filed.

---

## Standard republish

Use this path when the bug is user-visible but not a security issue.

### 1. Confirm working tree is clean

```bash
git status
```

Commit or stash any in-progress work. `scripts/release.sh` refuses to proceed
with a dirty tree.

### 2. Fix the bug

Apply the fix on `main` (or a fast-path branch if CI is needed first).
All CI checks must be green before proceeding.

### 3. Bump the version

For a pre-release fix, increment the pre-release suffix:

```text
0.1.0a1 → 0.1.0a2
0.1.0b2 → 0.1.0b3
```

For a stable release fix, increment the patch:

```text
0.1.0 → 0.1.1
```

Edit **only** `pyproject.toml` in the affected package. The version is read
dynamically from package metadata at runtime — do not add any hardcoded string.

If `oxi-adapter-reference` depends on the fixed package, update its pin too:

```toml
# adapters/_reference/pyproject.toml
dependencies = ["oxi-core==0.1.0a2"]  # was 0.1.0a1
```

### 4. Update release notes

Add `docs/release-notes/v<new-version>.md`. Follow the existing format. State
the previous version's status explicitly (e.g., "still on PyPI, still
installable, not yanked").

### 5. Commit and push

```bash
git add pyproject.toml docs/release-notes/v<new-version>.md
git commit -m "fix(release): <one-line description> + bump to <new-version>"
git push
```

Wait for CI to go green.

### 6. Release

```bash
scripts/release.sh oxi-core
# if the reference adapter pin changed:
scripts/release.sh adapters/_reference
```

The script runs leak-lint, builds both sdist and wheel, performs a local
install smoke test, then uploads. See `docs/runbooks/release.md` for full
prerequisites.

### 7. Verify on PyPI

Open `https://pypi.org/project/oxi-core/` and confirm the new version appears.
Install in a fresh venv and run `oxi --version`:

```bash
python -m venv /tmp/oxi-verify && source /tmp/oxi-verify/bin/activate
pip install oxi-core==<new-version>
oxi --version   # must print the new version
deactivate && rm -rf /tmp/oxi-verify
```

### 8. Communicate

If any users were on the bad version, post an errata note. For alpha
releases this is typically a short comment in the GitHub release or a note
in the roadmap doc.

---

## Emergency yank

Use this path when the release poses a security risk or has a data-loss bug.

### 1. Yank on PyPI immediately

Go to `https://pypi.org/manage/project/<pkg>/releases/<version>/` and click
**Yank**. Add a yank reason (e.g., "security regression: path-traversal guard
removed"). Yanked versions no longer appear in `pip install <pkg>` without an
explicit version pin.

> **Yanked versions are never reusable.** You cannot re-upload under the same
> version number. The next release must have a new version.

### 2. Open a security advisory

If the issue is a CVE-class vulnerability, open a GitHub Security Advisory
under the repository's Security tab before announcing publicly. This triggers
a coordinated disclosure window.

### 3. Fix and republish under a new version

Follow the Standard republish steps above, starting with a new version number.

### 4. Notify affected users

Post a clear public notice on the GitHub release page and in any community
channels. State: which versions are affected, what the impact is, what version
to upgrade to.

---

## Useful commands

```bash
# What version is installed?
pip show oxi-core | grep Version
oxi --version

# List all versions on PyPI (including yanked)
pip index versions oxi-core 2>/dev/null || pip install --dry-run oxi-core==nonexistent 2>&1 | grep "from versions"

# Pin to a known-good previous version
pip install "oxi-core==0.1.0a1"

# Dry-run release to TestPyPI before touching PyPI
scripts/release.sh oxi-core --test
```

---

## What not to do

- **Do not attempt to re-upload under a yanked version number.** PyPI will
  reject it. The version slot is gone permanently.
- **Do not `--no-verify` the commit hooks to speed up a hotfix.** The hooks
  exist to catch the class of mistake that caused the incident. They take
  seconds; skipping them re-exposes the risk. See
  [anti-patterns §9](../anti-patterns.md#9-no-no-verify-no-force-push-to-shared-branches).
- **Do not push a release directly from a dirty working tree.** `scripts/release.sh`
  enforces a clean tree; do not work around this check.
- **Do not embed the version string in source code.** The `0.1.0a1` incident
  was caused by exactly this pattern. Use `importlib.metadata.version()` as
  shown in the worked example.

---

## Related documents

- `docs/runbooks/release.md` — standard release workflow and PyPI prerequisites
- `docs/release-notes/v0.1.0a2.md` — the `0.1.0a1 → 0.1.0a2` incident record
- `docs/anti-patterns.md` — rules this runbook enforces operationally
