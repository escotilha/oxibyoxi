# Scheduling the Multi-Model and Auto-Improve subsystems against the live pipeline

**Date:** 2026-04-25
**Author:** scheduling pass after operator decisions on Q1-Q4 (multi-model) and Q1-Q8 (auto-improve)
**Status:** scheduling proposal — not yet a roadmap append. Once approved, the T2-30..T2-40 and T1-B1..T1-B11 entries land per the order below.

---

## 1. What's actually in flight

Read live from `origin/main` and `gh pr list` at 2026-04-25 13:50Z:

### Tier 0 (blocker / safety) — in flight

| Item | PR | Status |
|---|---|---|
| T0-201 / 202 / 203 — behavioral contract foundation (types, ledger events, registry) | #118 | open, mergeable unknown, **roadmap not seeded yet** (PR adds 3 lines to `docs/roadmap.md`). |

### Tier 1 (visual polish) — 7 PRs queued, all auto-merge

| Item | PR | Status |
|---|---|---|
| T1-19 — Rich-render `oxi status` and `oxi v3 tick` | #110 | open |
| T1-20 — dashboard hero row + pressure-gradient budget bar | #111 | open, mergeable |
| T1-21 — status glyph standardization | #112 | open |
| T1-22 — README screenshot slot + before/after rewrite | #113 | open |
| T1-23 — wizard step counter + review-and-confirm | #114 | open |
| T1-24 — accent color constant + dashboard application | #115 | open |
| T1-25 — ASCII logo for `--version` + dashboard + README | #116 | open |

### Tier 2 (quality gates) — 5 PRs queued, all auto-merge

| Item | PR | Status |
|---|---|---|
| T2-12 — mypy strict | #95 + #99 | auto-merge queued, awaiting CI |
| T2-13 — coverage gate 85% | #98 | auto-merge queued, awaiting CI |
| T2-14 — nightly live-GitHub integration test | #105 | open |
| T2-15 — benchmark regression guard | #104 | open |
| T2-16 — doc lint (lychee + markdownlint) | #103 | open |

### Plans not yet ingested (this round)

| Plan | PR | Status |
|---|---|---|
| Multi-model orchestration plan + research | #127 | open, awaiting review. **No roadmap entries seeded yet** — entries are T2-30..T2-40, ~11 PRs ~580 LOC. |
| Auto-improve subsystem plan + research | #128 | open, awaiting review. **No roadmap entries seeded yet** — entries are T1-B1..T1-B11, ~11 PRs ~700 LOC. |

### Adjacent merged this morning

- #106 — `auto_merge=True` flipped for `oxi-adapter-self`. **The "Pierre reviews every PR" assumption in both plans is now out of date.** Both plans need a stale-context update before T2-30 / T1-B1 ship.

### Pending external (Dependabot, ignore for sequencing)

#121-#125 are GitHub Actions dependency bumps. They merge or close on their own.

---

## 2. Dependency graph between everything in flight + everything proposed

```
                    ┌─────────────────────────────────────────────────┐
                    │ T2-12 mypy + T2-13 coverage + T2-14 live-GH +   │
                    │ T2-15 bench + T2-16 doc-lint  (#95,98,99,103,   │
                    │ 104,105) — quality gates, all merging now       │
                    └────────────────────┬────────────────────────────┘
                                         │ when these land:
                                         │  - mypy strict allow-list expands
                                         │  - coverage 85% gate enforced
                                         │  - doc-lint catches markdown errors in plans
                                         ▼
                    ┌─────────────────────────────────────────────────┐
                    │ T0-201, T0-202, T0-203 — behavioral contracts   │
                    │ foundation (#118)                               │
                    │  - depends on quality gates being green so the  │
                    │    new types pass mypy and the new constants    │
                    │    pass coverage                                │
                    └────────────────────┬────────────────────────────┘
                                         │ when this lands:
                                         │  - WORKER_CONTRACTS registry exists
                                         │  - contract-violation ledger events exist
                                         │  - critic + dispatch can wire into them
                                         │    (in T1-201/202/203 follow-up)
                                         ▼
              ┌──────────────────────────┴────────────────────────────┐
              │                                                       │
              ▼                                                       ▼
┌──────────────────────────────┐                   ┌──────────────────────────────┐
│ MULTI-MODEL plan (#127)      │                   │ AUTO-IMPROVE plan (#128)     │
│ T2-30 → T2-40 (11 PRs)       │                   │ T1-B1 → T1-B11 (11 PRs)      │
│ ~580 LOC                     │                   │ ~700 LOC                     │
│                              │                   │                              │
│ - T2-30 only depends on the  │                   │ - B1-B7,B9,B10 don't touch   │
│   AgenticAdapter Protocol    │                   │   `front` ingestion.          │
│   and `dispatch_invoke.py`,  │                   │ - B8 (emit) writes to `front` │
│   which is stable.           │                   │   → rebases if #118 changes   │
│ - T2-31..T2-35 add Codex     │                   │   the schema (Q8 resolution)  │
│   adapter — independent of   │                   │ - Loop never imports          │
│   plan #128.                 │                   │   `dispatch` or `auto_merge`  │
│ - T2-36..T2-38 wire Mac Mini │                   │   (lint-enforced from B1)     │
│   gateway — independent of   │                   │                              │
│   plan #128.                 │                   │                              │
│ - T2-39..T2-40 promote one   │                   │                              │
│   task class to Codex —      │                   │                              │
│   independent of plan #128.  │                   │                              │
└──────────────────────────────┘                   └──────────────────────────────┘
```

**Key observations:**

1. The two new subsystems are **structurally independent** of each other. Multi-model adds backends under `dispatch.py`; auto-improve adds a sibling to `auto_observe.py`. They share no files, no ledger events (different prefixes: `agentic_*`, `inference_*` vs `external_*`, `auto_improve_*`), no test fixtures.

2. **Both depend on #118** (behavioral contracts foundation) only loosely. Multi-model: contracts are keyed by role on the *normalized* DispatchResult, so the AgenticAdapter migration doesn't have to wait for #118 to land — they can ship in parallel and contracts attach later. Auto-improve: doesn't touch contracts at all; only B8 might rebase if #118 adjusts `front`'s ingestion shape.

3. The visual polish track (T1-19..T1-25, 7 PRs) is on a parallel rail with **zero overlap**. Whatever order the engine picks for those, they don't block T2-30 or T1-B1.

4. `auto_merge=True` (just flipped, #106 merged) means **both plans' "Pierre reviews each PR" framing is stale**. Both plans need a one-line update saying the operator reviews via the daily brief + `gh pr list` rather than per-PR; the merge gate is now CI + critic verdict.

---

## 3. Priority ranking (operator question: "should these be priorities, or end of pipeline?")

My read: **neither plan is a priority over what's currently in flight, but they ARE a priority over starting any new feature work after #118 lands.** Reasoning:

| Subsystem | Should it jump the queue? | Why |
|---|---|---|
| **Multi-model orchestration** | No, but ship right after #118 | The Codex adapter is a **risk-reduction** play (second-source diversity if Anthropic rate-limit-exhausts), but oxi has no current outage on Anthropic. The Mac Mini gateway integration (T2-36..T2-38) is an *enabler* for future inference-only paths, but no current path needs it. Ships next, not first. |
| **Auto-improve subsystem** | No, but ship right after multi-model | The loop adds *signal* — surfaces external roadmap candidates the operator wouldn't have seen — but the quality of the signal depends on having real roadmap-item history to embed against. The current roadmap has only ~5 open items; after T2-12..T2-16 + T0-201..T0-203 + multi-model land, the corpus is rich enough for the BM25/vector/judge stack to produce decent matches. Ships after multi-model so the corpus is bigger. |
| **Multi-model Phase 5 (local agentic models)** | Out of scope for this round | Defer per the plan; depends on internal eval against historical roadmap items, which doesn't exist yet. |
| **Auto-improve B11 (GHA fallback)** | Skip preemptively (per Q7) | Only ships if Routines GA slips. Contingent. |

**Summary ranking:** quality gates (in flight) → contracts (#118, in flight) → multi-model → auto-improve.

---

## 4. Proposed scheduling

### Wave 0 — Don't touch (in flight, will merge themselves)

T2-12, T2-13, T2-14, T2-15, T2-16, T1-19..T1-25, T0-201..T0-203 (#118), Dependabot bumps. Auto-merge handles all of these. Engine continues working through them.

**Operator action:** none. Watch the dashboard.

**Expected timeline:** 1-3 days for T2-12/T2-13 (CI gates), longer for the visual polish series (each is a tight UI PR).

### Wave 1 — Multi-model phase 1 (T2-30 only)

**Trigger:** wait for T0-201..T0-203 to land (PR #118). T2-30 doesn't depend on them mechanically, but the contract-attached-via-role design baked into the multi-model plan assumes the contracts module exists.

**Action:** single PR appending **only T2-30** to `docs/roadmap.md`. Don't append T2-31..T2-40 yet — let T2-30 prove out the AgenticAdapter Protocol against real dispatch traffic first.

**Why only T2-30:** it's the smallest possible Phase 1 (~80 LOC, zero call-site changes). If the Protocol turns out to need a redesign, only T2-30 has to be reverted. T2-31..T2-40 would have inherited a flawed Protocol and snowballed.

**Expected timeline:** 1 day to ship, 3-5 days to observe in production before opening Wave 2.

### Wave 2 — Multi-model phase 2 (T2-31..T2-35, Codex adapter)

**Trigger:** T2-30 has been on `main` for ≥ 3 days with no `agentic_contract_violation` ledger events.

**Action:** single PR appending T2-31..T2-35 to `docs/roadmap.md`. The engine ships them sequentially. T2-35 is the shadow-run harness — it gates production traffic from going to Codex until the operator manually opts in via env var.

**Operator decision during Wave 2:** after T2-35 ships, set `OXI_AGENTIC_SHADOW=codex` for 14 days. Watch the dashboard's shadow-comparison panel. If the codex adapter's normalized DispatchResults match the Claude adapter's at ≥ 85% structural parity, proceed to Wave 3. Else: hold, debug.

**Expected timeline:** 5-8 days to ship the 5 PRs, plus the 14-day shadow window.

### Wave 3 — Multi-model phase 3+4 (T2-36..T2-40, gateway + routing)

**Trigger:** Wave 2's 14-day shadow window completes with ≥ 85% parity.

**Action:** single PR appending T2-36..T2-40 to `docs/roadmap.md`.

**Operator action:** before T2-36 lands, provision a virtual key on the Mac Mini LiteLLM gateway for `oxi-heartbeat` role. Document in the runbook PR.

**Expected timeline:** 5-7 days for the 5 PRs, plus the Wave 3 promotion gate (T2-40 promotes the doc-tier-2 task class to Codex; needs 14-day post-promotion observation per the plan's status section).

### Wave 4 — Auto-improve foundation (T1-B1..T1-B7)

**Trigger:** Wave 3 dispatch path is stable; ledger has at least 30 days of dispatch events to embed against; #118 contracts have caught at least one real violation (proves the `front` schema is settled).

**Action:** single PR appending **T1-B1..T1-B7** (skeleton, sources, ranking pipeline up through judge — but not emit yet). The engine ships them sequentially.

**Why B1..B7 in one wave, not split:** the loop produces no output until B8 (emit) lands, so there's no risk of partial-loop weirdness while B1..B7 sequentially build up. The whole upper half can be merged and the loop is still effectively dark.

**Expected timeline:** 7-10 days for the 7 PRs.

### Wave 5 — Auto-improve goes live (T1-B8..T1-B10)

**Trigger:** Wave 4 complete; B7 (LLM judge) has been benchmark-tested against 20 historical roadmap items with ≥ 0.7 Haiku-Pierre rubric agreement (per the plan's calibration step).

**Action:** single PR appending T1-B8..T1-B10. The B10 (Routine config) PR is the one that makes the loop actually run at 5am.

**Operator action:** before B10 ships, configure the Anthropic Claude Code Routine via the Vault credentials path. Subscribe to the daily digest.

**Expected timeline:** 4-6 days. After B10 lands, the loop runs daily.

### Wave 6 — Auto-improve health monitoring (T1-B11 conditional)

**Trigger:** Routine GA slips OR the Routine fails 3 days in a row OR the operator wants belt-and-suspenders.

**Action:** single PR appending T1-B11 (GHA schedule + watchdog). Ships only if a trigger fires.

**Default outcome:** doesn't ship. Stays in the plan as a contingency.

---

## 5. Roadmap-append plan (the actual PR sequence)

| Wave | PR title | Roadmap entries added | Triggers |
|---|---|---|---|
| 1 | `feat(roadmap): seed T2-30 — multi-model phase 1` | T2-30 | After #118 merges |
| 2 | `feat(roadmap): seed T2-31..T2-35 — Codex adapter` | T2-31..T2-35 | T2-30 stable on main 3+ days, no contract violations |
| 3 | `feat(roadmap): seed T2-36..T2-40 — inference gateway + routing` | T2-36..T2-40 | Wave 2 14-day shadow ≥ 85% parity |
| 4 | `feat(roadmap): seed T1-B1..T1-B7 — auto-improve foundation` | T1-B1..T1-B7 | Wave 3 promotion gate cleared, 30 days of dispatch history available |
| 5 | `feat(roadmap): seed T1-B8..T1-B10 — auto-improve goes live` | T1-B8..T1-B10 | Wave 4 complete, Haiku-Pierre rubric agreement ≥ 0.7 |
| 6 | (conditional) `feat(roadmap): seed T1-B11 — GHA fallback` | T1-B11 | Routines GA slip or 3-day failure |

**No omnibus PR.** Each wave is its own roadmap-append PR. The engine ships each entry inside a wave sequentially. The operator can pause between waves by simply not opening the next roadmap-append PR.

---

## 6. Stale-context updates required before any wave ships

Both plans (#127 and #128) reference `auto_merge=False` as the operator-review gate. After #106 flipped this to True, those references are wrong. Two options:

**Option A (recommended):** ship a one-paragraph update PR against #127 and #128 noting the auto-merge state change and how it affects review surface. Does not delay Wave 1 — the multi-model plan's core architecture is unaffected.

**Option B:** wait until just before Wave 1 ships, update both plan docs in the same PR that seeds T2-30. Slightly cheaper but couples the plan-update commit to a roadmap-seed commit (anti-pattern #1: refactor mixed with feature).

**Recommendation: Option A.** Both plan PRs (#127, #128) are still in review; updating their text is cheaper than updating it later in a different PR.

---

## 7. Anti-patterns to avoid in this scheduling

- **Don't append T2-30..T2-40 in one PR.** That's 11 entries in one shot, which the engine would interleave with the visual polish + quality gate work and produce a confusing brief. Split into Waves 1-3 per the table above.
- **Don't append T1-B1..T1-B11 in one PR.** Same reason.
- **Don't ship Wave 4 before the auto-improve corpus has data.** The BM25 + vector + judge stack scores against historical roadmap items. With only ~5 open items in the current roadmap, every external signal would look "novel" and the dedup/relevance scoring would be useless. Wave 3 has to land first to grow the corpus past ~30 items.
- **Don't promote codex (T2-40) before the 14-day shadow window completes.** Even if the parity numbers look good early, give the dispatch path time to surface edge cases.
- **Don't skip Wave 6's trigger check.** GHA fallback adds operational complexity (two scans/day collision risk per Q7); only ship it if a trigger fires.

---

## 8. Risk register (scheduling-specific)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Quality gates (T2-12, T2-13) keep failing CI and never merge, blocking #118 indirectly | Medium | High — entire schedule slips | If T2-12 fails 3 days running, demote the affected modules from the mypy allow-list and ship a smaller mypy gate. T2-13 likewise — if 85% can't be hit, ship at the current measured floor + 5%. |
| #118 lands but the contracts API is materially different from what plan #127 assumes | Low | Medium — plan #127 needs revision | Plan #127 already accounts for this: contracts on normalized DispatchResult are model-agnostic. If #118 keys differently than expected, only Wave 1's plan-update step changes. |
| Visual polish PRs (T1-19..T1-25) hit a string of CI failures and consume engine cycles | Medium | Low — Wave 1 still ships in parallel | Engine has `max_concurrent=1` so at worst there's a queue; T2-30 doesn't compete with these for files. |
| Mac Mini gateway (Q3 resolution) goes down during Wave 3 | Low | Medium — heartbeat triage disabled | Plan #127's risk register already covers this: the InferenceGateway client emits `inference_gateway_unreachable` and falls back to today's no-LLM behavior. |
| Auto-improve corpus is too small at Wave 4 trigger and the loop emits noise | Medium | Low | Wave 4 trigger explicitly waits for ≥ 30 days of dispatch history. If the corpus is still thin, delay Wave 4 by another 14 days. |
| Operator (Pierre) wants to ship one of these subsystems sooner | Low | Low | Re-rank by re-opening this doc and re-evaluating the dependency graph. Nothing about the schedule is load-bearing on existing PRs — it's a recommendation, not a constraint. |

---

## 9. The TL;DR

1. **Don't append anything new this week.** Let T2-12, T2-13, T2-14, T2-15, T2-16, T1-19..T1-25, T0-201..T0-203 land first.
2. **Update plans #127 and #128** with a one-paragraph note that auto-merge is now True.
3. **Wave 1 — T2-30 only** as the next roadmap-append PR, after #118 merges.
4. **Each subsequent wave** ships as its own roadmap-append PR, gated on the previous wave's observation window.
5. **No omnibus appends.** Each wave is one PR with one wave's worth of entries.

Multi-model ships before auto-improve because (a) it's risk-reduction infrastructure, (b) it grows the dispatch-history corpus that auto-improve's ranking pipeline needs to be useful. Auto-improve ships last because it's a *signal generator*, not a *blocker resolver* — its value compounds with corpus size.

---

## 10. Status

- [ ] Plans #127 and #128 reviewed and merged.
- [ ] Stale-context update for #127 (auto_merge=True noted in §3).
- [ ] Stale-context update for #128 (auto_merge=True noted in §6 — "operator reviews via daily brief + gh pr list, not per-PR").
- [ ] Wave 0 cleanup verified (T2-12..T2-16 + T1-19..T1-25 + #118 all merged).
- [ ] Wave 1 ships (T2-30).
- [ ] Wave 2 ships after T2-30 stable 3+ days (T2-31..T2-35).
- [ ] Wave 3 ships after Wave 2 14-day shadow ≥ 85% parity (T2-36..T2-40).
- [ ] Wave 4 ships after Wave 3 promotion gate cleared (T1-B1..T1-B7).
- [ ] Wave 5 ships after Wave 4 + Haiku-Pierre calibration (T1-B8..T1-B10).
- [ ] Wave 6 ships only if trigger fires (T1-B11).
