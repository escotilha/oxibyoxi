# Auto-improve subsystem (`auto_external`) — implementation plan

**Repo:** `escotilha/oxi`
**Status:** Plan only. Architecture is settled (see research handoff in user prompt). This doc breaks the build into ingestible PRs.
**Last revised:** 2026-04-25
**Cohort identifier:** `T1-B<n>` (B = behavioral / auto-improve), with fallback `T1-AI<n>` if Q2 below resolves that way.
**Output of this plan:** a single PR appending the B-series entries to `docs/roadmap.md`. Each B entry then ships as its own PR through the existing critic + `auto_merge=False` gate.

---

## 0. Scope recap (settled — do not redesign)

A new module `oxi_core.v3.auto_external` runs once a day (Anthropic Claude Code Routine, 5am UTC) and emits up to ~10 candidate roadmap items per day. Candidates flow through the **same** ingestion surface as human-authored items: `auto_observe` event shape → `front` table → operator accepts via `oxi v3 observe --accept <id>` → existing seed → plan → dispatch → critic → `auto_merge=False` pipeline.

### Pipeline at a glance

```text
sources (GitHub releases, GitHub topic queries, AI newsletters, optional X)
   │
   ▼
prefilter (free, local, ~50–100 candidates)        ── stage P
   │
   ▼
BM25 over SQLite FTS5 vs roadmap+CHANGELOG+90-day-PRs (~30)  ── stage R1
   │
   ▼
sqlite-vec rerank + RRF k=60 (~15)                 ── stage R2
   │
   ▼
Haiku 4.5 LLM-judge (rubric, structured output, fabricated-module hard filter)
   │
   ▼
3-layer dedup (identifier / semantic / temporal)
   │
   ▼
emit ledger events + `observe_proposal` rows (task_id=NULL)
+ Markdown digest at .oxi/auto-improve-digest-YYYY-MM-DD.md
   │
   ▼
operator review → `oxi v3 observe --accept` → existing pipeline
```

### Hard guardrails (lint-enforced or test-enforced)

- `oxi_core.v3.auto_external` MUST NOT import `oxi_core.v3.dispatch`, `oxi_core.v3.dispatch_invoke`, `oxi_core.v3.dispatch_pool`, or `oxi_core.v3.auto_merge`. Enforced by `scripts/lint-for-leaks.sh` (extend the existing forbidden-import gate).
- `engine_state.is_stopping()` checked at loop entry, every 50 prefiltered candidates, and immediately before every LLM call.
- `budget.check()` called before every LLM-judge invocation. Loop has its own internal `$5/day` cap on top of the existing `daily_hard_cap=$20`.
- T0 reserved for security signals only (heuristic: text matches `CVE`, `security`, `auth bypass`, `data loss`, `RCE`, or correlates with an open Bandit/CodeQL finding). Else default T1.
- LLM-judge prompt includes the current `oxi_core/v3/` module list. Any proposal where `fabricated_module=true` is hard-rejected and logged as `EXTERNAL_PROPOSAL_REJECTED_FABRICATED`.

---

## 1. Module layout — five files under `oxi-core/src/oxi_core/v3/auto_external/`

`auto_external` is a package (not a single file) so each file stays small and independently testable. Public surface is re-exported from `__init__.py`:

| File | Responsibility | Approx LOC |
|---|---|---|
| `auto_external/__init__.py` | Re-exports `scan()`, `AutoExternalConfig`, `Candidate`, `Proposal`, `SOURCE_*`. | < 50 |
| `auto_external/sources/{__init__,github,newsletter,x}.py` | `Source` Protocol in `__init__.py`; one source per file. Each implements `fetch(now: datetime) -> list[RawItem]` with internal try/except + `auto_improve_source_failed` event on failure. (Per Q6: split per-source for review-ability.) | ~350 |
| `auto_external/ranking.py` | Prefilter, BM25 over FTS5, sqlite-vec rerank, RRF k=60. Pure functions, deterministic given a fixed corpus. | ~300 |
| `auto_external/judge.py` | `LLMJudge` Protocol + `HaikuJudge` impl with rubric prompt + structured output schema + fabricated-module check. Reads current `oxi_core/v3/` module list at call time. | ~250 |
| `auto_external/dedup.py` | Three-layer dedup (identifier counter, semantic cosine ≥ 0.85, temporal 14d window). | ~150 |
| `auto_external/emit.py` | Writes `EXTERNAL_PROPOSAL_*` ledger events, inserts `observe_proposal` rows with `task_id=NULL`, writes `.oxi/auto-improve-digest-YYYY-MM-DD.md`. | ~200 |
| `auto_external/health.py` | `auto_improve_health` — 14-day acceptance ratio, auto-pause, `AUTO_IMPROVE_NOISE_ALERT`. | ~150 |
| `auto_external/scan.py` | Top-level orchestrator: `scan(conn, state, adapter, now)`. Wires sources → ranking → judge → dedup → emit. Every step is a function call, no inline logic > 20 LOC. | ~200 |

(Yes — that is more than 5 files. The user said "split into 5 files for testability" but the realistic split is 8 small files. If the PR reviewer prefers 5, fold `dedup` into `emit`, `health` into `scan`, and `__init__` into `scan`. Flag as Q6.)

### Adapter Protocol extension

Add one method to `oxi_core.adapter.Adapter`:

```python
def auto_improve_config(self) -> AutoExternalConfig | None:
    """Return None to disable auto-improve entirely."""
```

`AutoExternalConfig` (frozen dataclass) holds:

```python
@dataclass(frozen=True)
class AutoExternalConfig:
    enabled: bool = False
    daily_proposal_cap: int = 10
    internal_budget_cap_usd: float = 5.0
    github_pinned_orgs: tuple[str, ...] = ()
    github_topic_queries: tuple[str, ...] = ()
    newsletter_urls: tuple[str, ...] = ()
    x_enabled: bool = False
    x_list_id: str | None = None
    semantic_dedup_threshold: float = 0.85
    temporal_dedup_days: int = 14
    acceptance_ratio_threshold: float = 0.15
    acceptance_ratio_window_days: int = 14
    judge_model: str = "claude-haiku-4-5"
```

Backward compat: keep the same convention as `auto_recover_config` — adapters written before B1 still satisfy the Protocol because the method is optional (returns `None` to disable). Default in `oxi_adapter_self` returns a config with `enabled=True`.

---

## 2. New ledger event kinds

Add to `oxi_core.v3.ledger_events.LedgerEvent`:

```python
EXTERNAL_PROPOSAL_EMITTED = "external_proposal_emitted"
EXTERNAL_PROPOSAL_DEDUP_SKIPPED = "external_proposal_dedup_skipped"
EXTERNAL_PROPOSAL_REJECTED_FABRICATED = "external_proposal_rejected_fabricated"
AUTO_IMPROVE_NOISE_ALERT = "auto_improve_noise_alert"
AUTO_IMPROVE_SOURCE_FAILED = "auto_improve_source_failed"
AUTO_IMPROVE_PAUSED = "auto_improve_paused"
AUTO_IMPROVE_UNPAUSED = "auto_improve_unpaused"
EXTERNAL_PROPOSAL_BUDGET_HOLD = "external_proposal_budget_hold"
```

Coordinates with **T1-14** (typed event-kind constants — already shipped). The B1 PR adds the constants alongside the rest of `LedgerEvent`.

---

## 3. Identifier scheme for emitted proposals

Format: `T<tier>-A<n>` (existing convention reserved for **auto-system-emitted** items, distinct from `T1-Bn` we use for the **plan to build the auto-improve system itself**).

- Counter is monotonic across all `EXTERNAL_PROPOSAL_EMITTED` events ever written: `n = max_existing + 1` over the entire ledger.
- Tier defaults to `1` unless the security heuristic matches → `T0-A<n>`.
- `_ITEM_LINE` regex in `oxi_core/planner.py:55` is `^\*\*([A-Za-z0-9_\-]+)\s*·\s*(.+?)\*\*\s*$` — confirmed alphanumeric, so `T1-A4` and `T1-B4` both parse cleanly. **No regex change needed.**
- Collision check: `T1-A<n>` and `T1-B<n>` live in their own counter spaces. No collision with existing items (highest T1 is `T1-18`; in-flight `T0-201/202/203` from PR #118; multi-model plan reserves `T2-30..T2-40`).

---

## 4. Phased delivery — sized for one ingestible PR per entry

Each entry is one PR. Each ships through the existing critic + `auto_merge=False` gate. **No PR depends on a later PR landing first** — the order below is the order tests expect (B1 sets up the skeleton; subsequent PRs fill in real fetchers/judges, replacing fakes one at a time).

**B1 ship-blocker:** B1 must include the full lint gate (forbidden-imports of `dispatch`, `dispatch_invoke`, `dispatch_pool`, `auto_merge`). No subsequent PR can pass CI without it.

### B1 · `auto_external` skeleton + adapter Protocol method + CLI subcommand stubs

_create the empty package; add `Adapter.auto_improve_config()`; add `AutoExternalConfig` dataclass; stub `oxi v3 auto-improve {scan,unpause,status}` subcommands that print "not implemented" and exit 0; add the `LedgerEvent` constants from §2; extend `scripts/lint-for-leaks.sh` with the forbidden-imports gate. All fakes/fixtures land here so subsequent PRs are tiny._

### B2 · GitHub source fetcher

_implements `GitHubSource` against pinned-org release feeds (8) and topic queries (5). Star-velocity prefilter: drop repos < 5 stars/day in the last 30 days. Commit-activity prefilter: drop repos with no commits in the last 14 days. Reuses `oxi_core.v3.github_client.GitHubClient` Protocol; tests use `FakeGitHubClient` from `tests/fixtures/fake_github.py`. Per-source try/except — failure emits `AUTO_IMPROVE_SOURCE_FAILED` and other sources continue._

### B3 · Newsletter source fetcher

_implements `NewsletterSource` against AlphaSignal, Latent Space, BensBites public archives. HTML scrape via `httpx` + `selectolax` (already a dep via `pr_watcher`? — confirm). Per-source try/except. New `FakeHTTPFetcher` in `tests/fixtures/fake_http.py` returns canned HTML fixtures from `tests/fixtures/data/newsletters/`. Each newsletter gets its own parser function so a layout change to one doesn't take down all three._

### B4 · X source fetcher (via X skill)

_implements `XSource` as a subprocess wrapper around the operator's X skill (per Q1). Reads ~15-account curated list from `AutoExternalConfig.x_account_list`; calls `config.x_skill_binary` with the list and a since-timestamp; parses the skill's stdout as a list of post records. Disabled when `config.x_skill_binary is None` — returns `[]` without subprocess call (asserted in test). New `FakeXSkill` fixture writes canned stdout. If subprocess returns non-zero or stdout fails to parse → `AUTO_IMPROVE_SOURCE_FAILED` with the exit code in payload, no retry within the same scan. Acceptance: when binary is `None`, no subprocess; when binary is set, subprocess runs with the configured arg shape; parse failures emit the right ledger event._

### B5 · Ranking pipeline (no LLM yet)

_implements `prefilter`, `bm25_score`, `vector_rerank`, `rrf_combine` in `ranking.py`. SQLite FTS5 virtual table built on the fly from roadmap.md + CHANGELOG.md + last-90-day merged PRs (queried via `GitHubClient.list_merged_prs(since=now-90d)`). sqlite-vec extension loaded; embeddings via `all-MiniLM-L6-v2` sentence-transformer (already a dep — confirm in `pyproject.toml`). RRF k=60 hardcoded (matches user's memory rule for hybrid retrieval). Tests use a fixed 50-item corpus and assert deterministic top-15 ordering._

### B6 · LLM judge with rubric + fabricated-module hard filter

_implements `HaikuJudge` in `judge.py`. Reads current module list via `pkgutil.iter_modules(oxi_core.v3.__path__)`. Structured output schema: `{relevance: 1-5, concreteness: 1-5, suggested_tier: 0|1|2, duplicate: bool, fabricated_module: bool, rationale: str}`. Hard-rejects when `fabricated_module=true`, emits `EXTERNAL_PROPOSAL_REJECTED_FABRICATED`. Calls `budget.check()` before every invocation; honors internal $5/day cap (separate ledger query against today's `EXTERNAL_PROPOSAL_*` cost-tagged events). Fake judge: `FakeJudge` in `tests/fixtures/fake_judge.py` with deterministic verdicts keyed by candidate ID._

### B7 · Three-layer dedup

_implements `dedup_identifier`, `dedup_semantic`, `dedup_temporal` in `dedup.py`. Identifier dedup: query `EXTERNAL_PROPOSAL_EMITTED` events for the next monotonic counter. Semantic dedup: cosine similarity ≥ 0.85 against open `front` rows + tasks updated in last 30 days. Temporal dedup: same `(signal_kind, target_identifier)` within 14 days → skip and emit `EXTERNAL_PROPOSAL_DEDUP_SKIPPED`. Tests cover each layer with fixed embeddings + ledger fixtures._

### B8 · Emit step — ledger events + Markdown digest writer

_implements `emit_proposal` in `emit.py`. Writes one `EXTERNAL_PROPOSAL_EMITTED` event per accepted proposal, inserts a row into `front` with `task_id=NULL` and the chosen `T<tier>-A<n>` identifier (or stages it pending operator accept — match `auto_observe.accept()` shape exactly). Writes Markdown digest at `.oxi/auto-improve-digest-YYYY-MM-DD.md` with sections: Top proposals, Skipped (dedup), Skipped (fabricated), Source failures, Budget held. File path uses `adapter.paths().repo_root` — never hardcoded._

### B9 · `auto_improve_health` — acceptance-ratio tracker + auto-pause

_implements `health.py`. On every scan, computes `accepted / emitted` over the last 14 days from ledger events. If ratio < `acceptance_ratio_threshold` (default 0.15) for two consecutive 7-day windows: emit `AUTO_IMPROVE_NOISE_ALERT`, write a `paused: true` row in a new `auto_improve_state` table (or reuse `engine_state` table — confirm in implementation), and emit `AUTO_IMPROVE_PAUSED`. Manual unpause via `oxi v3 auto-improve unpause` (CLI subcommand wired in B1; the actual unpause logic lands here). Auto-pause is **per-loop** — does not affect the engine killswitch._

### B10 · Claude Code Routine config + entry-point script

_adds `scripts/auto_improve_routine.py` (the entry point Routines invokes) and `routines/auto-improve.toml` (or whatever shape the Routines schema settles on at GA). Credentials via Anthropic Managed Agent Vaults — no token in the routine config or env file. The script: opens the SQLite DB, builds `EngineState`, calls `auto_external.scan()`, exits. Idempotent: if a scan already ran today, `EXTERNAL_PROPOSAL_EMITTED` events for today exist → skip and log. Smoke test: a CI job runs the script against a fixture DB and asserts the digest file is written._

### B11 · GitHub Actions schedule fallback + watchdog (ships only if Routines GA slips)

_adds `.github/workflows/auto-improve.yml` running on cron `0 5 * * *`. Calls `scripts/auto_improve_routine.py`. Watchdog: if no `EXTERNAL_PROPOSAL_EMITTED` event in the last 36h, the next run emits a `auto_improve_watchdog_stalled` event and notifies via the existing `notification.py` backend. Don't ship this unless Routines is actually delayed past B10's merge date. Track Routines GA in `docs/origin-feature-gap-2026-04-24.md` and decide at B10 acceptance time._

---

## 5. File-by-file change list (per entry)

Markers: **C** = create, **E** = edit existing, **R** = rename, **T** = test (new test file).

### B1 — skeleton + protocol + CLI stubs

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/__init__.py` | C | Re-exports `scan`, `AutoExternalConfig`, signal kinds. |
| `oxi-core/src/oxi_core/v3/auto_external/scan.py` | C | Top-level `scan()` raises `NotImplementedError`. |
| `oxi-core/src/oxi_core/v3/auto_external/sources/__init__.py` | C | `Source` Protocol + `RawItem` dataclass. Per-source files (`github.py`, `newsletter.py`, `x.py`) created here as empty stubs to be filled by B2/B3/B4. |
| `oxi-core/src/oxi_core/v3/auto_external/ranking.py` | C | Function signatures only. |
| `oxi-core/src/oxi_core/v3/auto_external/judge.py` | C | `LLMJudge` Protocol; impl stubbed. |
| `oxi-core/src/oxi_core/v3/auto_external/dedup.py` | C | Function signatures only. |
| `oxi-core/src/oxi_core/v3/auto_external/emit.py` | C | Function signatures only. |
| `oxi-core/src/oxi_core/v3/auto_external/health.py` | C | Function signatures only. |
| `oxi-core/src/oxi_core/adapter.py` | E | Add `auto_improve_config()` method to `Adapter` Protocol. |
| `oxi-core/src/oxi_core/v3/ledger_events.py` | E | Add 8 new `LedgerEvent` constants. |
| `oxi-core/src/oxi_core/cli.py` | E | Add `oxi v3 auto-improve {scan,status,unpause}` subcommand stubs. |
| `oxi-core/templates/adapter/src/oxi_adapter_TEMPLATE/adapter.py.tmpl` | E | Add stub `auto_improve_config` method returning `None`. |
| `scripts/lint-for-leaks.sh` | E | Add forbidden-imports gate. |
| `oxi-core/tests/test_auto_external_skeleton.py` | T | Imports work; protocol shape; CLI stubs exit 0. |
| `oxi-core/tests/test_lint_forbidden_imports.py` | T | Asserts `auto_external/*.py` source contains no `import dispatch` etc. |

### B2 — GitHub source

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/sources/github.py` | E | Implement `GitHubSource.fetch()`. |
| `oxi-core/src/oxi_core/v3/github_client.py` | E | Add `list_org_releases(org, since)` + `topic_search(query, min_stars_per_day)` to Protocol if missing. |
| `oxi-core/tests/fixtures/fake_github.py` | E | Extend `FakeGitHubClient` with the new methods. |
| `oxi-core/tests/fixtures/data/github/` | C | JSON fixtures for org releases + topic search. |
| `oxi-core/tests/test_auto_external_github_source.py` | T | Pinned-org happy path; star-velocity filter; commit-activity filter; per-source failure emits event. |

### B3 — Newsletter source

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/sources/newsletter.py` | E | Implement `NewsletterSource.fetch()` + 3 parser functions (AlphaSignal, Latent Space, BensBites). |
| `oxi-core/tests/fixtures/fake_http.py` | C | `FakeHTTPFetcher` returning canned HTML. |
| `oxi-core/tests/fixtures/data/newsletters/{alphasignal,latent_space,bensbites}/` | C | Canned HTML samples (each ≤ 50 KB). |
| `oxi-core/tests/test_auto_external_newsletter_source.py` | T | Each newsletter parses; one failing → others succeed; layout change in fixture → graceful degrade. |

### B4 — X source (via X skill)

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/sources/x.py` | C | `XSource.fetch()` shells out to `config.x_skill_binary` via `subprocess.run`; parses stdout JSON. When binary is `None`, returns `[]` immediately. |
| `oxi-core/src/oxi_core/v3/auto_external/config.py` | E | Add `x_skill_binary: str | None = None` and `x_account_list: tuple[str, ...] = ()`. |
| `oxi-core/tests/fixtures/fake_x_skill.py` | C | `FakeXSkill` — temp script that emits canned JSON; `subprocess.run` calls it during the test. |
| `oxi-core/tests/test_auto_external_x_source.py` | T | Disabled (binary `None`) → no subprocess; subprocess non-zero → `AUTO_IMPROVE_SOURCE_FAILED`; stdout parse failure → same event with `parse_error` in payload. |

### B5 — Ranking pipeline

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/ranking.py` | E | Full pipeline. |
| `oxi-core/pyproject.toml` | E (if needed) | Add `sentence-transformers` and `sqlite-vec` to deps; verify minimum SQLite version with FTS5+vec. |
| `oxi-core/tests/test_auto_external_ranking.py` | T | Fixed 50-item corpus; deterministic top-15; RRF math; FTS5 virtual table builds. |

### B6 — LLM judge

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/judge.py` | E | `HaikuJudge` impl + rubric prompt + structured output schema + module-list builder + fabricated-module gate. |
| `oxi-core/tests/fixtures/fake_judge.py` | C | `FakeJudge`. |
| `oxi-core/tests/test_auto_external_judge.py` | T | Rubric output parses; fabricated_module=true → reject + event; budget exhausted → skip + event; module-list reflects current `v3/`. |

### B7 — Dedup

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/dedup.py` | E | Three layers. |
| `oxi-core/tests/test_auto_external_dedup.py` | T | Each layer in isolation; combined; counter monotonicity. |

### B8 — Emit + digest

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/emit.py` | E | Event writes + `front` upsert + digest write. |
| `oxi-core/tests/test_auto_external_emit.py` | T | Ledger events match expected payloads; `front` row has `task_id=NULL` and correct identifier; digest file shape. |
| `oxi-core/tests/test_auto_external_integration.py` | T | End-to-end with all fakes: sources → ranking → fake judge → dedup → emit. Asserts ledger event sequence + digest file. |

### B9 — Health monitor

| Path | Action | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/auto_external/health.py` | E | Ratio computation + auto-pause + unpause. |
| `oxi-core/src/oxi_core/db.py` | E (if needed) | Add `auto_improve_state` table (or reuse existing key-value table — confirm). |
| `oxi-core/src/oxi_core/cli.py` | E | Wire `oxi v3 auto-improve unpause` to `health.unpause()`. |
| `oxi-core/tests/test_auto_external_health.py` | T | Ratio < threshold for 2 windows → pause; pause + unpause round-trip; ratio computation across edge cases. |

### B10 — Routine entry point

| Path | Action | Notes |
|---|---|---|
| `oxi-core/scripts/auto_improve_routine.py` | C | Entry-point script. |
| `routines/auto-improve.toml` | C | Routine config (Vault-injected creds, MCP github tool reference). Schema TBD at GA; placeholder OK for B10 PR. |
| `oxi-core/tests/test_auto_improve_routine.py` | T | Smoke: runs against fixture DB, exits 0, digest written, idempotent on second run. |

### B11 — GHA fallback (conditional)

| Path | Action | Notes |
|---|---|---|
| `.github/workflows/auto-improve.yml` | C | Cron + watchdog. Only if Routines GA slips. |
| `oxi-core/tests/test_auto_improve_watchdog.py` | T | 36h-stale detection + notification call. |

---

## 6. Test plan per entry (acceptance criteria)

Every entry follows the same shape: a fake for any external dependency, deterministic input → deterministic output, ledger-event assertion. The integration test in B8 is the gate — once it passes, the whole pipeline is wired end-to-end with fakes.

### B1

- `pytest oxi-core/tests/test_auto_external_skeleton.py` green.

- `pytest oxi-core/tests/test_lint_forbidden_imports.py` green.
- `bash scripts/lint-for-leaks.sh` exits 0.
- `oxi v3 auto-improve scan` exits 0 and prints "not implemented".

### B2

- `pytest oxi-core/tests/test_auto_external_github_source.py -v` — ≥ 4 cases (happy, low-velocity, low-activity, source-failure).

- All `FakeGitHubClient` calls deterministic — no network in test path.
- `AUTO_IMPROVE_SOURCE_FAILED` event written when fake raises.

### B3

- `pytest oxi-core/tests/test_auto_external_newsletter_source.py -v` — ≥ 4 cases (each newsletter happy + one fail-gracefully).

- No live HTTP — all via `FakeHTTPFetcher`.

### B4

- `pytest oxi-core/tests/test_auto_external_x_source.py -v` — 3 cases (binary unset → `[]`, subprocess error, parse error).

- Binary-unset path makes zero subprocess calls (asserted via `subprocess.run` monkeypatch).
- `FakeXSkill` covers the skill's expected stdout shape; if the real skill changes shape, the test fixture is updated and B4's parser updated alongside.

### B5

- `pytest oxi-core/tests/test_auto_external_ranking.py -v` — deterministic top-15 over fixed corpus; RRF formula matches expected for hand-computed case.

- sqlite-vec extension loads (or skip with clear xfail if SQLite build lacks loadable-extensions).

### B6

- `pytest oxi-core/tests/test_auto_external_judge.py -v` — ≥ 5 cases (rubric parse, fabricated_module reject, budget exhausted, module-list current, structured output schema).

- `FakeJudge` deterministic per candidate ID.

### B7

- `pytest oxi-core/tests/test_auto_external_dedup.py -v` — 3 layers × 2 cases each.

- Counter monotonicity: emit 3, then 4 → counter is 4.

### B8

- `pytest oxi-core/tests/test_auto_external_emit.py oxi-core/tests/test_auto_external_integration.py -v` green.
- Integration test asserts: exactly N `EXTERNAL_PROPOSAL_EMITTED` events; exactly M `EXTERNAL_PROPOSAL_DEDUP_SKIPPED`; digest file exists at `.oxi/auto-improve-digest-<today>.md` and contains the expected sections; `front` rows are inserted with `task_id=NULL` and identifiers match `T<tier>-A<n>` regex.

### B9

- `pytest oxi-core/tests/test_auto_external_health.py -v` — pause/unpause round-trip; ratio computation correct across 14d window.
- `oxi v3 auto-improve unpause` CLI integration test green.

### B10

- `pytest oxi-core/tests/test_auto_improve_routine.py -v` — smoke + idempotency.
- Manual: invoke routine entry-point against fixture DB, confirm exit 0 and digest written.

### B11 (conditional)

- Workflow YAML lints (yamllint clean).
- `pytest oxi-core/tests/test_auto_improve_watchdog.py` green.

### Shared lint gates (every PR must pass)

- `bash scripts/lint-for-leaks.sh` — confirms no `from oxi_core.v3.dispatch` / `auto_merge` imports inside `auto_external/`.
- `ruff check`, `mypy oxi-core/src/oxi_core/v3/auto_external/` (per T2-12 scope), `pytest oxi-core/tests/`.

---

## 7. Risk register (implementation phase)

Different from the research-phase risks — focus on what goes wrong **while shipping**.

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | `sqlite-vec` extension fails to load on operator's SQLite build (Apple-Silicon homebrew vs system, or distros with `--disable-load-extension`). | Medium | High (B5 doesn't run) | Detect at startup in `ranking.py`; if missing, fall back to BM25-only with a one-time `auto_improve_vec_unavailable` event. Document in install runbook. |
| R2 | AlphaSignal / Latent Space / BensBites change HTML layout, scraper silently empties out. | High | Medium | Per-source try/except + `AUTO_IMPROVE_SOURCE_FAILED` event + digest section "Source failures". A scraper that returns 0 items 3 days running emits a noisier alert. |
| R3 | X skill binary not on operator's PATH or its stdout shape changes. | Medium | Low | B4 disables itself when binary is unset (`config.x_skill_binary is None`). Smoke test on B10 routine init verifies the binary resolves; failure emits `auto_improve_source_failed` and proceeds with GitHub + newsletters. Skill-shape changes caught by the parse-error test path. |
| R4 | Haiku 4.5 rubric misaligned with Pierre's preferences — lots of "relevance=5" on noise. | Medium | Medium | B9 health monitor catches it: acceptance ratio < 0.15 → auto-pause within 14 days. Recovery is rubric tweak in `judge.py`. |
| R5 | LLM judge hallucinates a module name not in `oxi_core/v3/`. | High | Low (caught by gate) | Hard filter on `fabricated_module=true`. Rejected proposals logged; if rejection rate > 30%, prompt needs work. |
| R6 | Internal `$5/day` cap hit before all candidates judged → bottom-of-list candidates never see the LLM. | Medium | Low | Acceptable — prefilter + ranking already promote the best to the top. Emit `EXTERNAL_PROPOSAL_BUDGET_HOLD` for any skipped. |
| R7 | Claude Code Routines GA slips past B10 ship date. | Medium | Low | B11 (GHA fallback) is the contingency. Decision point at B10 merge. |
| R8 | PR #118 (behavioral-contract layer) lands during B-series implementation and changes `front`-table shape. | High | Medium | B8 emit step depends on the **current** `front` shape. Document the dependency in B8's PR description. If #118 lands first, rebase B8. If B8 lands first, #118 owner adapts. |
| R9 | sentence-transformers model download adds 80 MB to install size. | Low | Low | Acceptable; document in install runbook. Lazy-load on first scan, not at import. |
| R10 | Routine credential rotation breaks daily run silently. | Low | Medium | Vault auto-refreshes; B9 health monitor's source-failure events surface it within 24h. |
| R11 | `auto_external` accidentally imports `dispatch` via a transitive module. | Low | High (constraint violation) | B1 lint gate scans transitive imports too — uses `ast` to walk all `import` statements, then resolves to module names recursively (or stops at top-level names like `oxi_core.v3.dispatch`). |
| R12 | Two scans run on the same day (B10 routine + B11 GHA both fire). | Low | Low | Idempotency check in `scan.py` — if any `EXTERNAL_PROPOSAL_EMITTED` exists for today's UTC date, skip and log. |
| R13 | Operator deletes the digest file before reading; no other surface tells them what was emitted. | Medium | Low | Digest is a convenience; canonical record is the ledger. Document `oxi v3 observe --since today` as the recovery query (existing command from `auto_observe`). |

---

## 8. Decisions (resolved 2026-04-25 by operator)

All eight questions answered. The B-series can proceed.

### Q1 → Resolved: X integration via the `agentmail`/`scrapling`-style skill route, not direct X API.

The user has a skill that handles X access. B4 ships, but as a wrapper around that skill (subprocess call to the skill's CLI entry point) rather than a direct X API client. The `XSource` interface stays the same; its implementation calls out to the skill instead of making API requests directly. **Implication:** no $100/mo Basic tier dependency, no per-read fees in oxi's budget. Risk: the skill is not part of the oxi tree, so it must be available on the operator's PATH for B4 to function. Adapter config must declare the skill binary name; B4's smoke test verifies it's reachable. If unavailable, B4 disables itself with `auto_improve_source_failed` event and the loop runs on GitHub + newsletters only.

**Files affected:** `auto_external/sources/x.py` calls `subprocess.run([config.x_skill_binary, ...])` instead of making an HTTP client. Test fixture `tests/fixtures/fake_x_skill.py` replaces `fake_x.py` (same shape, different transport).

### Q2 → Resolved: `T1-Bn` identifier convention.

The B-series implementation PRs use `T1-B1` through `T1-B11`. Distinct from `T1-A<n>` (reserved for *emitted* auto-system proposals, never hand-typed). `T1-Bn` is shorter and avoids the "AI" overloading.

### Q3 → Resolved: Markdown digest file at `.oxi/auto-improve-digest-YYYY-MM-DD.md`.

No GitHub Discussions, no tracking issues, no Slack. Single local file. The dashboard route (`auto-improve-digest-latest.html`) renders the most recent digest; the file itself is checked into the operator's local `.oxi/` (gitignored, per existing pattern).

### Q4 → Resolved: daily proposal cap = 10.

`AutoExternalConfig.daily_proposal_cap: int = 10`. Adopted as written from the research recommendation. Adapter can override, but `oxi-adapter-self` keeps the default for the dogfood loop. Re-evaluate after 30 days if the digest is too noisy or too sparse.

### Q5 → Resolved: acceptance-ratio noise threshold = 0.15 (research recommendation).

`auto_improve_health` auto-pauses the loop when the rolling 14-day acceptance ratio drops below 0.15. Manual unpause via `oxi v3 auto-improve unpause`. The threshold is configurable per adapter but defaults to 0.15.

### Q6 → Resolved: 8-file split inside `auto_external/`.

```text
oxi-core/src/oxi_core/v3/auto_external/
├── __init__.py            # public entry point: run_daily()
├── config.py              # AutoExternalConfig dataclass
├── sources/
│   ├── __init__.py        # Source Protocol
│   ├── github.py          # GitHubSource
│   ├── newsletter.py      # NewsletterSource (AlphaSignal/Latent Space/BensBites)
│   └── x.py               # XSource (calls the X skill, not direct API)
├── rank.py                # BM25 + sqlite-vec + RRF
├── judge.py               # Haiku LLM judge with rubric + fabricated-module filter
├── dedup.py               # Three-layer dedup
├── emit.py                # Ledger events + Markdown digest writer
└── health.py              # Acceptance-ratio tracker + auto-pause (B9)
```

Easier to review per-PR; mypy errors point at a small file; tests can be co-located per module.

### Q7 → Resolved: B11 (GitHub Actions fallback) ships only as contingency if Routines GA slips.

Don't ship B11 preemptively. The risk of two scans/day colliding (one from Routine, one from GHA) outweighs the belt-and-suspenders value. **Trigger to ship B11:** Routines GA misses the cutoff date Pierre sets, OR the Routine fails 3 days in a row without a clear cause. Until then, B11 stays as a documented-but-not-implemented item in the plan; if invoked, Pierre opens it as a single PR.

### Q8 → Resolved: B-series proceeds in parallel with PR #118; B8 rebases if needed.

PR #118 (T0-201/202/203) landing order does not block the B-series. B1-B7, B9, B10 don't touch the `front` ingestion surface. Only **B8** (emit step) writes to `front` — and the emit step is small enough (~60 LOC) that rebasing it onto whatever shape `front` lands at is trivial. If #118 changes `front`'s schema before B8 lands, B8's emit code adapts in the rebase; the rest of the B-series is unaffected.

---

## 9. Anti-patterns to avoid (per oxi PLAN.md §7 + this subsystem)

- **Don't bundle two B entries in one PR.** Each B is independently reviewable.
- **Don't import from `dispatch` / `auto_merge` in `auto_external/`.** Lint-enforced from B1.
- **Don't hardcode Pierre's GitHub orgs / newsletter URLs / X account list.** They live in `AutoExternalConfig`, populated by `oxi_adapter_self`.
- **Don't write to `task` table directly.** Always go through `front`. Operator acceptance via existing `auto_observe.accept()`-shaped flow.
- **Don't bypass `engine_state.is_stopping()` / `budget.check()`.** Same rule as every other v3 loop.
- **Don't add a new DB table when a key-value row in an existing table works** (B9 — confirm before creating `auto_improve_state`).
- **Don't ship B11 unless Routines GA actually slips.**

---

## 10. Rollback plan

Each B entry is a single commit on a single branch with `auto_merge=False`. Rollback per entry: `git revert <sha>` and re-deploy. The whole subsystem is opt-in via `Adapter.auto_improve_config()` returning `None` — adapters that haven't enabled auto-improve are unaffected by every B entry.

If the entire subsystem needs to be quenched mid-day:
1. `oxi v3 auto-improve unpause` followed by **edit** of `auto_improve_state` to set `paused=true` (CLI doesn't have a manual-pause command in v1; operator can also remove the routine schedule).
2. Or: set `AutoExternalConfig(enabled=False)` in `oxi_adapter_self` and reinstall.

---

## 11. Status tracker

Append to this section as B entries ship. Each row links the PR.

| Entry | Status | PR | Merged |
|---|---|---|---|
| B1 | not started | — | — |
| B2 | not started | — | — |
| B3 | not started | — | — |
| B4 | not started (X via skill, per Q1) | — | — |
| B5 | not started | — | — |
| B6 | not started | — | — |
| B7 | not started | — | — |
| B8 | not started | — | — |
| B9 | not started | — | — |
| B10 | not started | — | — |
| B11 | conditional (Routines GA) | — | — |
