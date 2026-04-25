# Deep Research Report: Daily Auto-Improve Subsystem for oxi

**Date:** 2026-04-25
**Confidence:** 78% on architecture, 65% on cost projections, 90% on safety boundary
**Research Depth:** 7 tracks, ~30 sources, oxi codebase grounded against `~/code/oxi` HEAD (release 0.1.0b1, 2026-04-24)

---

## 1. Executive Summary

oxi already has half the auto-improve loop. `oxi_core.v3.auto_observe` watches the **internal** ledger and emits `observe_proposal` events when failure clusters cross thresholds; an operator accepts them and the proposal lands in `front` for normal seed-→plan-→dispatch processing. The work proposed here is the **external-signal** symmetric counterpart: read GitHub + X every morning, distill ranked signals into the same `observe_proposal` event shape, gate them through the same accept flow, and let the existing pipeline do the rest. This means **no new state machine, no new safety surface** — the auto-improve loop is a single new module (`oxi_core.v3.auto_external`) that reads outside the repo and writes into the proposal table that already exists.

Recommended trigger: **Anthropic Claude Code Routines** (research preview, daily-5 cap on Pro / 25 on Max). Runner-up: **GitHub Actions schedule** with workflow_dispatch fallback.

Recommended ranking pipeline: **prefilter (GitHub topic + star-velocity, X list-restricted) → BM25 against oxi roadmap + CHANGELOG → embed-rerank with `sqlite-vec` against the same corpus → Haiku LLM judge with rubric (1-10) → top-N proposals**. The LLM-judge step is the only paid stage; everything before it is local SQLite.

The loop is forbidden from: merging PRs, creating branches outside `feat/auto-`, modifying `auto_merge` policy, calling `claude` for code generation (only for ranking-judge), or bypassing budget. Every proposal becomes a roadmap entry which becomes a planned task which goes through the **same critic** as human-authored items. There is no shortcut.

Daily cost projection: **$0.30–$0.80/day** for the ranking phase (Haiku judge over ~150 candidates), $0–$2/day if the loop's own draft-PR generation uses Sonnet. Hard ceiling enforced by the existing `daily_hard_cap=$20` — the loop *cannot* spend more without operator intervention regardless of bugs.

---

## 2. Key Findings (Track-by-Track)

### Track A — Daily-Trigger Infrastructure

**Finding A1: Anthropic Claude Code Routines exists, is in research preview, and is the right answer.**
- Evidence Tier: 1 (verified via memory `tech-insight:claude-code-routines`, sourced from @claudeai 2026-04-14 announcement and @noahzweben 2026-04-16 PM tweet)
- Confidence: 95%
- Specifics: Routines run on Anthropic's cloud, package prompt + repo + connectors (MCP), trigger by schedule/API/GitHub webhook. Daily run caps: 5 on Pro, 25 on Max, more on Team/Enterprise. Pierre is on Max plan tier `20x` per `SelfAdapter.plan_tier()` — fits comfortably under the 25/day cap.
- Limitation noted: Still research preview as of 2026-04-25, GA pending. Webhook support currently GitHub-only.
- Why this beats alternatives: No VPS, no secrets management, MCP connectors handle GitHub auth via Vault credentials (per the 2026-04-22 MCP/CIMD blog rule), runs even when Pierre's machine is off.

**Finding A2: GitHub Actions cron is unreliable in measurable ways.**
- Evidence Tier: 1 (multiple GitHub community discussions Q1 2026 + January 2026 platform-wide incident)
- Confidence: 90%
- Specifics: GitHub explicitly documents that schedules can be delayed or dropped under load. January 23-24, 2026 platform incident silently broke schedules and required manual workflow re-edit + workflow_dispatch to recover. Pattern repeats: skipped runs without alerts, 15-min to 1-hour scheduling-recognition delays after edits.
- Mitigation if used: GitHub schedule + external watchdog (cron on Pierre's Mac Mini that pings GitHub API once an hour to verify the daily run actually happened, alerts via `notification.py` if it didn't).

**Finding A3: Temporal / Airflow / Prefect are over-engineered for one daily research job.**
- Evidence Tier: 2; Confidence: 85%
- Specifics: Temporal is purpose-built for stateful long-running workflows with exactly-once execution; Airflow for batch pipelines with DAGs; Prefect for hybrid orchestration. All require infrastructure investment that exceeds the value for a one-off daily 5am job. Skip.

### Track B — GitHub Discovery Sources

**Finding B1: GitHub topic + star-velocity is a real signal, but the noise floor is dominated by fake stars.**
- Evidence Tier: 1 (peer-reviewed CMU study)
- Confidence: 88%
- Specifics: ~6 million suspected fake stars across 18,617 repos, ~301,000 implicated accounts. AI/LLM repos are the largest non-malicious recipient category (~177,000 fake stars). Implication for ranking: stars alone are useless; **star-velocity over a sustained 14-day window** + **commit activity** is the minimum credibility filter.
- Practical query shape: `topic:agent OR topic:cli stars:>100 pushed:>2026-04-18` then filter by `commits/last-30-days > 5`.

**Finding B2: GitHub API rate limits comfortably accommodate a daily run.**
- Evidence Tier: 1; Confidence: 99%
- 5,000 req/hour authenticated for REST core; search API has separate, more restrictive limits — 30 req/minute authenticated. One daily run consuming ~50 search calls + ~200 detail calls finishes in under 5 minutes well below limits.

**Finding B3: Awesome lists + Octoverse trending dashboards are higher-signal than raw `/trending`.**
- Evidence Tier: 2; Confidence: 75%
- Recommended GitHub source set for oxi:
  1. Releases of pinned orgs: `anthropics/*`, `openai/*`, `microsoft/autogen`, `langchain-ai/langchain`, `block/goose`, `sst/opencode`, `mastra-ai/mastra`, `crewAIInc/crewAI`. (GitHub releases atom feed, no API quota.)
  2. New repos via topic queries with `stars:>50 created:>30days`.
  3. Star-velocity signals via star-history.com public API.
  4. Curated lists' diff: pull `awesome-ai-agents` HEAD vs HEAD~7-days.
  5. NOT GitHub Advisory Database — too noisy; security findings should land in the existing Bandit + CodeQL track.

### Track C — X / Twitter Discovery Sources

**Finding C1: X API is hostile to new use; aggregator-via-newsletter is the cheapest 2026 path.**
- Evidence Tier: 1; Confidence: 92%
- As of February 6, 2026, X switched to pay-per-use ($0.01/post created, $0.005/post read) and **eliminated free tier and Basic tier signups for new developers**. Old Basic at $100/month still works for existing subscribers.
- Implication: Direct X API monitoring of a 30-account list at ~50 posts/day each = ~$7.50/day in read fees. Not viable at oxi's $20/day budget.

**Finding C2: AI newsletters already do this synthesis — let them do the curation, scrape their public archives.**
- Evidence Tier: 2; Confidence: 78%
- AlphaSignal (250K+ developer subscribers) does daily 5-min summary, combines AI algorithms with expert curation. Latent Space, BensBites, TLDR AI similar. Scrape the public archive at 5am (one HTML fetch per source = ~5 fetches/day, ~$0 cost) — gets you the same synthesized signal without paying X.
- Risk: Newsletter latency is 12-24h; you're a day behind X-native curators. Acceptable for a daily loop targeting roadmap items, not real-time alerting.

**Finding C3: Direct X scraping (FixTweet/fxtwitter/nitter) is low-cost-fragile.**
- Evidence Tier: 3; Confidence: 60%
- FixTweet works for *fetching* known tweet URLs but does not solve *discovery*. Nitter instances frequently rate-limited or shut down.
- Recommendation: Tier-1 = newsletters. Tier-2 = if Pierre has X API Basic tier, monitor a curated list of ~15 accounts at ~$1.50/day. Tier-3 = FixTweet for fetching specific URLs the LLM judge wants to inspect.

### Track D — Source Ranking & Synthesis

**Finding D1: Three-stage ranking (BM25 → vector → LLM-judge) outperforms any single stage.**
- Evidence Tier: 1; Confidence: 88%
- Pipeline: BM25 retrieves top-K candidates fast and cheap; neural reranker scores them; LLM judges the final shortlist with a rubric. AST-based chunking on the *target* codebase improves recall to ~70% from ~42% baseline.

**Finding D2: `sqlite-vec` is the right embedded-vector choice for oxi.**
- Evidence Tier: 2; Confidence: 82%
- oxi already uses SQLite as its single source of truth (`oxi.db`). `sqlite-vec` is a SQLite extension that adds KNN to the existing connection, no new process, no new daemon, no new file format. Qdrant is faster but adds an entire service. LanceDB needs Lance file format. For ~1,500 entries (oxi roadmap + CHANGELOG + last 30 days of issues + last 30 days of merged PRs), sqlite-vec at ~5ms/query is well over budget.

**Finding D3: LLM-as-judge with structured rubric is the dominant practice, but biases must be controlled.**
- Evidence Tier: 1; Confidence: 85%
- Rubric for oxi proposals:
  - Relevance to oxi (1-10): does this relate to agent runtime, dispatch, safety, budget, claude-code orchestration?
  - Concreteness (1-10): is there a specific change-shaped action, or is it speculation?
  - Risk-tier fit (T0/T1/T2): blocker / polish / cleanup
  - Duplicate against existing roadmap (yes/no)
  - Final score = relevance × concreteness × (1.0 if novel else 0.0)
- Bias mitigation: Use **Haiku** as judge, not Sonnet/Opus — Haiku's known weaker self-confidence reduces position bias and over-promotion. Validate weekly with Pierre rating 10 random proposals; recalibrate rubric if Haiku-Pierre agreement drops below 0.7.

### Track E — Improvement-Proposal Generation

**Finding E1: Use the existing `auto_observe` event shape — don't invent a parallel one.**
- Evidence Tier: 1; Confidence: 98%
- `auto_observe` writes `observe_proposal` events with `task_id=NULL` and a JSON payload containing `signal_kind`, `target_identifier`, summary. Has built-in idempotency: identical `signal_kind + target_identifier` won't re-emit if a pending proposal already exists. Auto-improve should reuse this exact shape, just with new `signal_kind` values: `external_github_signal`, `external_x_signal`, `external_newsletter_signal`. The accept flow (`oxi v3 observe --accept <id>`) already injects to `front`, which `seed_from_roadmap` then promotes to a real task.

**Finding E2: One signal per proposal, batched for review.**
- Evidence Tier: 2; Confidence: 78%
- Pattern: Each ranked signal generates one `observe_proposal` event. The 5am loop then writes a single Markdown digest at `.oxi/auto-improve-digest-YYYY-MM-DD.md` listing all proposals in priority order. Pierre opens one file in the morning, runs `oxi v3 observe --accept <id>` for the ones he wants, ignores the rest. The unaccepted proposals stay in the ledger but expire after 14 days (configurable cooldown).

**Finding E3: Dedup against roadmap + CHANGELOG + closed issues is mandatory and non-trivial.**
- Evidence Tier: 2; Confidence: 85%
- Three-layer dedup:
  1. Identifier dedup: never propose an item with an identifier that already exists in `task` table. Generate identifiers via `T<tier>-A<n>` where A=auto, n=monotonic from event history.
  2. Semantic dedup: vector-search the proposed title+subtitle against embeddings of (a) all open task titles, (b) all closed-merged task titles in last 90 days, (c) all CHANGELOG entries. If max cosine > 0.85, skip with `external_proposal_dedup_skipped` event.
  3. Temporal dedup: same `signal_kind + target_identifier` not re-emitted if a pending proposal exists.

**Finding E4: Risk-tier assignment via heuristic, not LLM.**
- Evidence Tier: 3; Confidence: 70%
- Heuristic:
  - **T0** (blocker / safety): signal contains `CVE`, `security`, `auth bypass`, `data loss`, `RCE`, or matches an open Bandit/CodeQL finding. Otherwise never T0.
  - **T2** (cleanup): signal proposes refactor/cleanup/test-coverage with no user-facing change. LLM-judge tags this in the rubric.
  - **T1** (everything else): default.
- Rationale: Auto-loop should *never* unilaterally promote anything to T0 because T0 paused lower-tier work — the engine treats T0 as a "drop everything" signal.

### Track F — Approval Workflow + Safety

**Finding F1: Loop must NOT auto-merge, must NOT bypass critic, must NOT modify auto_merge policy.**
- Evidence Tier: 1; Confidence: 100%
- Pierre has explicitly set `auto_merge=False` for the oxi self-adapter. Auto-improve loop must never write to `policy()` at runtime, must never short-circuit the critic, must never call `auto_merge.merge_if_critic_approved()`. The proposals enter through `front` → `task` → normal dispatch → critic → human-approval, identical path as a hand-typed roadmap item.

**Finding F2: Default approval surface = single Markdown digest + ledger events. NOT issues, NOT PRs, NOT Slack.**
- Evidence Tier: 3; Confidence: 72%
- Justification: oxi's existing surfaces are `.oxi/brief.md`, `.oxi/dashboard.html`, ledger events. Adding GitHub issues or Slack channels expands the secret-management surface and adds an external dependency. A single file + ledger events is composable with oxi's existing operator workflow.
- Filename: `.oxi/auto-improve-digest-YYYY-MM-DD.md`. Linked from `.oxi/dashboard.html` via existing routes.

**Finding F3: Draft PR generation is OUT OF SCOPE for v1.**
- Evidence Tier: 1; Confidence: 80%
- Justification: The loop's job is *proposing* roadmap items. The oxi engine's existing dispatch loop already opens PRs from accepted items. Having auto-improve also open draft PRs in the same day would duplicate the dispatch path and bypass the critic.

### Track G — Failure Modes of Autonomous Research Loops

**Finding G1: Runaway cost is the dominant failure mode; oxi already has the only working defense.**
- Evidence Tier: 1; Confidence: 92%
- Documented incidents: AutoGPT users seeing "hundreds of dollars in token usage" from a single unattended run; Devin "looks like it's working but isn't" syndrome producing plausible-but-wrong fixes; cascading hallucinations where one invented class triggers downstream invented APIs. Replit AI and Google AI both deleted user data in 2025-2026.
- oxi's defense: `BudgetCaps(daily_hard_cap=20.0)` + `engine_health` + `kill.py` killswitch + `deadman` shouter + `auto_merge=False`. The auto-improve loop just has to not reach around these.

**Finding G2: The specific oxi failure mode — "noise faster than signal" — is detectable.**
- Evidence Tier: 3; Confidence: 70%
- Detection: Track the **acceptance ratio** of auto-improve proposals over a rolling 14-day window. Pierre opens digest, runs `accept` on N of M proposals. If `N/M < 0.15` for two consecutive weeks, the loop is generating noise — emit `auto_improve_noise_alert` ledger event and pause auto-improve dispatch until rubric is re-tuned.
- Implementation: New `auto_improve_health` module reading `observe_accepted` vs `external_proposal_emitted` event ratio. Pause is automatic; resume is manual.

**Finding G3: Hallucinated features that don't exist in oxi are the second-largest risk.**
- Evidence Tier: 2; Confidence: 75%
- Mitigation: LLM judge prompt must include the current oxi codebase outline (module names from `oxi_core/v3/`) and instruct: "Reject any proposal that references modules not in this list, or APIs not in oxi's adapter Protocol." Run this as a hard filter, not a scoring component.

---

## 3. Architecture: Where the Daily Loop Sits

```text
                    ┌─────────────────────────────────────────────────┐
                    │              Anthropic Claude Code              │
                    │              Routine — daily 5am UTC            │
                    │      (Vault-stored credentials, MCP only)       │
                    └──────────────────────┬──────────────────────────┘
                                           │ git pull + python -m oxi_core.v3.auto_external
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│                              oxi-core / v3 / auto_external (NEW)                   │
│                                                                                    │
│   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌────────────────────┐     │
│   │ Track B     │   │ Track C     │   │ Track C-2   │   │   PREFILTER        │     │
│   │ GitHub      │   │ Newsletter  │   │ X (Basic    │ → │   (BM25 over title │     │
│   │ topic+vel.  │   │ scrape      │   │  tier list) │   │    + body)         │     │
│   └─────────────┘   └─────────────┘   └─────────────┘   └─────────┬──────────┘     │
│                                                                   │                │
│                                                                   ▼                │
│                                                        ┌────────────────────┐      │
│                                                        │   VECTOR RERANK    │      │
│                                                        │   sqlite-vec vs.   │      │
│                                                        │   roadmap+CHANGELOG│      │
│                                                        └─────────┬──────────┘      │
│                                                                  │                 │
│                                                                  ▼                 │
│                                                        ┌────────────────────┐      │
│                                                        │   LLM JUDGE        │      │
│                                                        │   Haiku, rubric,   │      │
│                                                        │   structured JSON  │      │
│                                                        │   scoring          │      │
│                                                        └─────────┬──────────┘      │
│                                                                  │                 │
│                                                                  ▼                 │
│                                                        ┌────────────────────┐      │
│                                                        │   DEDUP            │      │
│                                                        │   id + semantic +  │      │
│                                                        │   temporal         │      │
│                                                        └─────────┬──────────┘      │
└──────────────────────────────────────────────────────────────────┼─────────────────┘
                                                                   │
                                                                   ▼
                                  ┌────────────────────────────────────────────────┐
                                  │   EMIT (writes to existing event table):       │
                                  │   - external_proposal_emitted                  │
                                  │   - observe_proposal (task_id=NULL,            │
                                  │       signal_kind=external_*)                  │
                                  │   - writes .oxi/auto-improve-digest-<date>.md  │
                                  └────────────────────┬───────────────────────────┘
                                                       │
                                                       ▼
                          ┌────────────────────────────────────────────────────────┐
                          │   PIERRE (next morning, on his Mac)                    │
                          │   - reads .oxi/auto-improve-digest-<date>.md           │
                          │   - runs `oxi v3 observe --accept <id>` for keepers    │
                          └────────────────────┬───────────────────────────────────┘
                                               │
                                               ▼
                          ┌────────────────────────────────────────────────────────┐
                          │   EXISTING PIPELINE (no changes)                       │
                          │   ingest_roadmap → seed_from_roadmap → planner →       │
                          │   dispatch → critic → auto_merge=False → Pierre        │
                          │   reviews PR → merges by hand                          │
                          └────────────────────────────────────────────────────────┘
```

---

## 4. Trigger Mechanism

**Recommendation: Anthropic Claude Code Routines.**

**Runner-up: GitHub Actions schedule + watchdog.**

**Rejected: Temporal / Airflow / Prefect / local cron.**

---

## 5. Ranking Pipeline

```text
SOURCES (raw fetch, ~5 min total)
  │ GitHub: 8 pinned-org release feeds + 5 topic queries
  │ Newsletters: AlphaSignal/Latent Space/BensBites public archive HTML
  │ X (optional): list of 15 accounts via Basic API tier
  ▼ raw candidates: ~150-300/day
  │
PREFILTER (local, free, ~1s)
  │ stars-velocity > 5/day for last 14d
  │ commits > 5 in last 30d (filters fake-star repos)
  │ dedup by URL + canonical-name
  ▼ filtered candidates: ~50-100/day
  │
BM25 RETRIEVAL (local SQLite FTS5, ~100ms)
  │ Index: oxi roadmap + CHANGELOG + last-90-day merged PR titles
  │ Keep top-30
  ▼ shortlist: 30 candidates
  │
VECTOR RERANK (sqlite-vec, all-MiniLM-L6-v2 local, ~500ms)
  │ Combine BM25 + vector via RRF (k=60)
  │ Keep top-15
  ▼ judged candidates: 15
  │
LLM JUDGE (Haiku, rubric prompt, ~5s, ~$0.05)
  │ Per-candidate JSON output:
  │   {relevance_1_10, concreteness_1_10, suggested_tier, duplicate, fabricated_module}
  │ Hard reject if fabricated_module=true
  ▼ proposals: top 5-10 with score >= 30
  │
DEDUP (3-layer)
  ▼ emitted: 3-7 proposals/day
  │
EMIT
  │ event(kind=external_proposal_emitted, payload=<rubric output>)
  │ event(kind=observe_proposal, task_id=NULL)
  │ append to .oxi/auto-improve-digest-YYYY-MM-DD.md
```

---

## 6. Safety Boundary — What the Loop Is NOT Allowed to Do

| # | Forbidden action | Enforcement |
|---|---|---|
| 1 | Merge any PR | Loop has no `gh pr merge` permission |
| 2 | Modify `auto_merge` policy | Adapter is read-only at runtime |
| 3 | Spend more than $5 in one daily run | Hard cap inside `auto_external.py`, in addition to $20 daily_hard_cap |
| 4 | Bypass `engine_state.is_stopping()` | Killswitch checked at entry, every 50 candidates, before every LLM call |
| 5 | Open issues / send Slack / send email | Out of scope v1 |
| 6 | Promote anything to T0 unless security keyword present | Hard heuristic |
| 7 | Touch any file outside `.oxi/auto-improve-digest-*.md` | File-write guard |
| 8 | Call `claude` for code generation | LLM judge uses Haiku via Anthropic API, not via `claude` subprocess |
| 9 | Call dispatch directly | Loop does not import `oxi_core.v3.dispatch`; lint enforced |
| 10 | Re-emit a proposal less than 14 days after dedup-skip | Existing `auto_observe` idempotency pattern |

**Single-line rule:** The auto-improve loop is allowed to do exactly two things: write `event` rows of specific kinds and write one Markdown digest per day.

---

## 7. Migration Path

**New modules in `oxi-core/src/oxi_core/v3/`:**
- `auto_external.py` — main entry point
- `auto_external_sources.py` — GitHub / newsletter / X fetchers
- `auto_external_rank.py` — BM25 + vector + LLM-judge stages
- `auto_external_dedup.py` — three-layer dedup
- `auto_external_emit.py` — event-writing + digest generation
- `auto_improve_health.py` — Track G2 noise-detection module

**New constants in `oxi_core.v3.ledger_events`:**
- `EXTERNAL_PROPOSAL_EMITTED`
- `EXTERNAL_PROPOSAL_DEDUP_SKIPPED`
- `EXTERNAL_PROPOSAL_REJECTED_FABRICATED`
- `AUTO_IMPROVE_NOISE_ALERT`
- `AUTO_IMPROVE_PAUSED` / `AUTO_IMPROVE_RESUMED`

**New optional Adapter protocol method:**

```python
def auto_improve_config(self) -> AutoImproveConfig | None:
    """Return source config for the daily auto-improve loop, or None to disable."""
```

**New CLI subcommands:**
- `oxi v3 auto-improve` — manual run
- `oxi v3 auto-improve --dry-run` — fetch + rank but don't emit
- `oxi v3 auto-improve unpause` — clear noise-alert pause

---

## 8. What We Are NOT Doing

- Auto-merging anything. Pierre still reviews every PR.
- Opening draft PRs from the loop.
- Replacing `auto_observe`.
- Building a new approval UI.
- Direct X scraping at scale.
- Multi-day stateful synthesis.
- Custom workflow engine.
- Generating code in the loop.
- Tracking trends — momentum scoring, hype indices.
- Auto-promoting to T0.

---

## 9. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | LLM judge hallucinates oxi modules that don't exist | Medium | High | Hard filter against current `oxi_core/v3/` module list in judge prompt |
| 2 | Daily run silently fails and operator doesn't notice | Medium | Medium | `auto_improve_health` emits count == 0 → `engine_unhealthy` event; deadman shouts after 48h |
| 3 | Newsletter sites change HTML; scraper breaks | High | Low | Per-source try/except; emit `auto_improve_source_failed` event |
| 4 | LLM judge produces high-confidence wrong rankings | Medium | Medium | 14-day acceptance ratio auto-pause if < 0.15 |
| 5 | X API tier removed for Pierre mid-flight | Low | Low | X is tertiary source; loop falls back to GitHub + newsletters |
| 6 | Routines feature changes shape before GA | Medium | Medium | Trigger mechanism is one module; swap to GitHub Actions + watchdog |
| 7 | Cost overrun from chatty LLM judge | Low | High | Hard $5/day cap inside `auto_external.py` + existing $20 `daily_hard_cap` |
| 8 | Auto-improve identifier `T1-A4` collides with hand-written | Very low | Low | Identifier monotonic counter + uniqueness enforced |
| 9 | Behavioral-contract layer (PR #118) lands and changes critic shape | Medium | Low | Loop doesn't talk to critic directly; only writes proposals to `front` |
| 10 | Fake-star repos slip through prefilter | Medium | Low | Star-velocity AND commit-activity dual-filter |
| 11 | Auto-improve becomes the dominant roadmap-source | Medium | High | `daily_proposal_cap: int=10` in adapter config |

---

## 10. Cost Projection

**Daily volume assumptions:**
- 50 GitHub topic-search results + 100 release-feed items = ~150 raw candidates
- 5 newsletter HTML fetches → ~50 mentions extracted
- 15 X account posts (if Basic tier) → ~50 candidates
- After prefilter: 50-100 candidates
- LLM judge runs on 15

**LLM cost (Haiku 4.5):**
- 15 candidates × (1,500 × $0.80 + 300 × $4) / 1M = **$0.036/day**
- Plus one synthesis pass: ~$0.04
- Optional: re-rank with Sonnet judge on top-5: $0.075

**Total LLM: $0.04 - $0.20 / day**.

**X API cost (if used):** ~50 reads/day × $0.005 = **$0.25/day**.

**Anthropic Routines session-hour:** $0.08/hour × ~5min = **$0.007/day**.

**GitHub API:** **$0** (within free 5,000/hour authenticated).

**Newsletter scraping:** **$0**.

**Total daily cost: $0.30 - $0.80/day (~$10-25/month)**, well under the $20/day daily_hard_cap.

---

## 11. Confidence Assessment

- **Overall Confidence:** 78%
- **Assumptions made:**
  - Pierre's Max plan tier `20x` gives 25 daily Routine runs — re-verify before committing.
  - The behavioral-contract layer (PR #118) keeps the `front` table as the canonical proposal-ingestion surface.
  - `sqlite-vec` extension loads cleanly in oxi's existing SQLite connection.
  - AlphaSignal et al. don't block scrapers via Cloudflare or paywall changes mid-flight.

---

## 12. Recommended Next Steps

1. Run `/deep-plan` against this report.
2. Verify Routines daily-run cap on Max plan tier `20x` by checking docs directly.
3. Spike `sqlite-vec` against oxi's existing `.oxi/oxi.db`.
4. Draft the rubric prompt and run a calibration set of 20 historical roadmap items past Haiku judge.
5. Add T1-A1 / T1-A2 / T1-A3 to `docs/roadmap.md`.

---

**Report ends. Ready for /deep-plan ingestion.**
