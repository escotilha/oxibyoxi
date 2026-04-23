# Oxi — Plan

**Repo:** `escotilha/oxi` (private)
**Status:** Plan only. No code until Phase 0 is approved.
**Last revised:** 2026-04-23

---

## 1. What oxi is

Oxi is a standalone, forkable autonomous coding orchestrator. It runs a loop that reads a roadmap, plans work, dispatches parallel Claude Code sessions to execute the work, opens PRs, runs a critic gate, merges what passes, and ships a daily brief. Anyone can `pip install oxi-core`, run `oxi init` against their own repo, and get a working orchestrator for their own project.

Target user: a solo engineer or small team who wants to ship features faster by having Claude write them against a roadmap, with human review at PR time. Not a coding agent. Not a chat assistant. A **delivery loop**.

Design constraints:
- **Forkable from day one.** No hardcoded project name, repo slug, path, or prompt. Everything project-specific is in an adapter. The core package makes no assumptions about what project it's driving.
- **One binary, one command.** `oxi init` scaffolds. `oxi v3` runs the loop. `oxi status` shows state. No sprawling CLI surface.
- **Opinionated defaults.** Sensible plan-tier, sensible budget caps, sensible skill weights. Users override via adapter, not by reading source.
- **PyPI-distributed.** `oxi-core` and reference adapters publish to PyPI. Installing oxi is a one-line `pip install`.
- **Python 3.11+.** No backward-compat with older interpreters.
- **SQLite-backed state.** No Postgres requirement. Users who want Postgres write an adapter.

## 2. Why a new project instead of evolving an existing orchestrator

Because the author has prior experience trying to generalize an embedded orchestrator, and the lessons are costly:

- Generalizing in-place invites rename refactors that leak path literals, SQL table names, and env var names through hardcoded strings that grep misses.
- Cutting over from an embedded version to a generalized version without a long shadow-run window regresses delivery.
- Introducing an adapter protocol, a wizard, a contribution protocol, and module renames in the same release multiplies the surface area for regressions.
- A one-command rollback is non-negotiable, and embedded orchestrators rarely have one.

These lessons are captured as **anti-patterns in Section 7** and enforced as CI invariants and runbook checklists. They apply to any orchestrator project; they're not specific to oxi's origin.

Fresh repo, fresh package, fresh PyPI name. No rename migration to plan, because there's nothing to migrate from.

## 3. Non-goals

- **Not a replacement for an existing orchestrator.** Oxi is a new product. It does not claim to replace, supersede, or migrate from anything.
- **Not a chat UI.** No ChatOps, no web app, no conversation history. It's a headless daemon plus a CLI plus an HTML dashboard.
- **Not a CI/CD system.** It opens PRs. CI systems run on those PRs. Oxi does not run tests itself; it reads PR check status.
- **Not a multi-tenant SaaS.** Each fork is its own install, its own DB, its own budget. No central control plane.
- **Not a replacement for human review.** PRs still need a human merge (or an auto-merge policy the human configured). The critic gate assists review; it doesn't replace it.

## 4. Target architecture

```
escotilha/oxi/
├── oxi-core/                         Engine package → PyPI as oxi-core
│   ├── pyproject.toml                name="oxi-core", console_script "oxi"
│   ├── src/oxi_core/
│   │   ├── cli.py                    CLI entrypoint. Commands: init, v3, status, brief.
│   │   ├── adapter.py                Adapter protocol + dataclasses + registration API.
│   │   ├── defaults.py               Fallback constants used when adapter omits a field.
│   │   ├── policy.py                 Skill weights, plan-tier, dispatch policy.
│   │   ├── wizard.py                 `oxi init` 8-step bootstrap.
│   │   ├── db.py                     SQLite schema + migrations.
│   │   ├── planner.py                Reads roadmap, emits task plans.
│   │   ├── critic.py                 Reviews plans before dispatch, reviews PRs before merge.
│   │   ├── prompts.py                Templated prompts for planner/critic/dispatch.
│   │   ├── v3/
│   │   │   ├── dispatch.py           Spawns claude -p sessions in worktrees.
│   │   │   ├── dispatch_pool.py      Host selection (local, remote via SSH).
│   │   │   ├── worktree_provision.py Git worktree lifecycle.
│   │   │   ├── heartbeat.py          Reaps stalled tasks, replans.
│   │   │   ├── pr_watcher.py         Watches GH PR status, transitions tasks.
│   │   │   ├── auto_merge.py         Post-CI critic gate + merge.
│   │   │   ├── ship_recovery.py      Recovers from failed dispatches.
│   │   │   ├── seed_from_roadmap.py  Parses roadmap markdown, creates tasks.
│   │   │   ├── ingest_roadmap.py     Roadmap-to-task mapping, tier-0 absorb logic.
│   │   │   ├── cost.py               Per-task + daily spend tracking.
│   │   │   ├── brief.py              Daily markdown recap generation.
│   │   │   ├── dashboard.py          HTTP server for the dashboard.
│   │   │   ├── dashboard_html.py     HTML rendering.
│   │   │   ├── tracker.py            Task state machine.
│   │   │   ├── ledger_events.py      Append-only event log.
│   │   │   ├── kill.py               Killswitch file handling.
│   │   │   ├── oauth_watch.py        OAuth token health check.
│   │   │   ├── handoff.py            Context-handoff artifact writer.
│   │   │   └── council.py            Multi-reviewer swarm for critical gates.
│   │   └── skills/
│   │       └── _bundled/             Skill definitions shipped with oxi-core.
│   └── tests/
│
├── adapters/
│   ├── _reference/                   Reference adapter. Drives a toy sample repo.
│   │                                 Published to PyPI as oxi-adapter-reference.
│   │                                 Used for docs, for tests, for dogfooding the wizard.
│   └── _template/                    `oxi init` scaffolds from this.
│
├── sample-project/                   Tiny standalone repo oxi can drive for demos.
│                                     Not a real product. Just something oxi operates on
│                                     to prove the loop works end-to-end.
│
├── docs/
│   ├── PLAN.md                       this file
│   ├── architecture.md               Phase 1 deliverable. Engine internals.
│   ├── adapter-protocol.md           Phase 2 deliverable. How to write an adapter.
│   ├── wizard.md                     Phase 3 deliverable. The 8-step `oxi init` UX.
│   ├── runbooks/
│   │   ├── install.md
│   │   ├── upgrade.md
│   │   ├── rollback.md
│   │   └── troubleshoot.md
│   ├── anti-patterns.md              Section 7 of this plan, elaborated.
│   └── release-notes/                Per-version changelog.
│
└── scripts/
    ├── lint-for-leaks.sh             CI: grep for forbidden strings in oxi-core source.
    ├── release.sh                    Build wheel, tag, upload to PyPI.
    └── smoke/                        End-to-end smoke tests against sample-project.
```

## 5. Distribution

- **PyPI.** `oxi-core`, `oxi-adapter-reference`. Users run `pip install oxi-core oxi-adapter-reference` (or their own adapter).
- **Rationale:** PyPI is the default distribution channel every Python tool oxi would be compared to (ruff, uv, poetry, hatch, mkdocs, black) uses. Git+URL installs signal alpha/hobby project; PyPI signals "installable, versioned, real." Cost to the author is one `twine upload` per release.
- **Versioning:** SemVer. `0.x` during pre-1.0 while the adapter protocol stabilizes. `1.0` when the protocol is frozen.
- **Release cadence:** No schedule. Ship when ready. Breaking changes in minor versions during 0.x; in major versions after 1.0.

## 6. Phased delivery

Each phase has **entry**, **goal**, **work**, **exit**, **rollback**. Phases ship sequentially. A phase doesn't end until its exit criteria are met. Oxi is the only product being planned; there's no concurrent production system to protect, so phase gates are about product quality, not migration safety.

The operator (Pierre) owns all 14-day observation windows.

### Phase 0 — Repo bootstrap (1 day)

**Entry:** This plan approved.

**Work:**
1. Populate the directory layout from Section 4 with stubs and READMEs.
2. `.github/workflows/ci.yml` — runs `pytest`, `ruff`, and `scripts/lint-for-leaks.sh` on every PR.
3. `scripts/lint-for-leaks.sh` — a grep-based CI check that fails if any forbidden string appears in `oxi-core/src/`. Forbidden list defined in `docs/anti-patterns.md §Sanitization`; starts with project names, paths, and IPs from the source-inspiration project, plus obvious leaks like real email addresses.
4. `pyproject.toml` for `oxi-core` with minimal dependencies.
5. `docs/anti-patterns.md` — the 7 rules from Section 7, elaborated with examples.
6. Open GitHub Project "Oxi v0.1". Columns: backlog, in-flight, done.
7. Open issues for Phase 1 work items.

**Exit:** CI green on an empty commit. `pip install -e oxi-core/` from the repo succeeds in a fresh venv.

**Rollback:** none. Admin phase.

**Earliest Phase 1 start:** same day.

---

### Phase 1 — Engine scaffold (2 weeks)

**Entry:** Phase 0 exit.

**Goal:** `oxi-core` has the 9-step loop skeleton, a reference adapter, and a sample project. The loop runs end-to-end against the sample project — plans one task, dispatches one Claude session, opens one PR, runs the critic, merges. All in isolation on the author's Mac. No VPS yet. No real work yet. Proves the architecture is sound.

**Work:**

1. **`sample-project/`.** A tiny Python/Markdown repo that oxi can drive. Contents: a README, a `roadmap.md` with three toy tasks ("add a health endpoint", "add a CLI flag", "write a changelog entry"), an empty `src/`, a pytest suite. Lives as a subdir of the oxi repo initially; extracted to its own repo once the loop works.

2. **Adapter protocol.** Define `Adapter` as a Protocol with ~10 methods covering: naming, paths, budget caps, github repo, roadmap location, branch prefixes, dispatch hosts, promote recipe. Dataclasses for each return type. **Write the protocol before writing any core code that consumes it** — core reads through the adapter from day one. No `defaults-only` phase to retrofit later.

3. **`oxi-adapter-reference`.** Adapter implementation that drives `sample-project/`. This is the reference implementation and the test fixture.

4. **Core modules.** Port the 9-step loop architecture from reference research (the author has prior experience with a similar engine). Each module is written fresh, not copy-pasted. As each module lands:
   - It must read all project-specific values from the adapter.
   - It must not contain any string literal referring to any specific project, path, or user.
   - It ships with tests that run against the reference adapter.

5. **CLI surface.** `oxi init` (stub — Phase 3), `oxi v3 tick --times N`, `oxi v3 status`, `oxi v3 kill`, `oxi brief`. Keep it small.

6. **Local dashboard.** HTML dashboard on localhost. Shows tasks, events, current tick. No auth — it's localhost.

7. **Smoke test.** `scripts/smoke/end-to-end.sh` — starts from a clean DB, seeds from sample-project roadmap, runs 5 ticks, asserts one task reaches `merged`. Runs in CI.

**Exit:**
- Smoke test passes in CI.
- `lint-for-leaks.sh` passes — zero forbidden strings in `oxi-core/src/`.
- Tagged `v0.1.0-alpha.1`. Not published to PyPI yet.
- Author can `pip install -e .` on their laptop, run `oxi v3 tick --times 5` against sample-project, and see a PR get opened, critic'd, and merged.

**Rollback:** none required. Phase 1 is build-only; nothing is deployed anywhere.

**Earliest Phase 2 start:** Phase 1 exit.

---

### Phase 2 — Dogfooding on oxi itself (14 days observation)

**Entry:** Phase 1 exit.

**Goal:** Oxi ships its own PRs. Oxi's roadmap lives in `docs/roadmap.md` in this repo. Oxi's dispatch runs on the author's Mac (or Mac Mini via SSH), opens PRs against `escotilha/oxi`, and merges them after critic + CI. Bugs found in dogfooding get fixed in oxi itself. Classic self-hosting loop.

**Work:**

1. **Write `docs/roadmap.md`** with 10–15 small oxi improvements: error-message polish, missing tests, doc stubs, a couple of refactors. Not critical work; bite-sized tickets where a failed dispatch hurts nothing.

2. **Create an `oxi-adapter-self` package** (lives in this repo, not PyPI, not shipped). Reads oxi's own roadmap, ships PRs to oxi's own repo. **Uses oxi to build oxi.**

3. **Observe for 14 days.** Every dispatch, PR, merge, and failure is logged. Operator reviews every PR before merge (no auto-merge during dogfooding). Any bug found in oxi → a new roadmap item → eaten by the loop. Meta.

4. **14-day exit criteria:**
   - At least 10 PRs merged via the loop with no operator bypass.
   - Zero incidents where the engine corrupts its own DB, worktree, or repo state.
   - Zero incidents where dispatch runs silently without progress (the `last_progress_at` bug class — if it happens once, Phase 2 resets).
   - A written post-dogfood review listing every bug and how it was fixed.

5. **Publish to PyPI** at the end of Phase 2 as `oxi-core 0.1.0` and `oxi-adapter-reference 0.1.0`. First public release.

**Exit:** 14 days clean + PyPI release + post-dogfood review doc.

**Rollback trigger:**
- Any DB/worktree/repo corruption: stop dogfooding, fix, restart the 14-day counter.
- Three consecutive days without a successful merge: stop, investigate throughput bug, restart.

**Rollback mechanism:** `oxi v3 kill` + operator manually reverts any bad PRs. Blast radius is one repo (oxi's own); no production elsewhere to protect.

**Earliest Phase 3 start:** Phase 2 exit.

---

### Phase 3 — `oxi init` wizard (2 weeks)

**Entry:** Phase 2 exit.

**Goal:** A stranger can `pip install oxi-core`, run `oxi init` against their own repo, answer 8 prompts, and end up with a working adapter package wired into their codebase. Zero hand-editing of Python.

**Work:**

1. **8-step wizard flow**, following the structure from design work prior to this repo (inherited design, adapted for oxi). Prompts collect: project name, repo slug, branch prefixes, roadmap path, budget caps, plan tier, dispatch hosts, secrets pattern.

2. **Scaffolding.** Wizard writes:
   - `oxi-adapter-<project>/` package under the user's chosen path.
   - `pyproject.toml` for the adapter.
   - A registration call so `oxi v3` picks up the adapter on next run.
   - A `.oxi/` config directory with the collected values.

3. **Validation.** After scaffold, wizard runs `oxi v3 status --dry-run` to confirm the adapter loads and the roadmap parses. If either fails, wizard explains and exits non-zero.

4. **User test.** Author runs the wizard against a second throwaway project (`sample-project-two`, a sibling to `sample-project`) as if they were a new user. If the wizard has a bad UX step, fix before shipping.

5. **Documentation.** `docs/wizard.md` written from the user-test experience.

**Exit:**
- `oxi init` scaffolds `sample-project-two` from scratch, runs 3 ticks, merges 1 PR. No manual editing.
- `docs/wizard.md` matches what the wizard actually does.
- `oxi-core 0.2.0` published to PyPI.

**Rollback:** wizard is additive — if it's broken, users skip it and write the adapter by hand. Can ship the release without the wizard if needed and follow up.

**Earliest Phase 4 start:** Phase 3 exit.

---

### Phase 4 — Stabilization + docs (ongoing, no hard timeline)

**Entry:** Phase 3 exit.

**Goal:** Oxi is boring and well-documented.

**Work (each a separate small release, each can ship independently):**

1. Runbooks: install, upgrade, rollback, troubleshoot. Write from real incidents during Phases 1–3, not imagined ones.
2. Bug fixes from continued dogfooding. Dogfood runs indefinitely.
3. Cost-tracker polish. Daily spend caps. Alerting on overspend.
4. Council (multi-reviewer swarm) for critical auto-merge decisions — optional, adapter-enabled.
5. Pattern detector — engine notices its own repeating failure modes and flags them. Optional.
6. Deep-fix escalation — when a task fails 3x, escalate to a longer-reasoning skill. Optional.
7. Public README rewrite for discoverability. Key phrase: "forkable autonomous coding orchestrator."
8. Semantic-versioning commitment. Freeze the adapter protocol at `1.0`.

**Exit:** `oxi 1.0.0` published when the adapter protocol has been stable for 60 days with no required changes.

**Rollback:** per feature. Each Phase 4 item is small enough to revert cleanly.

---

### Phase 5 — Contribution path (only if organic interest appears)

**Entry:** Multiple real forks exist in the wild, AND a fork owner has opened a PR back to oxi offering an improvement.

**Goal:** A minimal contribution protocol for upstream improvements. Don't build this on spec — build it when there's real demand.

Design sketched in prior work (inherited design, can port over when the time comes). Not a blocker for 1.0.

---

## 7. Anti-patterns encoded as guardrails

Every item is enforced by either CI, tests, or a runbook checklist. Not just written down.

| Anti-pattern | Enforcement |
|---|---|
| **One release ships multiple fundamental changes.** | Every release has one headline change. Release notes enumerate it. PRs that bundle a rename with a refactor with a new feature are split during review. |
| **DB path default mismatches the systemd flag.** | Unit test reads the default from code and from the shipped systemd unit, asserts they're equal. Runs in CI on every PR. |
| **Plan tier hardcoded instead of configured.** | Plan tier is an adapter method with no default. Missing → engine refuses to start. `oxi v3 status` prints the active tier at the top of every output. |
| **Rename refactor leaks path literals.** | `lint-for-leaks.sh` — CI grep for forbidden strings. List includes any project name, path, email, or IP that should never appear in `oxi-core/src/`. The list grows as leaks are found and fixed. |
| **Venv move without rebuild.** | Install runbook explicitly includes venv rebuild + `which oxi` verification steps. |
| **Cutover without shadow-run.** | Not applicable to oxi directly (no migration), but the principle is: every major release ships to a side-by-side test env first (Phase 2 dogfooding pattern applies to every subsequent major). |
| **Rollback is more than one command.** | Every release ships with a `scripts/rollback.sh` that reverts the install in one step. Tested in CI by installing N-1, upgrading to N, then rolling back to N-1. |
| **Secrets in committed files.** | `gitleaks` runs in CI. Secrets live only in env vars, loaded from OS keychain locally and GHA Secrets remotely. |
| **`--no-verify` on commits.** | Branch protection rejects pushes with skipped hooks. |

## 8. Sanitization discipline (the important one)

Because oxi's origin involves studying an existing private codebase, the risk is that a string literal leaks. Controls:

1. **Every file under `oxi-core/src/` is written from scratch.** No copy-paste. Even if a function's shape mirrors a reference, the author retypes it. Variable names, comments, docstrings are authored fresh.

2. **Forbidden-string list in `scripts/lint-for-leaks.sh`.** Includes project names, repo slugs, paths, IPs, email addresses, and any other identifier from the reference source. The list is maintained in `docs/anti-patterns.md §Sanitization` and reviewed on every PR that touches it.

3. **CI check runs on every PR, on every merge to main, and as a pre-release gate.** A release cannot ship until `lint-for-leaks.sh` passes.

4. **Review discipline.** Every PR gets read with the question "would I be comfortable if this landed in a public repo?" Even though oxi is private now, it's designed as a public product; sanitization happens now, not at public-launch time.

5. **Commit messages are sanitized too.** No references to the inspiration project in git history. Authors who paste a traceback into a commit message check it first.

## 9. What stays off the roadmap forever (unless explicitly added)

- Tight coupling to any one project.
- Any feature that only makes sense for a specific vertical (e.g., "Brazilian fiscal document handling").
- Hardcoded cloud provider assumptions (Oracle, AWS, GCP — all are user-provided via adapter).
- Hardcoded CI system assumptions (GitHub Actions is assumed; adding GitLab CI support is a future adapter, not a core change).
- A central control plane. Each install is standalone.
- A web UI. The dashboard is localhost HTML; anything fancier is a future product.

## 10. Decisions locked in

1. **Fresh product, no cutover, no Contably mention ever.** Confirmed.
2. **PyPI distribution.** Confirmed. `oxi-core` and `oxi-adapter-*` names reserved on PyPI at Phase 0.
3. **Operator owns 14-day observation windows.** Confirmed.
4. **Sanitization is a release gate, not a nice-to-have.** Confirmed via Section 8.
5. **Contably OS stays exactly as it is.** Not touched. Not replaced. Not referenced.

## 11. Immediate next action

Phase 0:

1. Reserve `oxi-core`, `oxi-adapter-reference` on PyPI (upload an empty placeholder `0.0.0` package to claim the name — costs nothing, prevents squatting).
2. Scaffold the directory layout from Section 4 into this repo.
3. Write `docs/anti-patterns.md` (the 7 rules + the sanitization list, elaborated).
4. Write `scripts/lint-for-leaks.sh` with the initial forbidden-string list.
5. Wire `.github/workflows/ci.yml`.
6. Open Phase 1 issues in the GitHub Project.

Operator approval of this revised plan unblocks Phase 0 the same day.
