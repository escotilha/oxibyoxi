# Semver contract

What every version segment promises (and doesn't).

## Versioning scheme

oxi follows [PEP 440](https://peps.python.org/pep-0440/) with a 4-segment shape: `MAJOR.MINOR.PATCH<phase><n>`.

Examples: `0.1.0a1`, `0.1.0a5`, `0.1.0b1`, `0.1.0rc1`, `0.1.0`, `0.2.0a1`.

## What each phase commits to

| Phase | Adapter protocol | CLI surface | DB schema | Recommendation |
|---|---|---|---|---|
| **alpha** (`a*`) | may break between alphas | may break between alphas | may break (auto-migrate where possible, document otherwise) | Pin exactly. `pip install oxi-core==0.1.0a4`. |
| **beta** (`b*`) | stable within `MAJOR.MINOR.0b*` | stable within `MAJOR.MINOR.0b*` | additive only within line | Pin minor. `pip install --pre oxi-core==0.1.*` allows beta + RC. |
| **release candidate** (`rc*`) | stable within `MAJOR.MINOR.0rc*` | stable within `MAJOR.MINOR.0rc*` | stable within line | Pin minor. Last chance to surface stable-blockers. |
| **stable** (`MAJOR.MINOR.PATCH`) | stable within `MAJOR.*` | stable within `MAJOR.*` | additive only within MAJOR | Pin major. `oxi-core>=1.0,<2.0`. |

## What changing each segment means

- **MAJOR bump** (`0 → 1`, `1 → 2`): adapter protocol may break. Existing adapters need code changes to upgrade.
- **MINOR bump** (`0.1 → 0.2`): adapter protocol stays compatible; new optional methods may appear. Existing adapters work unchanged.
- **PATCH bump** (`0.1.0 → 0.1.1`): bugfix only. No new features, no protocol changes.
- **Phase bump** within version (`0.1.0a5 → 0.1.0b1`): readiness signal only. No code-level promises beyond the previous phase.
- **Phase number** (`0.1.0a4 → 0.1.0a5`): incremental work within the alpha line. No promises within the alpha series.

## What "stable within the line" means concretely

If your code works against `0.1.0b1`:

- `0.1.0b2`, `0.1.0b3`, etc.: works without changes.
- `0.1.0rc1`: works without changes (release candidates only fix beta-bug regressions).
- `0.1.0` (stable): works without changes.
- `0.1.1`, `0.1.2`: works without changes.
- `0.2.0a1` and beyond: may require adapter changes; check the migration note in that release's notes.

## Pre-1.0 reality

Until `1.0.0`, oxi reserves the right to break the protocol with a MINOR bump if dogfood feedback warrants it. Each MINOR bump publishes a migration note in `docs/release-notes/`. Before pinning to a MINOR, read the migration note for the next planned MINOR — if you can't accept the changes, pin tighter.

After `1.0.0`, MINOR bumps are additive only.

## The `--pre` flag during alpha/beta

`pip install oxi-core` (no flags) will skip alpha and beta versions until `1.0.0`. To install pre-1.0 oxi:

```bash
pip install --pre oxi-core         # picks the latest pre-release (currently 0.1.0b1)
pip install oxi-core==0.1.0b1     # pin exactly
pip install "oxi-core>=0.1.0b1,<0.2"  # allow patch + RC, block 0.2 breakages
```

## Yanks

A version that ships a critical bug (data loss, security regression) gets yanked from PyPI. `--pre` will skip yanked versions. Yank decisions are documented in `docs/runbooks/rollback.md`.

So far: zero yanks. The 0.1.0a3 packaging bug was fixed in 0.1.0a4 within an hour; old installs that already pulled a3 keep working as long as the operator doesn't re-init.
