# Changelog

All notable changes to oxi (`oxi-core` + `oxi-adapter-reference`) are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [`docs/semver-contract.md`](docs/semver-contract.md).

Per-release detail lives at [`docs/release-notes/v<version>.md`](docs/release-notes/) — those files have the audit trail (PR numbers, dogfood cost, test counts). This file is the scannable summary.

## [0.1.0b1] — 2026-04-24

First beta. Same engine as the 0.1.0a series; promoted from alpha after dogfood validated the install path end-to-end.

- **Stability:** committing to no breaking changes within the `0.1.0b*` line.
- **PyPI classifier:** Alpha → Beta.
- **Release artifacts:** CycloneDX SBOMs now attached to GitHub releases.

[Detail](docs/release-notes/v0.1.0b1.md)

## [0.1.0a5] — 2026-04-24

CONTRIBUTING.md, PyPI project URLs, four new roadmap items (T1-16 SAST, T1-17 SBOM, T1-18 self-healing, T2-11 auto-observation).

[Detail](docs/release-notes/v0.1.0a5.md)

## [0.1.0a4] — 2026-04-24

Packaging hotfix. `oxi init` was crashing on PyPI installs because `adapters/_template/` lived at the repo root and wasn't included in the wheel. Template now ships under `oxi_core/templates/adapter/` as package-data.

[Detail](docs/release-notes/v0.1.0a4.md)

## [0.1.0a3] — 2026-04-24

First-fork-ready release. Adds entry-point auto-discovery (T0-101), `oxi v3 plan --dry-run` (T0-102), first-fork CI smoke test (T0-103), the 5-minute install runbook (T0-1).

[Detail](docs/release-notes/v0.1.0a3.md)

## [0.1.0a2] — 2026-04-24

`__version__` is now read dynamically from installed package metadata. Fixes the 0.1.0a1 bug where `oxi --version` printed `0.0.0` even after upgrading.

[Detail](docs/release-notes/v0.1.0a2.md)

## [0.1.0a1] — 2026-04-24

First usable alpha. Engine runs end-to-end: roadmap ingestion, dispatch pool, argv-form claude invocation, budget hard-cap, PR watcher, auto-merge with critic, deadman, kill-switch, heartbeat, orphan-reap, tail-dispatch, rolling handoff snapshots, localhost dashboard. Adapter protocol with 10 methods. Pre-alpha security audit (#41) addressed all MUST-FIX and SHOULD-FIX findings. 531 tests pass. 0.1.0a1 had a hardcoded `__version__` bug; users should install 0.1.0a2 or later.

[Detail](docs/release-notes/v0.1.0a1.md)

## [0.0.0] — 2026-04-23

PyPI name reservation. `oxi-core 0.0.0` and `oxi-adapter-reference 0.0.0` are scaffold-only — they install but the CLI prints a banner. Working release lands in 0.1.0a*.

[Detail](docs/release-notes/v0.0.0.md)

[0.1.0b1]: https://github.com/escotilha/oxi/releases/tag/v0.1.0b1
[0.1.0a5]: https://github.com/escotilha/oxi/releases/tag/v0.1.0a5
[0.1.0a4]: https://github.com/escotilha/oxi/releases/tag/v0.1.0a4
[0.1.0a3]: https://github.com/escotilha/oxi/releases/tag/v0.1.0a3
[0.1.0a2]: https://github.com/escotilha/oxi/releases/tag/v0.1.0a2
[0.1.0a1]: docs/release-notes/v0.1.0a1.md
