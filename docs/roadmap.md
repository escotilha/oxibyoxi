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

## Tier 0 — operational pain from 2026-04-25 dogfood

**T0-104 · file-overlap dispatch gate**
_before each `dispatch_one()` claims a planned task, query `gh pr list --json number,headRefName,files` for currently-open PRs originating from the engine. If the planned task's expected file scope overlaps with any open PR's `files`, defer the dispatch (re-mark as planned with a `last_deferred_at` timestamp; pick a different non-overlapping task). The "expected file scope" is read from the existing `files_touched` field on `task` (per Contably's v4.1 gate pattern). Acceptance: dispatching 5 codex-series tasks (T2-31..T2-37) in parallel produces zero merge conflicts on the resulting PRs; each task waits its turn. Caused by 2026-04-25 cascade where 5 codex-series PRs all opened cleanly then conflicted at merge-time, requiring manual close+reset for 4 of them._

**T0-105 · heartbeat reaper for zombie dispatches**
_currently if a worker dies without writing back (SIGKILL, parent supervisor crash, claude-code timeout-bug #45717), the task row stays `dispatched` forever and blocks the engine's view of in-flight count. Add a heartbeat: `dispatch_one()` writes `last_progress_at` to the task row at start; a new `reaper.py` runs at the head of every tick, finds tasks with `status='dispatched' AND last_progress_at < now() - 30min` AND no live worker process matching the session_id, and resets them to `planned`. Includes a process-existence probe (psutil or `kill -0`) — never reset a task whose worker is genuinely still running. Acceptance: stale `dispatched` rows from a killed supervisor don't accumulate; queue stays accurate. 8 such zombies appeared in the 2026-04-25 PM dogfood, blocking the engine's concurrency view._

## Tier 1 — operator self-host

**T1-19 · adapter loader supports constructor args via TOML config**
_today the entry-point auto-discovery can't load `SelfAdapter(repo_root=...)` because the discovered class needs no-arg construction. This forces operators to write a launcher script that imports + registers manually (e.g., `~/.oxi-tick.py`). Add `oxi.toml` (in repo root) with `[adapter]` section: `class = "oxi_adapter_self.SelfAdapter"` + `kwargs = { repo_root = "." }` (paths resolved relative to the toml location). The adapter discovery code reads this first, falls back to entry-point auto-discovery if absent. Acceptance: `oxi v3 tick --real-claude` works from a fresh shell with no launcher script; launchd plist + systemd units can call `oxi v3 tick` directly._

## Tier 2 — operator polish from 2026-04-25 pain

**T2-43 · CPU-aware concurrency probe**
_RAM probe (`compute_probe.recommend_ram_concurrency()`) recommends N workers based on free RAM, but doesn't consider CPU. On 2026-04-25, the probe recommended 7 workers on a Mac mini that was already at load avg 13 from non-oxi processes (Warp, openclaw-node, etc.) — adding 7 more workers pushed load to 36+. Extend the probe to also read load avg / CPU count and compute `cpu_envelope = max(1, cpu_count - load_avg_5min)`. Final recommendation: `min(ram_envelope, cpu_envelope, plan_tier_envelope, ceiling)`. Acceptance: on a busy host, probe recommends fewer workers than RAM alone would suggest; never spawns past load capacity._

**T2-44 · auto-merge enforcement on critic pass**
_today auto-merge requires both (a) the engine arming `gh pr merge --auto` and (b) GitHub's auto-merge feature being enabled at repo level. Several PRs today required manual `gh pr merge` because step (a) was missed during PR creation. Make critic.py the single point of arming: when the critic returns APPROVE, the same code path that records the verdict also calls `gh pr merge --auto --squash --delete-branch` via `GhCliClient`. Idempotent — already-armed PRs no-op cleanly. Acceptance: every critic-approved PR has auto-merge armed without operator intervention; verified by querying `pull_requests.auto_merge` field on next tick._

**T2-45 · `oxi v3 saturate` continuous-dispatch CLI command**
_today the supervisor pattern is a bash script (`~/.oxi-supervisor.sh`) running in tmux. Make it a first-class oxi subcommand: `oxi v3 saturate --concurrency N --max-cost-per-day USD`. Loops continuously: each iteration calls ingest → auto_observe → seed → dispatch_loop. Tracks daily cost and exits cleanly when the hard cap is hit (logs a `daily_budget_reached` event). Daemon-ready: writes a PID file at `adapter.paths().repo_root + ".oxi/saturate.pid"`; refuses to start if a live PID exists; signal-safe shutdown on SIGTERM (waits for in-flight workers, doesn't kill them). Acceptance: replaces the bash supervisor entirely; can be wrapped by launchd/systemd/tmux with no shim. 10 hours of validated continuous-dispatch on 2026-04-25 makes this the next abstraction to formalize._

**T2-46 · wizard learns about the AgenticAdapter choice**
_today's `oxi init` step 6 asks "Claude plan tier (e.g. standard, max_5x, max_20x)" — accurate while the agentic worker is hardcoded to `claude -p`, but once T2-30 lands the operator can pick a different agentic adapter (Claude, Codex, future Aider). Replace the single plan-tier prompt with a two-stage step: (a) "Agentic worker (claude/codex)" defaulting to `claude`; (b) plan tier prompt only asked if `claude` is selected (Codex has its own pricing model surfaced via `codex_rates.yaml`). Wizard writes the choice into the scaffolded adapter's `agentic_adapter_name() -> str` method, which `dispatch.py` reads via the T2-30 protocol. Doc clarifications from commit f34797c (agentic vs non-agentic split in README + install runbook) become the reference text for the new prompt's help line. Acceptance: a fresh `oxi init` run with `codex` selected scaffolds an adapter that reports `adapter=codex` in `oxi status`; the existing `claude` path stays the default and reads identically to today's wizard. Depends on T2-30, T2-31, T2-34 (the agentic adapter protocol + a working second adapter must exist before the wizard offers the choice)._

**T2-47 · wizard: editable destination + repo-root sanity check**
_the review screen already prints `Destination: {path}` (`wizard._render_review` line 450) but offers no way to change it without aborting the whole wizard. Two compounding paper cuts: (a) destination is `Path.cwd() / f"oxi-adapter-{slug}"`, so running `oxi init` from one directory above your project checkout silently scaffolds the adapter outside the repo (caught during 2026-04-26 dogfood when `oxi init` from `~/code/` produced `~/code/oxi-adapter-oxibyoxy/` while the user assumed it would land inside `~/code/oxibyoxi/`); (b) typos in the slug (`oxibyoxy` vs `oxibyoxi`) bake into the package name on disk and are tedious to fix after the fact. Add a confirmation prompt on the review screen: "Destination [{default}]:" — empty input keeps the default, otherwise the operator types a new absolute path. Validate the path is absolute and writable. Also: if the entered destination is not inside `answers.repo_root` (the value the operator gave for "repo root"), print a single yellow warning line — does not block, just surfaces the mismatch before files are written. Acceptance: pressing Enter on the new prompt preserves today's behavior bit-for-bit (test snapshot); typing a new path writes there instead; outside-repo-root scaffolds emit one warning line. Not urgent — operators who notice immediately can rm -rf and re-run, which is what the 2026-04-26 dogfood did._

---

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

## Tier 2 — multi-model orchestration (#127)

**T2-30 · agentic adapter protocol + ClaudeCodeAdapter shim**
_introduce `oxi_core/v3/agentic/__init__.py` defining the `AgenticAdapter` Protocol over the existing `dispatch_invoke.invoke()` contract; ship `ClaudeCodeAdapter` as a pure passthrough. `dispatch_invoke.py` keeps its public surface and is re-exported through `agentic/claude.py` so call sites (`dispatch.py`, `critic.py`, `tail_dispatch.py`, `cli.py`) change zero lines. Adds the `model_id`, `usage_normalizer` and `tool_translator` hooks the future `CodexCliAdapter` will need, but `ClaudeCodeAdapter` implements them as identity functions for now. New file `tests/test_agentic_contract.py` defines the round-trip contract test that every future adapter must pass against a recorded `DispatchResult` fixture._

**T2-31 · codex adapter skeleton + version pin smoke test**
_create `oxi_core/v3/agentic/codex.py` with a `CodexCliAdapter` class implementing `AgenticAdapter`. The skeleton spawns `codex exec --json` via `asyncio.create_subprocess_exec` (argv-form, never `shell=True`), drains stdout/stderr concurrently with the same 1MB StreamReader limit pattern from `dispatch_invoke.py`, and applies the same env whitelist + process-group isolation. On adapter init, emit a known-good 3-event JSONL fixture through the parser to verify the binary's output format matches; raise `CodexFormatDriftError` with the binary version on mismatch. The codex binary version is read from a new adapter method (returns the operator-pinned semver). No DispatchResult yet — this PR is the spawn shell only._

**T2-32 · codex JSONL event parser + event-type mapping**
_add `oxi_core/v3/agentic/codex_events.py`. Parses the codex `--json` event stream and maps codex event types (`turn.started`, `turn.completed`, `item.completed`, `tool.call.started`, `tool.call.completed`, `error`) to the canonical event types `dispatch_invoke.py` emits (`system`, `assistant`, `tool_use`, `tool_result`, `result`). Pure functions over dicts; no I/O, no subprocess. Round-trip property: replaying a normalized event sequence into `DispatchResult.result_event()` returns a non-None dict for every successful codex run. Exhaustively tested with recorded codex JSONL fixtures (committed under `tests/fixtures/codex/`)._

**T2-33 · codex session-file fallback for reasoning tokens**
_codex `--json` `usage` does not include reasoning tokens; they only appear in `~/.codex/sessions/<id>.json` written after `turn.completed`. Add `oxi_core/v3/agentic/codex_session_file.py` with `read_reasoning_tokens(session_id, sessions_dir) -> int | None`. The function polls (50ms × 20 attempts = 1s budget) for the file to exist after `turn.completed` lands; returns None on timeout (logged as a budget-undercount risk event in the ledger, never silent). The session-file path is configurable via adapter for non-default codex installs. Tests use a temp directory and write fixture session files synchronously; no real codex._

**T2-34 · codex cost calculation + DispatchResult normalizer**
_compose T2-32 + T2-33 into the adapter. `CodexCliAdapter.invoke()` builds the `DispatchResult`: `cost_usd` is computed from token counts × model rate-card (rate card lives in `oxi_core/defaults/codex_rates.yaml`, separate file so it can be updated without code changes); reasoning tokens are merged in from the session file before cost is computed. Classification mapping: codex exit 0 → SUCCESS; exit 130 (SIGINT) → RETRYABLE_TRANSIENT; rate-limit signal in the event stream → RETRYABLE_TRANSIENT with `rate_limit_exhausted=True`; everything else → FAILED. Wall-clock timeout enforcement uses the same `asyncio.wait_for` + `_kill_process_group` pattern as `dispatch_invoke.py`._

**T2-35 · codex shadow-run harness + agreement metric**
_new `oxi_core/v3/agentic/shadow.py`. When the operator sets `OXI_AGENTIC_SHADOW=codex`, every `ClaudeCodeAdapter.invoke()` call also dispatches to `CodexCliAdapter` against a copy of the same prompt, in a sibling worktree. Both `DispatchResult`s are persisted to the ledger as paired `agentic_shadow_observed` events with a `shape_match: bool` and a `cost_delta_usd: float`. **No behavior change** — the shadow result is observed only. Dashboard surfaces an "agentic shadow" panel with the last 50 paired runs, agreement rate, and cost delta. After 14 days of shadow data, operator decides whether T2-39 (promote a task class) is safe._

**T2-36 · attach to mac mini litellm gateway + key provisioning runbook**
_oxi connects to the LiteLLM gateway already running on the operator's Mac Mini (per the local-inference skill setup) instead of standing up its own proxy. This PR adds: (a) `defaults/inference.yaml` declaring the gateway URL (Tailscale-discovered) and the per-role virtual-key names (`oxi-heartbeat`, `oxi-classifier`, `oxi-summary`); (b) a runbook `docs/runbooks/litellm-gateway.md` walking the operator through provisioning a virtual key on the existing gateway, scoping its budget, and rotating it; (c) an adapter method `Adapter.inference_gateway_url()` returning the URL + key-name map; (d) a CI smoke check that hits the configured URL's `/health` (skipped when `OXI_INFERENCE_OFFLINE=1`). Coupling risk: oxi's heartbeat path now depends on the Mac Mini gateway being reachable — if it's down, T2-38's triage step disables itself and `heartbeat.py` falls back to today's no-LLM behavior. Documented in the runbook. Net new ~30 LOC + runbook._

**T2-37 · inference gateway client + non-streaming cost-header lock**
_create `oxi_core/v3/inference/__init__.py` defining `InferenceGateway` with one method: `async complete(messages, model, max_tokens, **kwargs) -> InferenceResult`. `InferenceResult` carries `text`, `cost_usd` (from the `x-litellm-response-cost` response header), `tokens_in`, `tokens_out`, `model`, `latency_ms`. Implementation uses `httpx.AsyncClient` against the LiteLLM proxy URL (configured via adapter). **Hard-coded `stream=False` in the request body** with a unit test that monkeypatches the httpx client and asserts every outbound request body has `stream=False`. A `FakeInferenceGateway` for tests returns canned responses by `(model, prompt_hash)` tuple. No call sites changed in this PR._

**T2-38 · migrate heartbeat reasoning to inference gateway**
_`heartbeat.py` currently has zero LLM calls — but the design calls for a future "stuck task triage" reasoning step that summarizes why a task is stuck before transitioning to `abandoned`. This PR adds that step using `InferenceGateway` (model: routing.yaml-driven, default `claude-haiku-4-5`). The triage summary is recorded on the `abandoned_by_heartbeat` ledger event in a new `triage_summary` field. Behind a feature flag in the adapter (`heartbeat.triage_enabled`, default False); when disabled, `heartbeat.py` behavior is byte-identical to today. First non-agentic call site, validates the gateway works in production. Fakes-not-mocks: tests pass `FakeInferenceGateway` through the heartbeat reap call._

**T2-39 · routing.py + defaults/routing.yaml**
_new `oxi_core/v3/routing.py` with one pure function: `route_for(role: str, task: dict | None = None) -> ModelChoice`. `ModelChoice` is a frozen dataclass with `(adapter_name: str, model_id: str, fallback_chain: tuple[str, ...])`. Reads from `oxi_core/defaults/routing.yaml` — schema is documented in the YAML comment header; loaded once and cached. YAML keys are roles (`worker`, `critic`, `heartbeat-triage`, `prompt-injection-screen`); values name the adapter + model + a fallback chain. No env-var overrides yet (deferred to T2-40 if it's ever needed). Tests cover: known role → expected adapter; unknown role → `RoleNotConfiguredError`; YAML missing → clear error pointing at the file path. **No call sites changed in this PR** — wiring `dispatch.py` to consult `route_for` is the next entry._

**T2-40 · promote one trivial task class to codex via routing**
_wire `route_for("worker", task)` into `dispatch.py`'s model-selection path (today inlined as `_pick_model`). For the initial promotion: the routing.yaml entry for the `worker` role with `task.tier == 2` and `task.title contains "doc"` (matching tasks like T2-16 doc-lint, T3-1 doc ingester) returns `(adapter="codex", model="codex-mini", fallback=["claude-haiku-4-5"])`. All other tasks continue to route to `(adapter="claude", model="claude-sonnet-4-5", ...)`. `_pick_model` becomes a thin wrapper that calls `route_for` and unpacks `ModelChoice.model_id`; `dispatch.py` now reads `ModelChoice.adapter_name` and selects which `AgenticAdapter` instance to invoke. Acceptance: a doc-tier-2 task dogfood-dispatches against Codex; `auto_merge` succeeds; the brief shows `adapter=codex` for that task. Per anti-pattern #1, this PR ships only the doc-tier promotion. Subsequent task-class promotions are separate PRs._

**T2-41 · xAI Grok as InferenceGateway target (via LiteLLM)**
_extend `oxi_core/defaults/inference.yaml` with an `xai` provider block and add the model rate-card entries for `grok-2`, `grok-4-fast`, and `grok-code-fast-1` to `oxi_core/defaults/codex_rates.yaml` (rename to `inference_rates.yaml` if T2-37 lands first). Provision a virtual key on the operator's existing LiteLLM gateway pointing at `https://api.x.ai/v1`. Add a `routing.yaml` recipe candidate so the operator can swap heartbeat-triage / classifier / summary roles to Grok via a one-line config change without redeploy. No agentic adapter — Grok is reachable only through the non-streaming `InferenceGateway` path (T2-37). Acceptance: a heartbeat triage call routes to grok via `route_for("heartbeat-triage")`, the cost lands on the ledger from `x-litellm-response-cost`, and a CI smoke test asserts the gateway proxies cleanly. Depends on T2-37._

**T2-42 · OpenRouter as InferenceGateway target (via LiteLLM)**
_same shape as T2-41 but for OpenRouter — adds an `openrouter` provider block and rate-card entries for the OpenRouter models the operator wants to be able to route to (Qwen 3.6 Plus is the current preview-free anchor; add Llama 3.3, DeepSeek V3, Mistral Large via OpenRouter as additional optional entries). Critical OpenRouter quirk — the API returns cost in `usage.total_cost` rather than the LiteLLM-standard `x-litellm-response-cost` header, so the rate-card lookup must be deterministic per model and tested against recorded fixtures. Acceptance: heartbeat-triage call routes to an OpenRouter model via `route_for("heartbeat-triage", task=None)`, cost ledger entry is within 5% of the OpenRouter dashboard for the same call. Depends on T2-37._

## Tier 1 — auto-improve subsystem (#128)

**T1-B1 · `auto_external` skeleton + adapter Protocol method + CLI subcommand stubs**
_create the empty package; add `Adapter.auto_improve_config()`; add `AutoExternalConfig` dataclass; stub `oxi v3 auto-improve {scan,unpause,status}` subcommands that print "not implemented" and exit 0; add the `LedgerEvent` constants from §2; extend `scripts/lint-for-leaks.sh` with the forbidden-imports gate. All fakes/fixtures land here so subsequent PRs are tiny._

**T1-B2 · GitHub source fetcher**
_implements `GitHubSource` against pinned-org release feeds (8) and topic queries (5). Star-velocity prefilter: drop repos < 5 stars/day in the last 30 days. Commit-activity prefilter: drop repos with no commits in the last 14 days. Reuses `oxi_core.v3.github_client.GitHubClient` Protocol; tests use `FakeGitHubClient` from `tests/fixtures/fake_github.py`. Per-source try/except — failure emits `AUTO_IMPROVE_SOURCE_FAILED` and other sources continue._

**T1-B3 · Newsletter source fetcher**
_implements `NewsletterSource` against AlphaSignal, Latent Space, BensBites public archives. HTML scrape via `httpx` + `selectolax` (already a dep via `pr_watcher`? — confirm). Per-source try/except. New `FakeHTTPFetcher` in `tests/fixtures/fake_http.py` returns canned HTML fixtures from `tests/fixtures/data/newsletters/`. Each newsletter gets its own parser function so a layout change to one doesn't take down all three._

**T1-B4 · X source fetcher (via X skill)**
_implements `XSource` as a subprocess wrapper around the operator's X skill (per Q1). Reads ~15-account curated list from `AutoExternalConfig.x_account_list`; calls `config.x_skill_binary` with the list and a since-timestamp; parses the skill's stdout as a list of post records. Disabled when `config.x_skill_binary is None` — returns `[]` without subprocess call (asserted in test). New `FakeXSkill` fixture writes canned stdout. If subprocess returns non-zero or stdout fails to parse → `AUTO_IMPROVE_SOURCE_FAILED` with the exit code in payload, no retry within the same scan. Acceptance: when binary is `None`, no subprocess; when binary is set, subprocess runs with the configured arg shape; parse failures emit the right ledger event._

**T1-B5 · Ranking pipeline (no LLM yet)**
_implements `prefilter`, `bm25_score`, `vector_rerank`, `rrf_combine` in `ranking.py`. SQLite FTS5 virtual table built on the fly from roadmap.md + CHANGELOG.md + last-90-day merged PRs (queried via `GitHubClient.list_merged_prs(since=now-90d)`). sqlite-vec extension loaded; embeddings via `all-MiniLM-L6-v2` sentence-transformer (already a dep — confirm in `pyproject.toml`). RRF k=60 hardcoded (matches user's memory rule for hybrid retrieval). Tests use a fixed 50-item corpus and assert deterministic top-15 ordering._

**T1-B6 · LLM judge with rubric + fabricated-module hard filter**
_implements `HaikuJudge` in `judge.py`. Reads current module list via `pkgutil.iter_modules(oxi_core.v3.__path__)`. Structured output schema: `{relevance: 1-5, concreteness: 1-5, suggested_tier: 0|1|2, duplicate: bool, fabricated_module: bool, rationale: str}`. Hard-rejects when `fabricated_module=true`, emits `EXTERNAL_PROPOSAL_REJECTED_FABRICATED`. Calls `budget.check()` before every invocation; honors internal $5/day cap (separate ledger query against today's `EXTERNAL_PROPOSAL_*` cost-tagged events). Fake judge: `FakeJudge` in `tests/fixtures/fake_judge.py` with deterministic verdicts keyed by candidate ID._

**T1-B7 · Three-layer dedup**
_implements `dedup_identifier`, `dedup_semantic`, `dedup_temporal` in `dedup.py`. Identifier dedup: query `EXTERNAL_PROPOSAL_EMITTED` events for the next monotonic counter. Semantic dedup: cosine similarity ≥ 0.85 against open `front` rows + tasks updated in last 30 days. Temporal dedup: same `(signal_kind, target_identifier)` within 14 days → skip and emit `EXTERNAL_PROPOSAL_DEDUP_SKIPPED`. Tests cover each layer with fixed embeddings + ledger fixtures._

**T1-B8 · Emit step — ledger events + Markdown digest writer**
_implements `emit_proposal` in `emit.py`. Writes one `EXTERNAL_PROPOSAL_EMITTED` event per accepted proposal, inserts a row into `front` with `task_id=NULL` and the chosen `T<tier>-A<n>` identifier (or stages it pending operator accept — match `auto_observe.accept()` shape exactly). Writes Markdown digest at `.oxi/auto-improve-digest-YYYY-MM-DD.md` with sections: Top proposals, Skipped (dedup), Skipped (fabricated), Source failures, Budget held. File path uses `adapter.paths().repo_root` — never hardcoded._

**T1-B9 · `auto_improve_health` — acceptance-ratio tracker + auto-pause**
_implements `health.py`. On every scan, computes `accepted / emitted` over the last 14 days from ledger events. If ratio < `acceptance_ratio_threshold` (default 0.15) for two consecutive 7-day windows: emit `AUTO_IMPROVE_NOISE_ALERT`, write a `paused: true` row in a new `auto_improve_state` table (or reuse `engine_state` table — confirm in implementation), and emit `AUTO_IMPROVE_PAUSED`. Manual unpause via `oxi v3 auto-improve unpause` (CLI subcommand wired in B1; the actual unpause logic lands here). Auto-pause is **per-loop** — does not affect the engine killswitch._

**T1-B10 · Claude Code Routine config + entry-point script**
_adds `scripts/auto_improve_routine.py` (the entry point Routines invokes) and `routines/auto-improve.toml` (or whatever shape the Routines schema settles on at GA). Credentials via Anthropic Managed Agent Vaults — no token in the routine config or env file. The script: opens the SQLite DB, builds `EngineState`, calls `auto_external.scan()`, exits. Idempotent: if a scan already ran today, `EXTERNAL_PROPOSAL_EMITTED` events for today exist → skip and log. Smoke test: a CI job runs the script against a fixture DB and asserts the digest file is written._

**T1-B11 · GitHub Actions schedule fallback + watchdog (ships only if Routines GA slips)**
_adds `.github/workflows/auto-improve.yml` running on cron `0 5 * * *`. Calls `scripts/auto_improve_routine.py`. Watchdog: if no `EXTERNAL_PROPOSAL_EMITTED` event in the last 36h, the next run emits a `auto_improve_watchdog_stalled` event and notifies via the existing `notification.py` backend. Don't ship this unless Routines is actually delayed past B10's merge date. Track Routines GA in `docs/origin-feature-gap-2026-04-24.md` and decide at B10 acceptance time._

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
