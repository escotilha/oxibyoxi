# ORIGIN → oxi feature-gap report

_Generated 2026-04-24 from ORIGIN `@ 2c7c195` (the pinned production commit) vs oxi `main` at Phase-1 complete._

## Method

ORIGIN is the predecessor autonomous orchestrator studied at a distance through its source tree. The question asked: what has ORIGIN shipped that oxi has not, and which of those features should oxi adopt — with the strict constraint that no project-specific strings, paths, or env var names cross into oxi (see `docs/anti-patterns.md §Sanitization`).

Each candidate feature is classified as:

- **Already in oxi** — no action.
- **Portable, worth adopting** — mechanism is generic; must be re-implemented in oxi using adapter fields, never ported verbatim.
- **ORIGIN-specific, stay out** — either the implementation is tied to a specific project's topology, or the useful pattern is covered by another item in Section 2.

No ORIGIN source is copy-pasted in this report or in any follow-up PR. Re-implementations study the behavior, then write fresh code.

---

## 1. Already in oxi (no action)

| ORIGIN module | Notes |
|---|---|
| `auto_merge.py` | oxi has critic-gated auto-merge + terminal-state discipline (anti-pattern #7). |
| `brief.py` | Markdown daily recap generator. ORIGIN adds Slack post + rate-limit sections; core loop matches. |
| `dashboard.py` + `dashboard_html.py` | Localhost HTTP server + HTML renderer. oxi's is driven by `adapter.naming()`. |
| `dispatch.py` | State-machine driver with atomic transitions. oxi version has `EngineState`-based killswitch discipline. |
| `dispatch_pool.py` | Multi-host pool with in-memory concurrency bookkeeping. |
| `heartbeat.py` | Reaper with orphan-with-PR guard + `last_progress_at` discipline. |
| `kill.py` | File-based killswitch bridged into `EngineState`. |
| `pr_watcher.py` | Branch-to-task reconciler with `GitHubClient` protocol. |
| `worktree_provision.py` | Provision + reap + validate; branch-collision detection. |
| `critic.py` | Adapter-backed `CriticBackend` protocol; `ClaudeCriticBackend` wired behind `--real-claude`. |
| `db.py` | Schema + migrations + `cost_usd` + `files_touched` + `pr_number`. |
| `planner.py` | Roadmap parser + task seeder. |
| `prompts.py` | Templated planner/dispatch/critic prompts. |

## 2. Portable, worth adopting

### Cost enforcement (Phase 2, ~1 day)

**ORIGIN:** `v3/cost.py` — soft-warn + hard-stop daily caps, per-task cap by model tier, ingests token counts from dispatch `stream-json` logs to back-fill `cost_usd` without a second API call.

**Status in oxi:** oxi records `cost_usd` on every transition but never enforces a cap.

**Why port it:** A runaway dispatch loop is the fastest path to a surprise invoice. Hard-cap enforcement is the single biggest safety gap before Phase 2 dogfooding.

**Adaptation:** Caps flow from `adapter.budget()` fields (already in the protocol). Log-dir comes from `adapter.paths()`. No project-specific strings needed.

---

### `seed_from_roadmap` + `ingest_roadmap` — auto-replenishment (Phase 2, ~2 days)

**ORIGIN:** `v3/ingest_roadmap.py` parses the roadmap markdown into a `fronts` table; `v3/seed_from_roadmap.py` auto-replenishes the planned-task queue from `fronts` when planned count drops below threshold, respecting tier priority and a 24h cooldown after merge.

**Status in oxi:** oxi's `planner.py` creates tasks directly from roadmap items but lacks the intermediate `fronts` layer. The current loop idles once the initially seeded tasks drain.

**Why port it:** Without this, Phase 2 observation windows are interrupted every few days by manual operator queue refilling. Auto-replenishment is what makes the loop self-sustaining.

**Adaptation:** Tier selection and cooldown become adapter-configurable. No project strings.

---

### `ship_recovery` (Phase 2, ~2 days)

**ORIGIN:** `v3/ship_recovery.py` — detects dispatched tasks whose Claude session exited without committing, runs `git add -A && git commit && git push && gh pr create` against the worktree, recovering the completed work.

**Status in oxi:** Not present. If a session compacts or crashes mid-task, the work is silently abandoned.

**Why port it:** This is the single most common failure mode. During Phase 2 dogfooding on oxi's own repo, it will surface early.

**Adaptation:** SSH target comes from `DispatchHost.ssh_alias` (already in `adapter` from P1-5c). The recovery sequence itself is generic.

---

### `pr_overlap` — file-level conflict gate (Phase 2, ~1 day)

**ORIGIN:** `v3/pr_overlap.py` fetches the file list of all open PRs and blocks dispatch if any file overlaps with the planned task's `files_touched`.

**Status in oxi:** oxi stores `files_touched` in the DB schema but only checks overlap against in-flight tasks, not against open PRs whose sessions already exited.

**Why port it:** Catches semantic conflicts between a planned task and a just-opened PR that heartbeat has reaped from `dispatched`.

**Adaptation:** Repo from `adapter.github_repo()`. TTL-cached tmpfile path from `adapter.paths()`.

---

### `branch_janitor` (Phase 2, ~0.5 day)

**ORIGIN:** `v3/branch_janitor.py` — deletes merged-and-closed remote branches matching the autonomous-branch pattern, skipping any with an open PR or unmerged commits.

**Status in oxi:** Not present. After N merges the repo accumulates N stale remote refs.

**Why port it:** Low-stakes cleanup, but matters for the usability of the target repo (`git fetch` time, `git branch -r` noise).

**Adaptation:** Branch patterns come from `adapter.branch_prefixes()` (already present).

---

### Skills loader (Phase 2, ~0.5 day)

**ORIGIN:** `skills.py` — parses `SKILL.md` frontmatter; loads `name + description` cheaply on startup, body only when triggered.

**Status in oxi:** oxi has a `skills/` directory stub but no loader; the dispatch skill-picker hardcodes skill names in a `SKILL_WEIGHTS` dict.

**Why port it:** Makes the available-skill set adapter-configurable without editing core.

**Adaptation:** Skills directory comes from `adapter.paths()`. Fully self-contained.

---

### `deadman` — silent-failure detector (Phase 3, ~1 day)

**ORIGIN:** `v3/deadman.py` — runs on a timer, detects prolonged heartbeat silence, stale dashboard, and rate-limit ceiling; fires escalating alerts.

**Status in oxi:** Not present. The orchestrator can stop working silently — operator only discovers this when they check manually.

**Why port it:** Essential for unattended operation (which is Phase 2 dogfooding's promise).

**Adaptation:** Requires a generic `NotificationBackend` protocol first (mirroring `CriticBackend` and `GitHubClient`). The three detection signals are generic.

---

### `handoff` — context-compaction recovery (Phase 3, ~2 days)

**ORIGIN:** `v3/handoff.py` — writes a rolling-3 `resume-<n>.md` snapshot before each dispatch, with dedup and a `handoff_consumed` ledger event.

**Status in oxi:** Not present. Compaction mid-task currently loses all context.

**Why port it:** Context compaction is the second most common failure class after uncommitted-crash.

**Adaptation:** Paths from the worktree handle; oxi_dir from `adapter.paths()`.

---

### `tail_dispatch` — live-tail CLI (Phase 3, ~1 day)

**ORIGIN:** `v3/tail_dispatch.py` — `iter_events`, `format_event`, `tail_log` over `stream-json` dispatch logs.

**Status in oxi:** Not present. Debugging a stalled dispatch requires manually `tail -f`-ing the log.

**Why port it:** Significant operator-experience improvement.

**Adaptation:** Log directory from `adapter.paths()`. Event format is Claude CLI's public `stream-json` shape.

---

### `ci_issue_filer` — CI failure surfacing (Phase 3, ~1 day)

**ORIGIN:** `v3/ci_issue_filer.py` — idempotently opens a GitHub issue per autonomous PR stuck with red CI beyond a grace period.

**Status in oxi:** Not present. CI failure on an autonomous PR produces no human-visible signal.

**Why port it:** Turns silent-CI-fail into an inbox notification.

**Adaptation:** Label (`autonomous-ci-failure`) is a sensible generic default. Repo from adapter.

---

### `oauth_watch` (Phase 3, ~1 day)

**ORIGIN:** `v3/oauth_watch.py` — reads the Claude OAuth token expiry from the dispatch host via SSH, emits escalating ledger events.

**Status in oxi:** Not present.

**Why port it:** Expired OAuth silently kills every dispatch.

**Adaptation:** SSH target from `DispatchHost.ssh_alias`. Credential path is the Claude CLI's standard `~/.claude/.credentials.json`. Alert emission needs the generic notification backend.

---

### `cto_verdict` — structured verdict parser (Phase 3, ~0.5 day)

**ORIGIN:** `v3/cto_verdict.py` — post-processes a `/cto` report into `{"verdict": "proceed"|"abort", "severity_max": ..., "concerns": [...]}` JSON; fail-closed on ambiguity.

**Status in oxi:** Not present.

**Why port it:** Machine-readable gate before dispatch, prevents "ship anyway because the report text was ambiguous."

**Adaptation:** Self-contained.

---

### `daily_promote` — adapter-driven promote engine (Phase 3, ~2 days)

**ORIGIN:** `v3/daily_promote.py` — timed window that halts dispatch, verifies staging, probes production health, triggers the staging→production workflow.

**Status in oxi:** oxi has `PromoteRecipe` in the adapter protocol but no engine that executes it.

**Why port it:** Without it the loop ships to staging only; final delivery requires human intervention every cycle.

**Adaptation:** The engine is generic; every concrete value (workflow names, endpoints, time-window) comes from `adapter.promote_recipe()`.

---

### Episodic + semantic memory (`hooks.py` + `consolidator.py`) — Phase later

**ORIGIN:** `hooks.py` appends an episodic memory entry on `SessionEnd` and syncs it centrally; `consolidator.py` uses Haiku to propose patches to a semantic memory index, with a post-generation validator rejecting hallucinated citations.

**Status in oxi:** Not present.

**Why port it:** Without episodic memory, every dispatched session starts with no knowledge of what prior sessions learned or broke.

**Adaptation:** Needs redesign. The sync destination, path conventions, and Anthropic client factory all need to go through the adapter. The rsync-to-central-host model assumes a specific topology — oxi should use `adapter.paths()` and a new `MemoryBackend` protocol.

---

## 3. ORIGIN-specific — stay out

Everything in this list uses a project-specific string, path, or env var that is forbidden in oxi by `scripts/lint-for-leaks.sh`. The *mechanism* behind each item is covered by a Section 2 entry if there was something useful to extract.

- `brief_issue.py` — title/label strings name the project explicitly.
- `hooks.py` — hardcoded env vars and SSH targets for the project's VPS.
- `oauth_watch.py` — env var names reference the project.
- `registry.py` — loads a project-named YAML directory (ORIGIN's session/pattern registry). oxi's adapter model replaces this.
- `client_factory.py` — env var names tie it to ORIGIN's VPS topology. oxi's adapter already owns this surface via `DispatchHost`.
- `ledger_events.py` — writes to a project-named table; schema lives in a project-named directory. Portable validation-registry pattern is absorbed into oxi's event model.
- `council.py` — invokes a project-specific deliberation skill by name.
- `daily_promote.py` — all concrete values (workflow names, BRT timezone offset, endpoints) are project-specific. Only the skeleton is portable.
- `dispatch_pool.py` — reads project-specific env vars and Tailscale hostnames. oxi's pool is already cleaner.
- `seed_from_roadmap.py` / `ingest_roadmap.py` — reference project-specific file patterns and table names. Logic is portable, names are not.

## 4. Recommendations — top 3 for Phase 2

**1. Cost enforcement (~1 day).** Single biggest safety gap. A runaway loop is the fastest path to a surprise invoice. Must land before real Claude dispatch goes unattended.

**2. `seed_from_roadmap` + `ingest_roadmap` (~2 days).** Without auto-replenishment, Phase 2 observation windows get interrupted every few days by manual operator refilling. Port both together — `seed_from_roadmap` assumes the `fronts` table `ingest_roadmap` populates.

**3. `ship_recovery` (~2 days).** The "Claude wrote code then exited before committing" failure class is common enough that ORIGIN built this before any ergonomic features. Essential before the 14-day observation window starts — otherwise the counter resets on every compaction-mid-task.

Together these three take ~5 days and close the three largest gaps between "Phase 1 demo loop" and "oxi can actually dogfood itself unattended."

---

## Appendix — what generated this

This report was produced by following the procedure in `skills/origin-review/SKILL.md`. Future reports on ORIGIN updates should use that skill so the process stays repeatable and the forbidden-string discipline stays enforced.
