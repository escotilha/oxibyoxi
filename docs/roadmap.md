# oxi dogfood roadmap

The queue the `oxi-adapter-self` dogfood loop picks from. Each item is a bold line shaped `**T{tier}-{N} · {title}**` followed by an italic subtitle — the planner reads this format.

Keep it tight — 10-15 open items at a time. Items ship as individual PRs.

Conventions:

- **Tier 0** — blockers, safety/security, installer bugs. Dispatch first.
- **Tier 1** — user-visible polish, runbooks, CLI ergonomics.
- **Tier 2** — internal cleanup, test coverage, refactors.

The roadmap is auto-pruned weekly by `.github/workflows/roadmap-prune.yml` — items get removed when there's a substantive merged commit on `main` naming the T-id.

---

## Tier 2

**T2-12 · mypy strict typing pass**
_add mypy to dev deps + CI job. Start with oxi-core/src/oxi_core/v3/ (the engine core), then expand. strict=True aspirationally; disable_error_code list for acknowledged gaps. Catches a class of bugs tests miss — silent None returns, wrong Protocol conformance, dataclass field types._

**T2-13 · coverage gate: 80% on new code**
_add coverage.py + pytest-cov to CI. Fail the build if a PR introduces new lines under 80% coverage. Keeps the engine from writing untested code via dogfood._

**T2-14 · nightly integration test against real GitHub**
_new CI schedule that runs once a day against a dedicated sandbox repo, exercising: oxi init, pip install, real dispatch (no --real-claude, just the GitHub calls via GhCliClient), real PR open/merge/watch. Catches GitHub API drift + gh CLI breakage. Alerts operator on failure but doesn't block PRs._

**T2-15 · benchmark regression guard**
_record dispatch latency, DB query p50/p95, dashboard render time on every main commit. Publish to a JSON file in the repo + flag regressions > 20%. Prevents slow creep._

**T2-16 · doc lint (lychee + markdownlint)**
_CI job that checks docs/ for broken internal + external links, markdown style consistency. Protects the manual from rot as the codebase evolves underneath it._

---

## Done (moved to release notes)

The 0.1.0a* alpha series and the 0.1.0b1 cut shipped 24 of the original roadmap items:

- T0-1, T0-2, T0-11, T0-101, T0-102, T0-103
- T1-3, T1-4, T1-5, T1-6, T1-7, T1-12, T1-13, T1-14, T1-15, T1-16, T1-17, T1-18
- T2-8, T2-9, T2-10, T2-11
- T3-1, T3-2

See `docs/release-notes/` for the per-version detail.

---

## Notes for the dogfood engine

- The adapter (`oxi-adapter-self`) enforces `auto_merge=False` by default. Operator can flip to `True` once the repo allows auto-merge (currently blocked by GH Free + private repo).
- Budget: hard cap $20/day, $2/task Opus, $0.50/task Sonnet. Tasks that estimate beyond per-task cap get held at `queued` until operator intervention.
- Serial dispatch — `max_concurrent=1`. No fan-out until the single-task loop is stable for two weeks.
- Identifiers here (T0-*, T1-*, T2-*) are what the engine sees. Keep them stable — renaming invalidates handoff snapshots and ledger cross-references.
