# Multi-Model Orchestration — Implementation Plan

**Repo:** `escotilha/oxi`
**Status:** Plan only. Roadmap entries below land as one PR; engine ships them sequentially.
**Authored:** 2026-04-25
**Owner:** Pierre (operator)
**Source decisions:** the architecture is settled (AgenticAdapter Protocol, InferenceGateway via self-hosted LiteLLM, pure routing function, ClaudeCriticBackend stays default). This document does **not** redesign — it sequences shippable PRs.

---

## 1. Architecture summary (from settled research)

Three boundaries are introduced into `oxi-core/src/oxi_core/v3/`:

1. **`agentic/__init__.py`** — `AgenticAdapter` Protocol over the existing `dispatch_invoke.invoke()` contract. Two concrete adapters: `ClaudeCodeAdapter` (today's behavior) and `CodexCliAdapter` (wraps `codex exec --json`). Both normalize to `DispatchResult`.
2. **`inference/__init__.py`** — `InferenceGateway` for non-agentic LLM calls (heartbeat reasoning, ledger summaries, prompt-injection screening). Backend: self-hosted LiteLLM proxy. Per-call cost from `x-litellm-response-cost` response header. **Non-streaming only** — LiteLLM bug #12689 omits the cost header on streamed responses.
3. **`routing.py`** — pure function `route_for(role, task) → ModelChoice`, driven by `oxi-core/src/oxi_core/defaults/routing.yaml`. No DSPy. No RouteLLM.

Verifier strategy: keep `ClaudeCriticBackend` as default. Add `CodexCriticBackend` shadow only — measure agreement, do not switch.

---

## 2. Hard constraints

These are non-negotiable. Every roadmap entry below honors them.

| Constraint | Why | Where enforced |
|---|---|---|
| `DispatchResult` shape is sacred | Every adapter must normalize to the same dataclass; downstream code (`auto_merge`, `pr_watcher`, `tail_dispatch`) reads its fields by name. | Adapter normalizer + contract test (`test_agentic_contract.py`). |
| Codex CLI does not return reasoning tokens via `--json` | Cost calculation will undercount silently if not addressed. | `CodexCliAdapter` reads `~/.codex/sessions/<id>.json` after `turn.completed` and merges reasoning-token counts into the `result` event before normalizing. Hard requirement; not optional. |
| LiteLLM streaming bug #12689 | `x-litellm-response-cost` header is absent on `stream=True` responses; cost falls back to provider markup which under-counts ~15%. | `InferenceGateway.complete()` sets `stream=False` always. Unit test asserts `stream=False` in every code path. |
| Codex CLI rapid-release (no deprecation policy) | Format drift will break the JSON parser silently. | `CodexCliAdapter` pins binary version (configured via adapter), runs a smoke test on init that emits a known-good JSONL sequence and parses it. |
| Tool-schema drift between Claude/Codex (15% silent failure baseline) | Tools defined in oxi's flavor get dropped/renamed/reordered when handed to Codex's tool-call format. | MCP is canonical on the oxi side. Each adapter translates oxi-MCP → provider flavor. Tool-call success rate is a first-class metric on the dashboard. |
| Behavioral contracts (PR #118) attach at the AgenticAdapter boundary | `T0-201/202/203` introduce `ContractSpec` keyed by `(role, model)`. Adapter must surface the model name in the invocation, not just the prompt. | `DispatchInvocation` already carries `model` and `allowed_tools` — sufficient. Contract registry is consulted in `dispatch.py` before adapter call, and the violation events fire on the `DispatchResult` after. |

---

## 3. Phased delivery — sized for one ingestible PR per entry

Each entry below is shaped for the oxi roadmap parser:

```text
**T<tier>-<n> · <title>**
_<italic subtitle>_
```

Identifiers chosen to **not collide** with shipped (T0-1..T0-103, T1-3..T1-18, T2-8..T2-16, T3-1..T3-2) or in-flight (T0-201/202/203 from PR #118). I reserve the **T2-30 series** for this multi-model migration so it's grep-able as one cohort.

Phase numbering below is for human reading; the engine sees only `T2-30..T2-39`.

### Phase 1 — `AgenticAdapter` Protocol + Claude shim (one PR, ~80 LOC net new)

**T2-30 · agentic adapter protocol + ClaudeCodeAdapter shim**
_introduce `oxi_core/v3/agentic/__init__.py` defining the `AgenticAdapter` Protocol over the existing `dispatch_invoke.invoke()` contract; ship `ClaudeCodeAdapter` as a pure passthrough. `dispatch_invoke.py` keeps its public surface and is re-exported through `agentic/claude.py` so call sites (`dispatch.py`, `critic.py`, `tail_dispatch.py`, `cli.py`) change zero lines. Adds the `model_id`, `usage_normalizer` and `tool_translator` hooks the future `CodexCliAdapter` will need, but `ClaudeCodeAdapter` implements them as identity functions for now. New file `tests/test_agentic_contract.py` defines the round-trip contract test that every future adapter must pass against a recorded `DispatchResult` fixture._

### Phase 2 — `CodexCliAdapter` (5 PRs, ~250 LOC total)

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

### Phase 3 — Self-hosted LiteLLM proxy + InferenceGateway (3 PRs, ~150 LOC total)

**T2-36 · attach to mac mini litellm gateway + key provisioning runbook**
_oxi connects to the LiteLLM gateway already running on the operator's Mac Mini (per the local-inference skill setup) instead of standing up its own proxy. This PR adds: (a) `defaults/inference.yaml` declaring the gateway URL (Tailscale-discovered) and the per-role virtual-key names (`oxi-heartbeat`, `oxi-classifier`, `oxi-summary`); (b) a runbook `docs/runbooks/litellm-gateway.md` walking the operator through provisioning a virtual key on the existing gateway, scoping its budget, and rotating it; (c) an adapter method `Adapter.inference_gateway_url()` returning the URL + key-name map; (d) a CI smoke check that hits the configured URL's `/health` (skipped when `OXI_INFERENCE_OFFLINE=1`). Coupling risk: oxi's heartbeat path now depends on the Mac Mini gateway being reachable — if it's down, T2-38's triage step disables itself and `heartbeat.py` falls back to today's no-LLM behavior. Documented in the runbook. Net new ~30 LOC + runbook._

**T2-37 · inference gateway client + non-streaming cost-header lock**
_create `oxi_core/v3/inference/__init__.py` defining `InferenceGateway` with one method: `async complete(messages, model, max_tokens, **kwargs) -> InferenceResult`. `InferenceResult` carries `text`, `cost_usd` (from the `x-litellm-response-cost` response header), `tokens_in`, `tokens_out`, `model`, `latency_ms`. Implementation uses `httpx.AsyncClient` against the LiteLLM proxy URL (configured via adapter). **Hard-coded `stream=False` in the request body** with a unit test that monkeypatches the httpx client and asserts every outbound request body has `stream=False`. A `FakeInferenceGateway` for tests returns canned responses by `(model, prompt_hash)` tuple. No call sites changed in this PR._

**T2-38 · migrate heartbeat reasoning to inference gateway**
_`heartbeat.py` currently has zero LLM calls — but the design calls for a future "stuck task triage" reasoning step that summarizes why a task is stuck before transitioning to `abandoned`. This PR adds that step using `InferenceGateway` (model: routing.yaml-driven, default `claude-haiku-4-5`). The triage summary is recorded on the `abandoned_by_heartbeat` ledger event in a new `triage_summary` field. Behind a feature flag in the adapter (`heartbeat.triage_enabled`, default False); when disabled, `heartbeat.py` behavior is byte-identical to today. First non-agentic call site, validates the gateway works in production. Fakes-not-mocks: tests pass `FakeInferenceGateway` through the heartbeat reap call._

### Phase 4 — Routing function + first promotion (2 PRs, ~100 LOC total)

**T2-39 · routing.py + defaults/routing.yaml**
_new `oxi_core/v3/routing.py` with one pure function: `route_for(role: str, task: dict | None = None) -> ModelChoice`. `ModelChoice` is a frozen dataclass with `(adapter_name: str, model_id: str, fallback_chain: tuple[str, ...])`. Reads from `oxi_core/defaults/routing.yaml` — schema is documented in the YAML comment header; loaded once and cached. YAML keys are roles (`worker`, `critic`, `heartbeat-triage`, `prompt-injection-screen`); values name the adapter + model + a fallback chain. No env-var overrides yet (deferred to T2-40 if it's ever needed). Tests cover: known role → expected adapter; unknown role → `RoleNotConfiguredError`; YAML missing → clear error pointing at the file path. **No call sites changed in this PR** — wiring `dispatch.py` to consult `route_for` is the next entry._

**T2-40 · promote one trivial task class to codex via routing**
_wire `route_for("worker", task)` into `dispatch.py`'s model-selection path (today inlined as `_pick_model`). For the initial promotion: the routing.yaml entry for the `worker` role with `task.tier == 2` and `task.title contains "doc"` (matching tasks like T2-16 doc-lint, T3-1 doc ingester) returns `(adapter="codex", model="codex-mini", fallback=["claude-haiku-4-5"])`. All other tasks continue to route to `(adapter="claude", model="claude-sonnet-4-5", ...)`. `_pick_model` becomes a thin wrapper that calls `route_for` and unpacks `ModelChoice.model_id`; `dispatch.py` now reads `ModelChoice.adapter_name` and selects which `AgenticAdapter` instance to invoke. Acceptance: a doc-tier-2 task dogfood-dispatches against Codex; `auto_merge` succeeds; the brief shows `adapter=codex` for that task. Per anti-pattern #1, this PR ships only the doc-tier promotion. Subsequent task-class promotions are separate PRs._

### Phase 5 (deferred — out of scope)

Local agentic models (Mac Mini MLX, VPS) are explicitly out of scope for the initial migration. The `routing.yaml` schema reserves a `local` adapter slot but no adapter is built. Re-evaluate after T2-40 has 14 days of clean dogfood.

---

## 4. File-by-file change list

For each entry, **C** = create, **E** = edit, **R** = rename-or-shim, **T** = test only.

### T2-30 — agentic protocol + Claude shim

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/agentic/__init__.py` | C | Defines `AgenticAdapter` Protocol; re-exports `DispatchInvocation`, `DispatchResult`, `Classification` from `dispatch_invoke` for one canonical import path. |
| `oxi-core/src/oxi_core/v3/agentic/claude.py` | C | `ClaudeCodeAdapter` class. Methods: `invoke`, `model_id`, `usage_normalizer`, `tool_translator`. Internally delegates `invoke` to `dispatch_invoke.invoke`. |
| `oxi-core/src/oxi_core/v3/dispatch_invoke.py` | E | No code changes. Add a module-level docstring note that this is the canonical Claude implementation; `ClaudeCodeAdapter` wraps it. **Not renamed.** **Not shimmed away.** Keeping it where it is preserves git blame and the existing 30+ test references. |
| `oxi-core/src/oxi_core/v3/dispatch.py` | E | Zero-line behavioral change: imports `DispatchInvocation` / `DispatchResult` continue to come from `dispatch_invoke`. A future PR could re-route imports through `agentic`, but doing it now is anti-pattern #1 (mixing rename with feature). |
| `oxi-core/tests/test_agentic_contract.py` | T | Round-trip test: hand a recorded successful `DispatchResult` fixture (captured from a real claude run) to a `ClaudeCodeAdapter`, verify the adapter's `invoke` returns the same shape. This test is what every future adapter must pass. |
| `oxi-core/tests/fixtures/agentic/claude_success.json` | T | Recorded successful `DispatchResult` (events + result + cost), used by the contract test. |

### T2-31 — codex adapter skeleton

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/agentic/codex.py` | C | `CodexCliAdapter` class. Spawn shell only; raises `NotImplementedError` from `invoke()`. Implements `model_id`, smoke-test on init. |
| `oxi-core/src/oxi_core/v3/agentic/_subprocess.py` | C | Extract the spawn helpers (env whitelist, process-group kill, stream drain, 1MB limit) from `dispatch_invoke.py` into a shared module so both Claude and Codex adapters use them. **No behavior change** for `dispatch_invoke.py`; it imports the helpers from the new module. This is a pure refactor and is the single concession to anti-pattern #1 — justified because both adapters need the exact same subprocess discipline. |
| `oxi-core/src/oxi_core/v3/dispatch_invoke.py` | E | Replace inline spawn helpers with imports from `_subprocess.py`. All existing tests must pass byte-identically. |
| `oxi-core/tests/test_codex_adapter_init.py` | T | Verifies smoke test fires on init; verifies version mismatch raises `CodexFormatDriftError`. |
| `oxi-core/tests/test_subprocess_helpers.py` | T | Covers the extracted helpers; replaces equivalent assertions in `test_dispatch_invoke.py`. |

### T2-32 — codex event parser

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/agentic/codex_events.py` | C | Pure event-mapping functions. Maps codex event types to `system` / `assistant` / `tool_use` / `tool_result` / `result`. |
| `oxi-core/tests/fixtures/codex/turn_completed.jsonl` | T | Recorded codex JSONL for a completed turn. |
| `oxi-core/tests/fixtures/codex/tool_call_sequence.jsonl` | T | Recorded codex JSONL for a tool-use sequence. |
| `oxi-core/tests/fixtures/codex/error_path.jsonl` | T | Recorded codex JSONL for a failed turn. |
| `oxi-core/tests/test_codex_events.py` | T | Round-trip property test against all three fixtures. |

### T2-33 — codex session-file fallback

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/agentic/codex_session_file.py` | C | `read_reasoning_tokens` + `wait_for_session_file` helpers. |
| `oxi-core/tests/test_codex_session_file.py` | T | Covers happy path (file written before timeout), timeout path (returns None + emits ledger event), corrupt JSON path. Uses `tmp_path` fixture. |
| `oxi-core/src/oxi_core/v3/ledger_events.py` | E | Add `CODEX_REASONING_TOKENS_UNAVAILABLE` event constant. (Coordinates with T1-14's typed-event-kinds work.) |

### T2-34 — codex cost + normalizer

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/agentic/codex.py` | E | `invoke()` now fully implemented. Composes the spawn helpers, event parser, session-file reader, and cost calculator. Returns `DispatchResult`. |
| `oxi-core/src/oxi_core/defaults/codex_rates.yaml` | C | Per-model rate card: input $/Mtoken, cached input $/Mtoken, output $/Mtoken, reasoning $/Mtoken. Operator updates this when codex changes pricing. |
| `oxi-core/src/oxi_core/v3/agentic/codex_cost.py` | C | Pure cost-calc function reading `codex_rates.yaml`. |
| `oxi-core/tests/test_codex_invoke.py` | T | End-to-end adapter test using a fake codex binary (bash script that emits the recorded JSONL fixtures + writes a fixture session file). Verifies `DispatchResult` round-trip against the contract test from T2-30. |
| `oxi-core/tests/test_codex_cost.py` | T | Pure-function tests against rate-card edge cases. |

### T2-35 — shadow-run harness

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/agentic/shadow.py` | C | `ShadowDispatcher` wraps two adapters; emits paired ledger events. |
| `oxi-core/src/oxi_core/v3/dispatch.py` | E | When `OXI_AGENTIC_SHADOW=codex`, route through `ShadowDispatcher`. Default off — zero behavior change. |
| `oxi-core/src/oxi_core/v3/dashboard.py` | E | New "Agentic Shadow" panel reading the paired events. |
| `oxi-core/src/oxi_core/v3/dashboard_html.py` | E | HTML for the panel. |
| `oxi-core/src/oxi_core/v3/ledger_events.py` | E | Add `AGENTIC_SHADOW_OBSERVED` constant. |
| `oxi-core/tests/test_shadow.py` | T | Verifies shape-match comparison; verifies cost-delta math; verifies env-var gate. |

### T2-36 — Mac Mini gateway attach (Q3 resolution)

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/defaults/inference.yaml` | C | Gateway URL + per-role virtual-key names. URL defaults to the Tailscale name; operator overrides via env var if needed. No secrets — virtual-key *names* only; the keys themselves live on the gateway. |
| `oxi-core/src/oxi_core/adapter.py` | E | Add `inference_gateway_url() -> InferenceGatewayConfig \| None` (default `None`, meaning "no gateway, inference paths disabled"). `oxi-adapter-self` overrides to read from `inference.yaml`. |
| `docs/runbooks/litellm-gateway.md` | C | How to provision a virtual key on the existing Mac Mini gateway, scope its budget, rotate it; how to discover the gateway URL via Tailscale; what happens when the gateway is unreachable (heartbeat triage disables itself). |
| `.github/workflows/inference-smoke.yml` | C | CI job: skip when `OXI_INFERENCE_OFFLINE=1` (default in CI). Locally and in the dogfood Routine: hit configured `/health`, assert 200. |
| `scripts/lint-for-leaks.sh` | E | Add `inference.yaml` to scanned set; ensure no literal API keys land there. |

### T2-37 — inference gateway

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/inference/__init__.py` | C | `InferenceGateway`, `InferenceResult`, `FakeInferenceGateway`. |
| `oxi-core/tests/test_inference_gateway.py` | T | Asserts `stream=False` on every outbound request body. Asserts cost extracted from `x-litellm-response-cost` header. Covers 4xx and 5xx error paths. |

### T2-38 — heartbeat triage

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/heartbeat.py` | E | Optional triage step on `abandoned_by_heartbeat` events. Behind adapter flag. |
| `oxi-core/src/oxi_core/adapter.py` | E | New adapter method `heartbeat_triage_enabled() -> bool` with default `False`. Default placement justified — this is opt-in polish, not the plan-tier class of decision that anti-pattern #3 governs. |
| `oxi-core/tests/test_heartbeat.py` | E | Add a test case with `FakeInferenceGateway` returning a canned summary. Default-flag-off case stays byte-identical to today. |

### T2-39 — routing.py

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/routing.py` | C | Pure `route_for` function, `ModelChoice` dataclass, `RoleNotConfiguredError`. |
| `oxi-core/src/oxi_core/defaults/__init__.py` | C | Empty package marker. |
| `oxi-core/src/oxi_core/defaults/routing.yaml` | C | Initial roles: `worker` → `(claude, claude-sonnet-4-5, [claude-haiku-4-5])`; `critic` → `(claude, claude-sonnet-4-5, [])`; `heartbeat-triage` → `(claude, claude-haiku-4-5, [])`; `prompt-injection-screen` → reserved, no entry. |
| `oxi-core/tests/test_routing.py` | T | Covers known/unknown role; YAML-missing error path; cache invalidation if test changes the file. |

### T2-40 — first promotion

| File | Op | Notes |
|---|---|---|
| `oxi-core/src/oxi_core/v3/dispatch.py` | E | `_pick_model` becomes a wrapper that calls `route_for("worker", task)` and reads `ModelChoice.model_id`. New `_pick_adapter` reads `ModelChoice.adapter_name` and returns the right `AgenticAdapter` instance. |
| `oxi-core/src/oxi_core/defaults/routing.yaml` | E | Add a conditional rule for `task.tier == 2 AND title contains "doc"` → codex. |
| `oxi-core/src/oxi_core/v3/routing.py` | E | `route_for` signature gains `task: dict | None = None`; rule evaluation supports the simple matchers needed (`tier`, `title contains`). No general-purpose query language — keep it tiny. |
| `oxi-core/tests/test_routing_with_task.py` | T | Covers the new conditional rule + its negation. |
| `oxi-core/tests/test_dispatch_adapter_selection.py` | T | Verifies dispatch picks the right adapter instance for a doc-tier-2 task vs. an arbitrary tier-1 task. |
| `oxi-core/src/oxi_core/v3/brief.py` | E | Surface `adapter=codex` (vs. `adapter=claude`) in the daily brief. |
| `oxi-core/src/oxi_core/v3/dashboard.py` | E | Show adapter name per task row. |

---

## 5. Test plan per entry

The repo's convention (`FakeGitHubClient`, `FunctionCriticBackend`, `AlwaysApproveBackend`) is **fakes-not-mocks**. Every test below follows that pattern. No `unittest.mock.patch` of the actual subprocess or HTTP client unless the alternative is materially worse.

### T2-30 acceptance test plan

- **`test_agentic_contract.py::test_claude_adapter_passes_contract`** — golden DispatchResult fixture, ClaudeCodeAdapter.invoke returns identical structure.
- **`test_agentic_contract.py::test_protocol_minimum_methods`** — `isinstance(ClaudeCodeAdapter(), AgenticAdapter)` returns True; `runtime_checkable` Protocol.
- All existing `test_dispatch_invoke.py` tests pass unchanged (regression gate).
- All existing `test_dispatch.py`, `test_critic.py`, `test_tail_dispatch.py` tests pass unchanged.

### T2-31 acceptance test plan

- **`test_subprocess_helpers.py`** — full coverage of extracted helpers (env whitelist, pgid kill, drain).
- **`test_codex_adapter_init.py::test_smoke_passes_on_known_version`** — fake codex binary writes the canonical fixture; init succeeds.
- **`test_codex_adapter_init.py::test_smoke_fails_on_unknown_event_type`** — fake codex binary writes an unknown event type; init raises `CodexFormatDriftError` with the version + the unknown type name.
- All `test_dispatch_invoke.py` tests still pass (helpers still wired in).

### T2-32 acceptance test plan

- **`test_codex_events.py::test_round_trip_<fixture>`** — one parametrized test per JSONL fixture; verifies the parsed events have the right canonical types.
- **`test_codex_events.py::test_partial_trailing_line_tolerated`** — last line is half-written; parser swallows it like `dispatch_invoke` does.
- **`test_codex_events.py::test_unknown_event_type_passes_through`** — unknown events get a synthetic `system` mapping with the original payload preserved (forward-compat for codex adding new events).

### T2-33 acceptance test plan

- **`test_codex_session_file.py::test_returns_reasoning_tokens_when_present`** — write a fixture file synchronously; helper returns the count.
- **`test_codex_session_file.py::test_returns_none_on_timeout`** — file never appears; helper returns None and emits the ledger event.
- **`test_codex_session_file.py::test_corrupt_json_returns_none`** — corrupt file; helper returns None, ledger event has `reason: corrupt_session_file`.

### T2-34 acceptance test plan

- **`test_codex_invoke.py::test_full_invoke_normalizes_to_dispatch_result`** — composes everything; runs against a bash-script fake codex; result passes the agentic contract test.
- **`test_codex_invoke.py::test_classification_mapping`** — parametrized over (exit_code, expected Classification): 0→SUCCESS, 130→RETRYABLE_TRANSIENT, 1→FAILED, timeout→TIMEOUT.
- **`test_codex_cost.py::test_cost_includes_reasoning_tokens`** — verifies the rate-card math; reasoning tokens are folded in.
- **`test_codex_cost.py::test_cost_under_counts_when_session_file_missing`** — explicitly verifies the failure mode AND that the ledger event records the under-count.

### T2-35 acceptance test plan

- **`test_shadow.py::test_paired_events_emitted`** — env var on, both adapters fakes; verify two `agentic_shadow_observed` events with matching shapes.
- **`test_shadow.py::test_env_var_off_no_shadow`** — default state; only one event, no shadow events.
- **`test_shadow.py::test_shape_mismatch_reported`** — fake codex returns a malformed result; `shape_match=False` in the event payload.

### T2-36 acceptance test plan

- **CI job `inference-smoke.yml`** — skipped under `OXI_INFERENCE_OFFLINE=1` (default in CI), green when the operator runs it locally against the live Mac Mini gateway.
- **`test_adapter_inference_gateway_default.py`** — adapters that don't override `inference_gateway_url()` return `None`; the InferenceGateway client accepts `None` and disables inference paths cleanly (no exceptions, just feature-flag-off semantics).
- **Runbook reproducibility test** (manual, documented in the runbook): operator follows the runbook on a clean machine; provisions a virtual key, hits healthcheck, reaches a passing state in <10 min.
- No proxy-spin-up tests — the gateway lives outside oxi.

### T2-37 acceptance test plan

- **`test_inference_gateway.py::test_stream_false_locked`** — every outbound request body has `stream=False`. Locked under three call paths (default, with kwargs, with system prompt).
- **`test_inference_gateway.py::test_cost_extracted_from_header`** — fake httpx response with the cost header; `InferenceResult.cost_usd` matches.
- **`test_inference_gateway.py::test_4xx_raises_typed_error`** — 401 → `InferenceAuthError`; 429 → `InferenceRateLimitError`; 5xx → `InferenceServiceError`. Mapped explicitly.
- **`test_fake_inference_gateway.py::test_canned_response_by_prompt_hash`** — verifies the test fake works for the rest of the codebase.

### T2-38 acceptance test plan

- **`test_heartbeat.py`** — existing tests pass unchanged with flag default-off.
- **`test_heartbeat.py::test_triage_summary_attached_when_enabled`** — flag on, FakeInferenceGateway returns a canned summary; ledger event includes the `triage_summary` field.
- **`test_heartbeat.py::test_triage_failure_does_not_block_abandon`** — gateway raises; the abandon transition still happens, with `triage_summary: null` and `triage_error: <type>` in the event payload.

### T2-39 acceptance test plan

- **`test_routing.py::test_known_role_returns_model_choice`** — parametrized over the four seeded roles.
- **`test_routing.py::test_unknown_role_raises`** — unknown role → `RoleNotConfiguredError` with the role name and the YAML path in the message.
- **`test_routing.py::test_yaml_missing_raises`** — file missing → clear error with the expected path.
- **`test_routing.py::test_pure_function_no_io_after_first_call`** — second call doesn't re-read the YAML (cached).

### T2-40 acceptance test plan

- **`test_routing_with_task.py::test_doc_tier_2_routes_to_codex`** — task `{tier: 2, title: "T2-16 doc lint"}` returns codex `ModelChoice`.
- **`test_routing_with_task.py::test_non_doc_tier_2_routes_to_claude`** — task `{tier: 2, title: "T2-9 fspath at boundaries"}` returns claude.
- **`test_dispatch_adapter_selection.py::test_doc_task_picks_codex_adapter`** — dispatch unit-test with both adapters as fakes; verifies the codex fake is the one invoked.
- **End-to-end smoke** (`scripts/smoke/end-to-end.sh`): seeds a doc-tier-2 task, runs 3 ticks, asserts the brief shows `adapter=codex` for that task and `adapter=claude` for any other task in the same run.

---

## 6. Acceptance criteria per entry (machine-checkable)

The critic verifies these on PR review.

| Entry | Acceptance criterion |
|---|---|
| T2-30 | `pytest oxi-core/tests/test_agentic_contract.py` green; `git diff --stat origin/main -- oxi-core/src/oxi_core/v3/dispatch.py oxi-core/src/oxi_core/v3/critic.py oxi-core/src/oxi_core/v3/tail_dispatch.py oxi-core/src/oxi_core/cli.py` shows zero changes; `lint-for-leaks.sh` green. |
| T2-31 | `pytest oxi-core/tests/test_codex_adapter_init.py oxi-core/tests/test_subprocess_helpers.py oxi-core/tests/test_dispatch_invoke.py` all green; `CodexCliAdapter().invoke(...)` raises `NotImplementedError`. |
| T2-32 | `pytest oxi-core/tests/test_codex_events.py` green; coverage on `codex_events.py` ≥ 95% (no untested branches). |
| T2-33 | `pytest oxi-core/tests/test_codex_session_file.py` green; `ledger_events.CODEX_REASONING_TOKENS_UNAVAILABLE` defined; `T1-14` typed-event-kinds doc updated. |
| T2-34 | `CodexCliAdapter` passes the **same** contract test as `ClaudeCodeAdapter` (`test_agentic_contract.py::test_codex_adapter_passes_contract`); `pytest oxi-core/tests/test_codex_invoke.py oxi-core/tests/test_codex_cost.py` green. |
| T2-35 | Dashboard renders the agentic-shadow panel; with `OXI_AGENTIC_SHADOW=codex` and a fake codex binary, dogfood tick produces ≥ 1 `agentic_shadow_observed` event in the ledger. |
| T2-36 | `inference-smoke.yml` skips correctly under `OXI_INFERENCE_OFFLINE=1`; `docs/runbooks/litellm-gateway.md` exists; `Adapter.inference_gateway_url()` defaults to `None` and `oxi-adapter-self` overrides it; `lint-for-leaks.sh` green on `inference.yaml`. |
| T2-37 | `pytest oxi-core/tests/test_inference_gateway.py` green; the `test_stream_false_locked` test specifically asserts `stream=False` in three call paths. |
| T2-38 | `pytest oxi-core/tests/test_heartbeat.py` green; with flag off, no inference calls (verified by passing a `FakeInferenceGateway` that records calls and asserting zero). |
| T2-39 | `pytest oxi-core/tests/test_routing.py` green; `route_for` is a pure function (verified by static check: no I/O imports beyond yaml load). |
| T2-40 | End-to-end smoke shows `adapter=codex` in the brief for a doc-tier-2 task and `adapter=claude` for everything else; `auto_merge` ships the PR; `lint-for-leaks.sh` green. |

---

## 7. Risk register (implementation-phase)

Distinct from the research-phase risk register. These are the things that can go wrong **while shipping**.

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Codex CLI version drift mid-rollout — operator updates codex between T2-31 and T2-34 and the parser breaks | Medium | High — silent | Smoke test on adapter init (T2-31). Pin version in adapter config. Operator changelog reviewed before each codex upgrade. |
| The extracted `_subprocess.py` refactor in T2-31 introduces subtle behavior change that breaks `dispatch_invoke` tests | Medium | High — regression in production | Refactor is deliberately small: pure-function helpers, no statefulness. All `test_dispatch_invoke.py` tests gate the merge. PR title flags the refactor; reviewer reads the diff line-by-line. |
| Mac Mini gateway unreachable during a Routine run (Q3 coupling) | Medium | Low — graceful degradation | `InferenceGateway` catches connection errors, emits `inference_gateway_unreachable` ledger event, returns a no-op result. T2-38 heartbeat triage falls back to today's no-LLM behavior. Documented in the runbook. |
| Virtual key on the Mac Mini gateway gets revoked or its budget exhausted | Low | Medium — heartbeat triage silently disabled | Healthcheck in T2-36 hits `/health` (which validates the configured key); failure emits a ledger event the operator sees in the daily brief. Runbook covers rotation. |
| Shadow-run harness (T2-35) doubles cost during the 14-day observation window | High | Medium — budget burn | Shadow runs gated behind `OXI_AGENTIC_SHADOW=codex` env var (default off). Operator opts in deliberately. Dashboard surfaces shadow cost separately. Daily hard cap still applies — shadow contributes to it. |
| Routing.yaml schema choices in T2-39 lock us into a syntax that can't express future rules | Medium | Medium — re-architecture | Schema is documented in the YAML comment header; first version is intentionally tiny (string equality + `contains`). The schema doc says "extending this is fair game; any extension ships as its own PR with a schema-version bump." |
| Codex adapter promotion (T2-40) ships a PR that the codex worker can't actually pass — codex isn't yet good at oxi's task style | Medium | Low — only doc tasks affected | Initial promotion is deliberately the **smallest** task class (doc-tier-2). Failures get auto-recovered (T1-12). After 14 days of mixed dogfood, decide whether to widen the promotion or roll back the rule. |
| Tool-schema drift (15% silent failure) breaks codex worker on any task that uses tools | High | High — silent | MCP-as-canonical (per hard constraint). Tool-call success rate is a first-class dashboard metric; operator watches it. If success rate drops below 80%, T2-40 is rolled back via the routing.yaml rule (one-line revert). |
| Reasoning-token undercount accumulates undetected in budget tracking | Medium | Medium — budget overshoot | T2-33 explicitly emits `CODEX_REASONING_TOKENS_UNAVAILABLE` events. Daily brief (T2-40 wiring) surfaces the count. If > 5% of dispatches hit this, escalate. |
| Behavioral contracts (T0-201/202/203) collide with the new adapter abstraction — contracts assume Claude-shaped events | Medium | Medium — false positives or false negatives at contract boundary | Open decision in §8: confirm whether contracts are keyed by `(role, model)` or `role`. If by `(role, model)`, add codex-specific contracts in a separate PR after T2-34. If by `role` alone, the contract layer needs to operate on the *normalized* DispatchResult (after the adapter), not on raw Claude events — which the architecture already enables via the AgenticAdapter boundary. |

---

## 8. Decisions (resolved 2026-04-25 by operator)

All four questions answered before Phase 2 begins. Phase 1 (T2-30) can ship now; Phase 2+ proceed under these resolutions.

### Q1 → Resolved: contracts keyed by `role` alone.

T0-203's `WORKER_CONTRACTS` stays as `dict[str, ContractSpec]`. Contracts apply on the **normalized** `DispatchResult` after the adapter call, so they're model-agnostic by construction. Codex-specific quirks (reasoning-token visibility, session-file dependency) are the adapter's responsibility to normalize; they don't leak into the contract layer.

**Implication for T2-30..T2-40:** no change required from the plan-as-written. The AgenticAdapter boundary is the right place for normalization; contracts run after it.

### Q2 → Resolved: no rename.

`dispatch_invoke.py` stays where it is. `agentic/__init__.py` defines the Protocol; `agentic/claude.py` wraps `dispatch_invoke.invoke()`. Git blame preserved, 30+ test references untouched, anti-pattern #1 (rename-mixed-with-feature) avoided. Shared subprocess helpers extracted to `_subprocess.py` per T2-31.

**Implication:** plan-as-written is correct. T2-30 ships without touching `dispatch_invoke.py`'s file path.

### Q3 → Resolved: Option A — reuse the existing Mac Mini gateway.

T2-36 connects to the LiteLLM gateway already running on Mac Mini (per the `local-inference` skill setup) instead of standing up a fresh docker-compose stack. Faster path, no duplicate infra.

**Implication for T2-36:** the entry rewrites slightly. Instead of "stand up LiteLLM via docker-compose," the work becomes "(a) provision a virtual key on the existing Mac Mini gateway for `oxi-heartbeat` role, (b) write the gateway URL + key into `defaults/inference.yaml`, (c) document the operator-side bootstrap (gateway URL discovery via Tailscale, key rotation procedure)." Net effect: T2-36 shrinks from ~80 LOC to ~30 LOC + a runbook entry. The InferenceGateway client (T2-37) is unchanged because it's just an HTTP client against any LiteLLM-compatible URL.

**Coupling risk acknowledged:** oxi's roadmap is now coupled to non-oxi infra (the Mac Mini gateway). If that gateway goes down, oxi's heartbeat falls back to the existing dispatch path. Document the dependency in the brief.

### Q4 → Resolved: Option A — `Adapter.codex_binary_version()` method, required, no default.

Adapters declare the codex binary version they expect. `oxi-adapter-self` returns the pinned version (e.g., `"0.125.0"`); the smoke test on adapter init refuses to run against any other version. Matches anti-pattern #3's "explicit, not defaulted" rule.

**Implication for T2-31:** adds one method to the Adapter Protocol with a backward-compat default returning `None` (= no smoke check), so existing adapters keep working. `oxi-adapter-self` overrides to return the pin. Test in T2-31 covers both cases.

---

## 9. Roadmap-PR shape

The single PR that lands this work appends the 11 items below to `docs/roadmap.md` (T2-30 through T2-40). Each item is a strict-grammar block:

```text
**T2-30 · agentic adapter protocol + ClaudeCodeAdapter shim**
_introduce oxi_core/v3/agentic/__init__.py defining the AgenticAdapter Protocol over the existing dispatch_invoke.invoke() contract; ship ClaudeCodeAdapter as a pure passthrough. ..._
```

The middle-dot is U+00B7 (`c2 b7` in UTF-8) — the same character T0-201/202/203 used and that the parser regex `^\*\*([A-Za-z0-9_\-]+)\s*·\s*(.+?)\*\*\s*$` matches.

After the PR merges, the engine seeds the items via `oxi v3 plan`. The dogfood loop ships them sequentially. Pierre reviews each PR (`auto_merge=False`).

---

## 10. Status

- [x] Q1, Q2, Q3, Q4 answered by operator (2026-04-25)
- [ ] T2-30 merged
- [ ] T2-31 merged
- [ ] T2-32 merged
- [ ] T2-33 merged
- [ ] T2-34 merged
- [ ] T2-35 merged + 14-day shadow window started
- [ ] T2-36 merged
- [ ] T2-37 merged
- [ ] T2-38 merged
- [ ] T2-39 merged
- [ ] T2-40 merged + 14-day post-promotion dogfood window started
- [ ] Decision on widening codex promotion beyond doc-tier-2 (separate plan doc)
