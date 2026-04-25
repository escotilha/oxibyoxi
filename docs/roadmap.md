# oxi dogfood roadmap

The queue the `oxi-adapter-self` dogfood loop picks from. Each item is a bold line shaped `**T{tier}-{N} · {title}**` followed by an italic subtitle — the planner reads this format.

Keep it tight — 10-15 open items at a time. Items ship as individual PRs.

Conventions:

- **Tier 0** — blockers, safety/security, installer bugs. Dispatch first.
- **Tier 1** — user-visible polish, runbooks, CLI ergonomics.
- **Tier 2** — internal cleanup, test coverage, refactors.

The roadmap is auto-pruned weekly by `.github/workflows/roadmap-prune.yml` — items get removed when there's a substantive merged commit on `main` naming the T-id.

---

Every item below has a PR open and auto-merge enabled. The weekly auto-prune workflow removes each line once its T-id appears in a substantive merged commit on `main`.

## Tier 2

**T2-12 · mypy strict typing pass**
_PR [#95](https://github.com/escotilha/oxi/pull/95) (initial allow-list: adapter, db, v3.notification) + PR [#99](https://github.com/escotilha/oxi/pull/99) (6-module ratchet expansion) — auto-merge queued, awaiting CI._

**T2-13 · coverage gate: 85% global**
_PR [#98](https://github.com/escotilha/oxi/pull/98) — auto-merge queued, awaiting CI. Initial floor 85% (current measured ~89%)._

**T2-14 · nightly integration test against live GitHub**
_PR [#105](https://github.com/escotilha/oxi/pull/105) — daily cron probes GhCliClient against the live API; read-only by design._

**T2-15 · benchmark regression guard**
_PR [#104](https://github.com/escotilha/oxi/pull/104) — three benchmarks (db_insert, db_select, dashboard_render) tracked, fail-on-regression at 20% p95 threshold._

**T2-16 · doc lint (lychee + markdownlint)**
_PR [#103](https://github.com/escotilha/oxi/pull/103) — lychee for broken-link detection, markdownlint for style consistency on every docs/ change._

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

- The adapter (`oxi-adapter-self`) enforces `auto_merge=True` (flipped 2026-04-25 in [#106](https://github.com/escotilha/oxi/pull/106) once the critic + CI track record was established). Branch protection on `main` requires `lint-for-leaks` + `python 3.12` to pass before any merge — including engine PRs.
- Budget: hard cap $20/day, $2/task Opus, $0.50/task Sonnet. Tasks that estimate beyond per-task cap get held at `queued` until operator intervention.
- Serial dispatch — `max_concurrent=1`. No fan-out until the single-task loop is stable for two weeks.
- Identifiers here (T0-*, T1-*, T2-*) are what the engine sees. Keep them stable — renaming invalidates handoff snapshots and ledger cross-references.
