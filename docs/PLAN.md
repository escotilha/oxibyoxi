# Oxi — Extraction Plan

**Repo:** `escotilha/oxi` (private, fresh)
**Source:** `Contably/contably-os` @ `2c7c195` (the rollback-pinned production commit of `contably-os 0.4.6`)
**Reference of what-not-to-do:** `escotilha/psos` — the 0.5.0 attempt that regressed 40h of delivery and was rolled back 2026-04-23.
**Author:** planning session, 2026-04-23.
**Status:** Plan only. No code written in this repo beyond this doc until Phase 0 is approved.

---

## 1. Why this exists

Contably OS is the autonomous coding orchestrator that ships Contably. Today it lives entangled in a Contably-owned repo, tied to Contably-specific paths, table names, prompts, and secrets. The goal is a **standalone, forkable orchestrator** — code that a stranger can clone, run `oxi init`, and use to drive their own project's roadmap with the same 9-step loop.

Two prior attempts at this:

1. **`Contably/contably-os` extracted to its own repo (2026-04-22)** — a `git filter-repo` out of `Contably/contably/packages/`. The repo exists but is flagged deprecated and never became truly portable.
2. **`escotilha/psos` 0.5.0 (2026-04-22 → 23)** — introduced seven new modules at once (`adapter.py`, `defaults.py`, `policy.py`, `wizard.py`, `contribute.py`, `_compat/`, `v3/sandbox.py`, `v3/pattern_detector.py`) plus renamed every `contably_os_*` table to `psos_*`. Cutover to production regressed delivery for 40 hours (tasks reaped at 8s, plan-tier hardcoded to 5× instead of 20×, `psos_task` pointed at empty stub DB). Rolled back to `contably-os 0.4.6` @ `2c7c195`. **That attempt is the anti-pattern this plan is designed to avoid.**

Oxi is attempt #3. The premise: **extract first, rename later, add new features never** (until parity is proven in production for 14 days).

## 2. Non-goals

Spelled out because 0.5.0 violated all of them:

- **Not a rewrite.** No module gets refactored for cleanliness during extraction. Diff between `oxi-core` and `contably-os 0.4.6` source must be minimal and mechanically auditable.
- **Not a renaming.** Database tables, env vars, systemd unit names, file paths on the VPS stay `contably_os_*` for Phase 1 and 2. Renames are Phase 4, gated by production parity.
- **No new features.** The `adapter.py` / `defaults.py` / `policy.py` trio from 0.5.0 is the eventual design target, but Phase 1 does not ship any of it. Forkability is implemented by convention + env vars first; structured adapter is Phase 3.
- **No wizard in Phase 1.** `oxi init` is Phase 5.
- **No upstream contribution protocol in Phase 1–4.** Phase 5+.

## 3. Guiding principles

Derived directly from the 0.5.0 post-mortem:

| Principle | How 0.5.0 violated it | How oxi honors it |
|---|---|---|
| **One change per release.** | Renamed tables + introduced adapter + added wizard + new modules all in v0.5.0. | Phases ship sequentially. Phase N cannot start until Phase N-1 runs in production for 14 days with zero engine-caused regressions. |
| **Database path defaults must match systemd `--db` flags.** | CLI defaults opened `/opt/contably-os/contably-os.db` (empty stub); systemd opened `/opt/psos/psos/psos.db`. | Every CLI entrypoint resolves its DB path through one function. That function is tested on every PR against the exact path the systemd units use. |
| **Plan-tier is config, not a literal.** | `5x` hardcoded in three defaults files. Production runs Max 20x. | Plan tier is a single env var `OXI_PLAN_TIER`, read once at startup, logged at startup, surfaced in `oxi v3 status`. |
| **Rename refactors need a grep-the-world pass.** | `contably_os_task` → `psos_task` missed four string literals inside SQL written as raw strings. | Renames in Phase 4 are done by a single rename script that produces a diff preview; the script greps for the legacy name across the codebase, the systemd unit files, the DB path, and the VPS filesystem before committing. |
| **Venv rebuild on install-dir move.** | Moving `/opt/contably-os/venv` → `/opt/psos/venv` didn't rebuild entrypoints; pip console-scripts have absolute shebangs. | Phase 2 VPS migration explicitly recreates the venv; the runbook includes a `which oxi` check before and after. |
| **Shadow-run before cutover.** | 0.5.0 cutover was a hard swap. When it regressed, 40h of delivery was already gone. | Phase 2 runs `oxi-core` in **shadow mode** against a second DB while `contably-os 0.4.6` continues to drive production. Parity is measured for 14 days before the swap. |
| **Rollback must be one command.** | 0.5.0 rollback required `git checkout`, venv reinstall, DB re-migration, symlink fight. | Every VPS change ships with a paired `rollback.sh` that reverts in one step. |

## 4. Target architecture (end state, not Phase 1)

```
escotilha/oxi/
├── oxi-core/                         Engine package, forkable
│   ├── pyproject.toml                name="oxi-core", console_script "oxi"
│   ├── src/oxi_core/
│   │   ├── cli.py                    Same 9-step loop as contably_os.cli
│   │   ├── v3/                       All v3/* modules, 1:1 from 0.4.6
│   │   ├── adapter.py                Protocol (Phase 3)
│   │   ├── defaults.py               Fallback constants (Phase 3)
│   │   ├── policy.py                 Skill weights + plan-tier policy (Phase 3)
│   │   └── wizard.py                 `oxi init` 8-step bootstrap (Phase 5)
│   └── tests/
│
├── adapters/
│   ├── contably/                     Reference implementation. Contably-specific
│   │   ├── pyproject.toml            name="oxi-adapter-contably"
│   │   ├── src/contably_adapter/
│   │   │   ├── __init__.py           register_adapter() call
│   │   │   └── config.py             NamingConfig, PathsConfig, BudgetCaps overrides
│   │   └── deploy/                   systemd units, dispatch scripts
│   └── _template/                    `oxi init` scaffolds from this
│
├── docs/
│   ├── PLAN.md                       this file
│   ├── architecture.md               Phase 3 deliverable
│   ├── wizard.md                     Phase 5 deliverable
│   ├── migration/                    runbooks per phase
│   │   ├── phase-1-extraction.md
│   │   ├── phase-2-shadow-run.md
│   │   ├── phase-3-adapter-wiring.md
│   │   ├── phase-4-rename.md
│   │   └── phase-5-wizard.md
│   └── post-mortems/
│       └── psos-0.5.0-rollback.md    the anti-pattern, locked in as reference
│
└── scripts/
    ├── extract-from-contably-os.sh   Phase 1 one-shot extraction script
    ├── rollback/                     one-command reverts per phase
    └── parity/                       Phase 2 shadow-run harness
```

## 5. Phased sequencing

Each phase has: **entry criteria**, **work**, **exit criteria**, **rollback trigger**, **earliest next-phase start**. A phase does not end until exit criteria are met in *production*. Rollback triggers are automatic — if any fire, revert the phase and re-plan.

### Phase 0 — Repo bootstrap (1 day)

**Entry:** none.

**Work:**
1. Create `escotilha/oxi` on GitHub (private).
2. Push this `PLAN.md` as the first commit on `main`.
3. Create `.github/workflows/ci.yml` — runs `pytest` and `ruff` on every PR.
4. Create issue template `phase-exit.md` — checklist per phase.
5. Create GitHub Project "Oxi Extraction" with columns for each phase.
6. Open phase-1 issue, link to runbook stub.

**Exit:** repo exists, CI green on an empty commit, phase-1 issue open.

**Rollback trigger:** none (admin phase).

**Earliest Phase 1 start:** same day.

---

### Phase 1 — Mechanical extraction (3 days of work, 0 days of production risk)

**Entry:** Phase 0 exit. Pierre approval of this plan.

**Goal:** `oxi-core` package exists, is `pip install`-able, runs the exact same 9-step loop as `contably-os 0.4.6`, against the *same* database schema, against the *same* Contably repo, with zero behavioral difference.

**Work:**

1. **Extract source with history.** Use `git filter-repo` to copy the 42 `contably_os/**.py` files and their full history from `Contably/contably-os` at `2c7c195` into `oxi-core/src/oxi_core/`. Preserves authorship and blame.

2. **Rename the Python package name only.** `src/contably_os/` → `src/oxi_core/`. Update every `from contably_os` import. Use a single `sed` pass + AST verification. Do not touch:
   - SQL table names (`contably_os_task`, `contably_os_event`, `contably_os_fronts`, etc.)
   - Env var names (`CONTABLY_*`, `ANTHROPIC_*`)
   - File paths (`/opt/contably-os/`, `.contably-os/`, `docs/contably-product-roadmap-*.md`)
   - Systemd unit names (`contably-os-v3-*.timer`)
   - The `contably-os` console script name (keep it as an alias alongside `oxi`)

3. **`pyproject.toml`.** `name="oxi-core"`, `version="0.1.0"`, same dependencies as 0.4.6 pinned byte-for-byte. Console scripts: `oxi = "oxi_core.cli:main"` **and** `contably-os = "oxi_core.cli:main"` (alias, for drop-in compat).

4. **Tests.** Copy the 0.4.6 test suite verbatim. Update imports. Suite must pass green on local + CI before the first commit lands on `main`. If any test depends on a hardcoded `contably_os` string outside imports, rewrite the assertion to accept both the old and new module name during the migration window.

5. **Diff audit.** Commit to `oxi-core/DIFF-FROM-0.4.6.md` a file-by-file diff summary. For Phase 1, the only allowed diff categories are:
   - Import-path renames (`contably_os` → `oxi_core`)
   - Package name in `pyproject.toml`
   - Console script registration
   - Test assertion updates for above

   Any other diff requires explicit Pierre approval in the PR thread.

6. **Drop-in compatibility shim.** Ship a tiny `contably_os` stub package (`contably_os/__init__.py` that `from oxi_core import *`) so legacy code that still imports `contably_os.v3.dispatch` keeps working. **Temporary, removed in Phase 4.**

**Exit:**
- PR merged to `main` with green CI.
- `pip install oxi-core` in a fresh venv succeeds.
- `oxi v3 status --db /path/to/fixture.db` returns identical output to `contably-os v3 status --db /path/to/fixture.db` against the same fixture.
- `pytest` passes (same number of tests, same assertions as 0.4.6).

**Rollback trigger:** the compat shim doesn't import cleanly, OR a renamed import breaks the v3 loop on fixtures, OR the diff audit shows any non-allowed diff.

**Rollback:** delete the PR. Production is unaffected because Phase 1 doesn't touch the VPS.

**Earliest Phase 2 start:** same day Phase 1 merges.

---

### Phase 2 — Shadow run on VPS (14 days minimum)

**Entry:** Phase 1 exit. Production still on `contably-os 0.4.6`.

**Goal:** Prove `oxi-core 0.1.0` ships PRs identically to `contably-os 0.4.6` before any cutover.

**Work:**

1. **Second venv.** On the VPS, create `/opt/oxi-shadow/venv/` alongside `/opt/psos/venv/`. Never merge them. Never symlink between them during shadow mode.

2. **Second DB.** Create `/opt/oxi-shadow/oxi-shadow.db` as a fresh SQLite file with the same schema as production's `/opt/psos/psos/psos.db`. Empty — shadow starts fresh.

3. **Shadow timer.** Add `oxi-shadow-v3.timer` (10-minute interval, staggered against production's 30s), pointing at `/opt/oxi-shadow/venv/bin/oxi`, reading the shadow DB. Units live in `/etc/systemd/system/oxi-shadow-*.service`.

4. **Read-only roadmap access.** The shadow engine reads `docs/contably-product-roadmap-2026-Q2.md` from a read-only git clone at `/opt/oxi-shadow/contably-repo-ro/` — **never opens PRs, never pushes branches, never calls Claude**. Shadow mode is a dry-run: planner produces plans to a log file, critic runs against diff fixtures, dispatch writes "would have dispatched" events to the shadow DB.

5. **Parity harness.** `scripts/parity/compare-ticks.py` reads the last 24h of events from both DBs and diffs:
   - Tasks planned (count + IDs + priority ordering)
   - Tasks that would have been dispatched (same ordering, same host selection, same skill choice)
   - Scope-gate and PR-overlap decisions (same verdict per roadmap item)
   - Cost estimates (within 5%)

   Parity must be 100% on planning + dispatch decisions, 95% on cost estimates, for 14 consecutive days.

6. **Daily parity report.** Cron emits a markdown summary to `/opt/oxi-shadow/parity-YYYY-MM-DD.md`. Any mismatch → Slack alert to Pierre with the specific roadmap item and the diverging decision.

**Exit:**
- 14 consecutive days with zero planning/dispatch divergences.
- `oxi-shadow-v3.timer` has not crashed, restarted, or missed ticks.
- Cost parity within 5% for every day.

**Rollback trigger:**
- Any crash in `oxi-shadow-v3.service` — investigate before extending.
- Any parity divergence on planning/dispatch — investigate before extending. Day counter resets.
- Shadow DB path or venv path touched by a non-shadow systemd unit — stop, audit, re-isolate.

**Rollback:** `systemctl stop oxi-shadow-v3.timer && systemctl disable oxi-shadow-v3.*`. Production `contably-os 0.4.6` is untouched.

**Earliest Phase 3 start:** day 15 of shadow mode, once parity report has 14 consecutive green days.

---

### Phase 3 — Adapter wiring (7 days)

**Entry:** Phase 2 exit.

**Goal:** Introduce the `Adapter` protocol from the 0.5.0 design *carefully*, one call site at a time. Production still runs `contably-os 0.4.6`.

**Work:**

1. **Add `oxi_core/defaults.py`.** Copy the 0.5.0 `defaults.py` content, minus the hardcoded `SKILL_WEIGHTS` and `GITHUB_REPO` that caused the Contably-flavoring leak. Those become adapter-only. **Every constant here is identical to what's inlined in `contably-os 0.4.6` today.**

2. **Add `oxi_core/adapter.py`.** Copy the 0.5.0 protocol + dataclasses verbatim. **No call site uses it yet.** This is the contract only.

3. **Add `adapters/contably/`.** A minimal `ContablyAdapter(Adapter)` class that returns the current production values:
   - `NamingConfig.instance_name = "Contably OS"`
   - `PathsConfig.db_path = "/opt/psos/psos/psos.db"`
   - `BudgetCaps` with Contably's actual numbers (daily_soft=130, daily_hard=250, per_task_opus from current prod)
   - `PromoteRecipe` matching the 5am BRT daily-promote flow
   - `policy.plan_tier = "max_20x"` — explicit, not defaulted
   - `github_repo = "Contably/contably"`

4. **Wire one call site as proof-of-concept.** Pick `v3/dashboard.py`'s dashboard title — lowest risk, user-visible so regressions are obvious. Change the literal to `get_active_adapter().naming.instance_name` with a fallback to `defaults.INSTANCE_NAME`.

5. **Test both paths.** Unit test asserts: with `ContablyAdapter` registered, dashboard title is "Contably OS"; without adapter, it's "Oxi".

6. **Shadow-run the wired path.** The Phase 2 shadow timer picks up the adapter-wired `oxi-core 0.2.0` automatically. Parity harness continues to diff against production. Any divergence → revert, investigate.

7. **Migrate call sites in batches of 3–5.** Not all at once. Each batch is a PR, runs in shadow for 3 days, then merges. Estimated batches: 8. Total call sites to migrate: ~40 (hardcoded `Contably/contably` repo refs, `/opt/psos` path refs, SKILL_WEIGHTS, etc.).

8. **CI gate.** A new CI check runs `grep -r "Contably" oxi-core/src/` and fails if any literal `Contably` remains in engine code (tests and fixtures exempt). Forces the migration to completion.

**Exit:**
- All 40 call sites read from adapter with default fallback.
- `ContablyAdapter` produces identical behavior to inlined defaults (validated by 14-day shadow parity over Phase 3).
- `grep` gate passes.

**Rollback trigger:**
- Any shadow parity divergence caused by an adapter wiring → revert that batch's PR immediately.
- Any production incident blamed on adapter changes → halt Phase 3, stay on last-green batch.

**Rollback:** revert PR. Shadow catches the regression before production ever sees it.

**Earliest Phase 4 start:** Phase 3 exit + 14 days of green shadow parity on fully-wired adapter.

---

### Phase 4 — Production cutover (1 day of cutover work, 14 days of observation)

**Entry:** Phase 3 exit. Shadow has been running `oxi-core` with `ContablyAdapter` for 28+ days total (14 during Phase 2, 14 during Phase 3) with zero unresolved parity divergences.

**Goal:** Swap production from `contably-os 0.4.6` to `oxi-core` + `ContablyAdapter`. Keep the DB. Keep the table names. Keep the systemd unit names. Only the Python package changes.

**Work:**

1. **Morning of cutover.**
   - Stop `contably-os-v3.timer` and all `contably-os-v3-*.timer` units.
   - Confirm killswitch is SET.
   - Snapshot `/opt/psos/psos/psos.db` to `/opt/psos/psos/psos.db.pre-oxi-cutover.backup`.
   - Snapshot `/opt/psos/venv/` to `/opt/psos/venv.pre-oxi-cutover.tar.gz`.

2. **Install oxi-core.**
   - `pip install oxi-core==0.3.0 oxi-adapter-contably==0.1.0` into `/opt/psos/venv/` (overwrites the 0.4.6 install, keeps the venv path).
   - Rebuild console scripts: `which contably-os` still works (alias), `which oxi` now works too.
   - `oxi v3 status --db /opt/psos/psos/psos.db` — must return identical output to pre-cutover.

3. **Gated restart.**
   - Re-enable timers one at a time: dashboard, then pr-watcher, then seed, then v3 (main loop). 5-minute gap between each.
   - After each, watch `/opt/psos/psos/psos.db` for new events with the expected shape. Any anomaly → stop, roll back.

4. **Killswitch lift.**
   - Pierre confirms engine is healthy. Remove killswitch file.
   - First tick: verify `last_progress_at` stamped correctly (this was the 0.5.0 killer bug).
   - Verify plan-tier reads `max_20x` from adapter, not hardcoded `5x`.
   - Verify one full dispatch → PR open → critic → auto-merge cycle completes.

5. **14-day observation window.** No other engine changes land during this window. Daily `gh pr list`-based health check. Any degradation vs the 7-day pre-cutover baseline → rollback.

**Exit:**
- 14 days of production runtime on oxi-core with health metrics matching or exceeding the 7-day pre-cutover baseline:
  - PRs merged per day (±20%)
  - Mean time from task-planned to PR-open
  - Mean time from PR-open to PR-merged
  - Rate of dispatched-but-never-progressed tasks (the 8s reaping bug) — target 0

**Rollback trigger:**
- Any of: `last_progress_at` not stamped, plan-tier wrong, PR count drops >30% vs baseline, any non-CI-caused task regression, any `no such table` error.

**Rollback:** `rollback/phase-4-cutover.sh`:
1. `systemctl stop contably-os-v3*.timer`
2. `mv /opt/psos/venv /opt/psos/venv.oxi-failed && tar xzf /opt/psos/venv.pre-oxi-cutover.tar.gz -C /opt/psos/`
3. `cp /opt/psos/psos/psos.db.pre-oxi-cutover.backup /opt/psos/psos/psos.db`
4. `systemctl start contably-os-v3.timer`
5. Verify dispatch resumes within 5 minutes.

**Total rollback time target: under 10 minutes.**

**Earliest Phase 5 start:** 30 days after Phase 4 exit. Not 14 — because renames (Phase 5 prep) should sit on a rock-solid oxi-core foundation.

---

### Phase 5 — Rename + wizard + fork-readiness (open-ended)

**Entry:** Phase 4 exit + 30 days green.

**Goal:** Now that `oxi-core` is running production, start the actually-visible changes.

**Work (each a separate milestone, each with its own 14-day shadow/observation):**

1. **`.oxi/` directory convention.** `.contably-os/` dir is renamed to `.oxi/` inside the Contably repo. Adapter reads the legacy path if the new one doesn't exist. 14-day compat window, then legacy path removed.

2. **Systemd unit rename.** `contably-os-v3-*.timer` → `oxi-v3-*.timer`. Paired with a generator script that produces both old and new units, and a cutover script that stops the old while starting the new. 14-day compat window.

3. **Table rename (the 0.5.0 killer).** `contably_os_task` → `oxi_task`, all sibling tables. Done via an Alembic migration that creates views under the legacy names so any external tooling keeps working. 30-day view window, then views dropped.

4. **`oxi init` wizard.** 8-step bootstrap scaffolds an adapter from `adapters/_template/`. The flow from `escotilha/psos` `docs/design/02-wizard.md` applies — copy it wholesale, since that design was the not-broken part of 0.5.0.

5. **Contribution protocol.** `escotilha/psos` `docs/design/03-contribution-protocol.md` design applies. Implement when a second fork actually exists.

6. **Census.** `04-census-and-safety.md` — implement only if contributions actually flow.

Phase 5 has no fixed timeline. Each milestone lands only when the previous is stable.

---

## 6. Cross-cutting invariants

Enforced by CI and by runbook checklists from Phase 1 onward.

| Invariant | Enforcement |
|---|---|
| No literal `Contably` in `oxi-core/src/` outside comments | CI grep check, Phase 3+ |
| DB path default matches systemd `--db` flag | Unit test reads both, asserts equality |
| Plan tier explicit in config, not defaulted | Startup logs the tier, `oxi v3 status` shows it |
| Every Phase 2+ PR includes parity harness output | PR template requires the diff |
| Every VPS-touching Phase ships with `rollback/<phase>.sh` | PR template requires it |
| No `--no-verify` pushes to main | Branch protection rule |
| Secrets only via env vars, never in committed files | gitleaks CI check |
| Tokens live in macOS Keychain locally, GHA Secrets in CI, environment-injected on VPS | Documented in `docs/secrets.md` |

## 7. What stays exactly the same (to protect production)

- Live DB path: `/opt/psos/psos/psos.db` through Phase 4.
- Venv path: `/opt/psos/venv/` through Phase 4.
- Systemd unit names: `contably-os-v3-*` through Phase 5.
- Dashboard URL: `http://100.77.51.51:8765/` through Phase 5.
- `contably-os` console script: remains as an alias forever (drop-in compat).
- `BuildFailed.yml` workflow stub in the Contably repo — don't delete, it absorbs the phantom workflow.
- `FISCAL_EMISSION_ENABLED=false` policy — oxi does not touch Contably fiscal code.

## 8. What kills this plan

The plan fails — and the project reverts to "just keep patching `contably-os 0.4.6` forever" — if any of these happen:

- Phase 2 shadow-run can't reach 14 days without parity divergence after three attempts. Means the extraction introduced a latent behavioral change. Requires root-cause before continuing.
- Phase 4 cutover triggers rollback twice. Means there's a class of bug we're not catching in shadow. Halt, post-mortem, rethink shadow harness.
- Pierre loses confidence at any phase gate. The plan explicitly routes through Pierre-approvable exit criteria; if the exit criteria are hit but the operator says no, the plan pauses and we talk before proceeding.

## 9. Decisions required before Phase 0 starts

1. **Repo privacy.** User said private — confirmed.
2. **Repo name.** User said `escotilha/oxi` — confirmed.
3. **`contably-os` console-script alias in Phase 1.** Keep (yes) or drop (no)? Recommendation: keep, for drop-in compat on the VPS. Question is whether it's permanent or Phase-5 removable.
4. **Adapter package distribution.** Published to PyPI (`oxi-adapter-contably`) or installed from a git URL (`pip install git+https://github.com/escotilha/oxi@main#subdirectory=adapters/contably`)? Recommendation: git URL until a second fork exists; adds zero publishing burden.
5. **Who owns the 14-day observation windows.** If Pierre is the only reviewer, windows need to be long enough to span weekends and vacations. Current 14-day minimum assumes Pierre is actively watching.

## 10. Immediate next action

Phase 0. If the plan is approved as written:

1. Create `escotilha/oxi` (private) on GitHub.
2. Push this `PLAN.md`.
3. Copy the relevant `docs/design/*.md` files from `escotilha/psos` into `docs/design-reference/` (marked clearly as "inherited design, not yet implemented").
4. Write `docs/post-mortems/psos-0.5.0-rollback.md` — the anti-pattern, fixed in writing. Uses the `rollback-briefing-for-new-agents-2026-04-23.pdf` as source material.
5. Open GitHub issues for Phase 1 work items. Close when merged.

Nothing on the VPS changes until Phase 2 at the earliest. Production stays on `contably-os 0.4.6` through the entire extraction process.
