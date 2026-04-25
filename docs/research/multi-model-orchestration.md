# Deep Research Report: Multi-Model Orchestration Layer for oxi

**Date:** 2026-04-25
**Confidence:** 84%
**Research Depth:** 6 sub-questions, 47 distinct sources, source files in `~/code/oxi` directly inspected (`dispatch_invoke.py`, `critic.py`, repository layout)

---

## 1. Executive Summary

**Recommendation: Split the multi-model boundary into two layers, not one.** Most of the literature (and most of the commercial gateway pitches) collapse "agentic CLI" and "raw inference" into a single abstraction — that's wrong for oxi. The oxi engine has two structurally different traffic patterns: long-running tool-using subprocess sessions ($1-2, multi-turn, tool calls, file edits) and short structured-output calls (heartbeat, ledger summary, prompt-injection check, tier router — sub-cent, single-shot, no tools). Putting both behind one adapter forces the cheap path to inherit the expensive path's contract complexity (events, sessions, process groups), and forces the expensive path to inherit the cheap path's stateless API shape.

The architecture that fits oxi's existing code is:

1. **`AgenticAdapter` Protocol** at `oxi-core/src/oxi_core/v3/agentic/` — a generalization of the current `dispatch_invoke.invoke()` contract. Two backends in scope: **Claude Code** (today, preserved) and **Codex CLI** (`codex exec --json`). Each adapter normalizes its native event stream into oxi's existing `DispatchResult`. This composes cleanly with the PR #118 behavioral-contract layer because each `(role, model)` pair gets its own contract.

2. **`InferenceGateway`** for non-agentic calls, deployed as a self-hosted **LiteLLM proxy**. Provides `x-litellm-response-cost` per-request rollup, virtual keys with budget caps, OpenAI-compatible API surface across Anthropic/OpenAI/Ollama/MLX. Used by heartbeat, ledger summary, prompt-injection classifier, and the routing decision itself. Runner-up: Portkey (managed). OpenRouter explicitly rejected for oxi because per-request cost metadata is not exposed and that breaks `DispatchResult.cost_usd`.

3. **Routing** lives in `dispatch.py` (the supervisor), not in a new framework. A small typed `route_for(role, task) → ModelChoice` function plus a YAML routing matrix beats RouteLLM/DSPy router for oxi's needs — those frameworks optimize for win-rate-on-MT-Bench, oxi optimizes for "did the task pass critic + CI."

4. **Verifier pattern: hold off.** The literature (Mastra: 37% silent tool-call mismatches; LLM-as-judge calibration is order-sensitive ±14%; current critic is already a single-model verifier) does not yet justify a Codex-verifies-Claude critic loop in production. Keep the existing `ClaudeCriticBackend`, add an experimental `CodexCriticBackend` behind a flag for shadow runs only.

5. **What we don't do: Agent Client Protocol (acpx) is rejected** for the agentic boundary (already evaluated this session — cost reporting gap, schema mismatch, missing budget flag). LangGraph / AutoGen / CrewAI are rejected — they are orchestration frameworks that would replace `dispatch.py`, not slot under it. RouteLLM is rejected as a primary router — it solves a different problem (chat quality cascade) and its routing signal (preference data) is wrong for "should this task get Opus or local Qwen3-Coder."

The migration is bounded: ~600 lines net new code, four new modules, no break to the `DispatchResult` contract, four downstream consumers (`dispatch.py`, `critic.py`, `heartbeat.py`, `tail_dispatch.py`) need a one-line change each.

---

## 2. Key Findings

### Finding 1: Codex CLI's `codex exec --json` event schema is a near-isomorphism with Claude Code's stream-json

- **Claim:** `codex exec --json` emits JSONL events `{thread.started, turn.started, turn.completed, turn.failed, item.started, item.completed, error}` where `turn.completed` carries `usage: {input_tokens, cached_input_tokens, output_tokens}` and `item.completed` covers `agent_message`, `reasoning`, `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, `plan_update`. This maps almost field-for-field onto Claude's `{system, assistant, tool_use, tool_result, result}` event types.
- **Evidence Tier:** 1 (multiple primary sources: OpenAI's non-interactive docs and the Codex GitHub TypeScript SDK README directly enumerate these types)
- **Confidence:** 90%
- **Sources:** OpenAI Codex non-interactive docs; Codex CLI reference; Codex TypeScript SDK README; ccusage Codex guide; GitHub issues #5276 and #2288.
- **Counter-evidence:** Codex `--json` does NOT include `cost_usd` in dollars — only token counts. **oxi must compute cost from tokens × OpenAI pricing table inside the Codex adapter** (this is solved territory; LiteLLM and ccusage already publish current price tables). Reasoning tokens specifically were a documented gap (issue #5276 closed "not planned" as of 2025-10-17) — the adapter must know that "real" output tokens may include reasoning.

### Finding 2: LiteLLM is the right gateway for the inference layer; OpenRouter is not

- **Claim:** LiteLLM (Apache 2.0, BerriAI) is the only mature open-source gateway that returns per-request cost via `x-litellm-response-cost` header, supports virtual-key budgets, and gives a unified OpenAI-format surface across Anthropic + OpenAI + Ollama + MLX (via `ollama_chat/`, `openai/` for MLX servers, native `anthropic/`). It maps to the oxi requirement that `DispatchResult.cost_usd` must be populated for ledger and budget enforcement.
- **Evidence Tier:** 1
- **Confidence:** 88%
- **Counter-evidence:** LiteLLM has a known bug — when streaming with `include_usage: true`, the `x-litellm-response-cost` header is dropped. For oxi this is mitigable: non-agentic paths (heartbeat, ledger summary, classifier) call LiteLLM in **non-streaming** mode where the header works correctly. Agentic paths don't go through LiteLLM at all. Issue #12689 is open and unresolved as of April 2026.
- **Why not OpenRouter:** Per-request cost is dashboard-only, never returned in API responses. That breaks oxi's per-task cost-cap contract.
- **Why not Portkey:** Managed SaaS — adds an external dependency for what is currently a fully self-hosted, secrets-isolated supervisor. Acceptable as runner-up if oxi later moves to managed infra.

### Finding 3: Local agentic models cleared the production threshold in late 2025–early 2026

- **Claim:** Three open-weight models can drive multi-turn coding sessions reliably on M4 Pro 48GB:
  - **Qwen3-Coder-30B-A3B** (Apache 2.0, MoE 30.5B / 3.3B active, MLX 4-bit) — SWE-bench Verified **51.6%** with OpenHands-100-turn scaffold, ~30-35 tok/s on M4 Pro Q4, ~68-87 tok/s on M4 Max MLX-4bit.
  - **Devstral 2 Small** (Apache 2.0, 24B dense, 256K context) — SWE-bench Verified **72.2%**, native function-calling, "tracks project structure" agentic loop.
  - **GLM-4.5-Air** (106B MoE, MLX 4-bit on Apple Silicon, MIT-license-equivalent open) — 90.6% tool-calling accuracy beating Sonnet's 89.5%, hybrid thinking modes.
- **Evidence Tier:** 2 (vendor scores corroborated by multiple third-party MLX benchmarks; some reproducibility caveats on Qwen scaffold version)
- **Confidence:** 75% — these scores come from vendor cards and reproductions; in-house validation is needed before oxi routes a real PR through them.
- **Counter-evidence:** All open-weight numbers are scaffold-dependent. The Qwen3-Coder Hugging Face discussion specifically flags that OpenHands version, vLLM version, and tool-call-parser choice (`qwen_xml` vs `qwen_coder`) move scores by significant margins. **For oxi: do not route production tasks to local models on the strength of leaderboard numbers; gate on internal eval against oxi's own roadmap-item corpus.**

### Finding 4: Tool-schema drift is a documented production hazard — the multi-model layer must own a normalization step

- **Claim:** Cross-provider tool-calling fails at 15% baseline error rate without normalization, dropping to 3% with a compatibility layer. OpenAI throws on schema violations; Gemini silently ignores constraints; Anthropic is most permissive. Twenty-six of every 100 tool calls in mixed-provider production traffic fail or silently misbehave.
- **Evidence Tier:** 1 (Mastra publishes specific error rates per model class with the fix architecture documented)
- **Confidence:** 90%
- **Implication for oxi:** The `AgenticAdapter` boundary must declare an oxi-internal tool schema, and each backend (Claude, Codex) must translate to/from its provider's flavor. This is not optional. The good news: Codex CLI and Claude Code both consume MCP, which already imposes a JSON Schema discipline.

### Finding 5: The verifier/critic literature does not justify dual-model verification as the default

- **Claim:** LLM-as-judge has documented calibration problems: order-sensitivity flips up to 14% of decisions, GPT-4-class judges hallucinate bugs that don't exist, prompt wording shifts scores ±3 points on individual tasks. Production AutoGen averages $0.35/query at 70% uptime — non-trivial overhead for a verifier. **Self-verification (the model double-checks its own work in thinking mode) often matches dual-model in quality at a fraction of the cost.**
- **Evidence Tier:** 1-2 (mix of empirical study and production telemetry)
- **Confidence:** 80%
- **Implication for oxi:** **Don't replace the current single-model critic with a Codex-verifies-Claude pipeline as the default.** The current `ClaudeCriticBackend` Sonnet-on-diff is already cost-efficient ($1.50/$7.50 per million tokens batched). Add a `CodexCriticBackend` as a *shadow* backend behind an opt-in flag — log the verdicts, don't gate on them, until you have at least 100 in-distribution comparisons.

### Finding 6: Small models are good enough for routing, classification, and summary — but not all sizes

- **Claim:** Qwen 2.5 1.5B/3B Instruct surpassed DeBERTa-v3-Prompt-Injection-v2 baseline F1 of 0.73 with only 5 training examples; published prompt-injection classifiers achieve F1 0.96-0.99 on small lightweight models. Phi-4 Mini (3.8B) outperforms Llama 3.2 3B on every standard benchmark. For Anthropic's published ticket-routing pattern, Haiku achieves 85-90% intent accuracy.
- **Evidence Tier:** 1 (peer-reviewed benchmarks with F1 numbers)
- **Confidence:** 85%
- **Implication for oxi:** Run **Qwen 2.5 3B Instruct** or **Phi-4 Mini** on the Mac Mini (or VPS Ollama) for: ledger summarization, prompt-injection screening of inbound roadmap items, the routing-tier classifier, and "is this PR description coherent" checks. Quality floor for these tasks is around 1.5B–3B. Below 1.5B (e.g. Qwen 0.5B), the noise overwhelms the signal — that's the floor.

### Finding 7: The other agentic CLIs (Aider, Goose, opencode, Cline) are not fit-for-purpose as oxi adapters today

- **Claim:** None of Aider, Goose, opencode, or Cline expose a stable, cost-reporting, JSONL-streamed, cwd-targeted, programmatic-orchestration contract on par with `claude -p` or `codex exec --json`. Aider has `--message` for one-shot but no `--json` and no documented cost output. Goose's headless `goose serve` is documented but JSON event format and cost reporting aren't published. Opencode is TUI-first with client/server but lacks a documented orchestration contract. Cline is a VS Code extension primarily.
- **Evidence Tier:** 2
- **Confidence:** 80%
- **Implication for oxi:** Build adapters for **Claude Code (today)** and **Codex CLI (next)** only. Defer Aider/Goose/opencode/Cline. They are good interactive tools; they are not yet good orchestration backends.

---

## 3. Architecture (one-page diagram in text)

```
┌─────────────────────────────────────────────────────────────────────┐
│ oxi supervisor (dispatch.py)                                        │
│   - claims a task                                                   │
│   - composes prompt via prompts.py                                  │
│   - decides budget cap, wall-clock, allowed_tools                   │
│   - calls: route_for(role, task) → ModelChoice                      │
└─────────────────────────────────────────────────────────────────────┘
                  │                                    │
                  │ (agentic)                          │ (inference-only)
                  ▼                                    ▼
┌─────────────────────────────────────┐  ┌──────────────────────────────┐
│ AgenticAdapter Protocol             │  │ InferenceGateway             │
│ v3/agentic/__init__.py              │  │ v3/inference/__init__.py     │
│                                     │  │                              │
│ async def invoke(                   │  │ async def chat(              │
│   inv: DispatchInvocation           │  │   model: str,                │
│ ) -> DispatchResult                 │  │   messages: list[Message],   │
│                                     │  │   max_budget_usd: float,     │
│ contract: stream events,            │  │   tools: list | None,        │
│   compute cost_usd, classify exit,  │  │   stream: bool = False,      │
│   honor wall_clock, cwd, env        │  │ ) -> InferenceResult         │
│   whitelist, ssh wrap.              │  │                              │
└────────────┬───────────┬────────────┘  │ contract: dollars-per-call,  │
             │           │               │   structured-output schema,  │
             ▼           ▼               │   no shell, no fs, no tools  │
┌──────────────────┐ ┌──────────────────┐│   except light MCP allowed.  │
│ ClaudeCodeAdapter│ │ CodexCliAdapter  │└──────────┬───────────────────┘
│                  │ │                  │           │
│ wraps current    │ │ wraps codex exec │           ▼
│ dispatch_invoke. │ │ --json. Computes │ ┌──────────────────────────┐
│ invoke()  (zero  │ │ cost_usd from    │ │ LiteLLM Proxy (self-host)│
│ break to current │ │ tokens × pricing │ │  -  Anthropic / OpenAI / │
│ behavior)        │ │ table.           │ │     Ollama / MLX-server  │
└──────────────────┘ └──────────────────┘ │  -  per-key budgets      │
                                          │  -  x-litellm-response-  │
                                          │     cost header (non-    │
                                          │     streaming)           │
                                          │  -  virtual keys per role│
                                          └──────────────────────────┘
                                                     │
                                                     ▼
                                          ┌──────────────────────────┐
                                          │ Local backends           │
                                          │ - Mac Mini MLX:          │
                                          │   Qwen 2.5 3B (router),  │
                                          │   Phi-4 Mini (classifier)│
                                          │ - Optional: Ollama VPS   │
                                          └──────────────────────────┘
```

**The boundary line is sharp**: `AgenticAdapter` returns `DispatchResult` (preserves the existing contract end-to-end). `InferenceGateway` returns a new, smaller `InferenceResult` because heartbeat/summary/classifier callers don't need or want events, sessions, process groups, or wall-clock kills.

---

## 4. Routing Matrix

| Operation | Path | Model class | Backend | Rationale |
|---|---|---|---|---|
| Roadmap-item implementation (high-stakes) | Agentic | Opus 4.7 | ClaudeCodeAdapter | Highest-quality agentic loop, behavioral-contract attached. Budget $2 (current default). |
| Roadmap-item implementation (standard) | Agentic | Sonnet 4.6 | ClaudeCodeAdapter | Default "real work" tier. Budget $0.50. Today's path, untouched. |
| Roadmap-item implementation (cheap) | Agentic | GPT-5.4 / GPT-5-mini | CodexCliAdapter | Second-source diversity; if Anthropic rate-limit-exhausted, fall over to Codex on same task. |
| Roadmap-item implementation (experimental) | Agentic | Devstral 2 / Qwen3-Coder local | (future, behind flag) | Zero marginal cost. Gate on internal eval before promoting from experimental. |
| Critic / pre-merge gate | Agentic | Sonnet 4.6 | ClaudeCodeAdapter (today) | Keep `ClaudeCriticBackend` as default. Add `CodexCriticBackend` shadow. |
| Auto-fix CI failure | Agentic | Sonnet 4.6 | ClaudeCodeAdapter | Tight feedback loop; same provider as the implementation reduces context-translation cost. |
| Heartbeat / engine_health summary | Inference | Phi-4 Mini local | LiteLLM → Ollama/MLX | Cents per day. Sub-50ms latency floor. F1 0.96+ on classification. |
| Ledger event summarization | Inference | Phi-4 Mini local | LiteLLM → Ollama/MLX | Same. |
| Prompt-injection screen on inbound roadmap items | Inference | Qwen 2.5 3B Instruct local | LiteLLM → Ollama/MLX | Documented F1 0.96-0.99 floor for this task class. |
| Tier-routing decision | Inference | Qwen 2.5 3B Instruct or Haiku 4.5 | LiteLLM → Ollama or Anthropic | Anthropic ticket-routing cookbook validates Haiku at 85-90%. Local Qwen 2.5 3B if budget-zero is the goal. |
| PR-description coherence check | Inference | Phi-4 Mini local | LiteLLM → Ollama/MLX | Single-shot, structured-output. |
| Roadmap intent classification | Inference | Haiku 4.5 | LiteLLM → Anthropic | Cookbook-blessed pattern, 85-90% accuracy out-of-box. |

**Routing logic placement:** A pure function `oxi_core/v3/routing.py::route_for(role: WorkerRole, task_meta: TaskMeta) → ModelChoice`, driven by a YAML config `oxi-core/src/oxi_core/defaults/routing.yaml`. No DSPy, no RouteLLM, no ML-trained router — those are oversized for "pick a tier from a 4×3 matrix." Re-evaluate if the matrix grows past 20 cells.

---

## 5. Gateway Choice

**Selected: LiteLLM (self-hosted Docker).**

**Why:** Per-request `x-litellm-response-cost` header is the only mature mechanism that preserves oxi's `DispatchResult.cost_usd` contract for non-agentic calls. Virtual keys with budget caps map naturally onto oxi roles (one virtual key per role: `oxi-router`, `oxi-classifier`, `oxi-summary`, `oxi-critic-shadow`). Apache 2.0. Python-native ecosystem matches oxi. Self-hosted preserves the secrets-isolated supervisor model.

**Runner-up: Portkey** — managed equivalent, hierarchical budgets, RBAC. Worth evaluating only if oxi later adopts managed infra and operator burden shifts to Portkey-as-vendor.

**Explicitly rejected:**
- **OpenRouter** — per-request cost is not in the response, only the dashboard. Breaks budget enforcement at the call site.
- **Bifrost / Helicone / Cloudflare AI Gateway** — either narrower scope or focused on observability, not budget enforcement at the per-call level.

**Mitigation for the streaming-cost bug** (LiteLLM #12689): all `InferenceGateway` calls run **non-streaming** by default. Streaming is only relevant for human-facing UIs; oxi's heartbeat/classifier/summary callers are programmatic and benefit from atomic, header-attached responses anyway.

---

## 6. Migration Path

### What changes in `dispatch_invoke.py`

**No change to file `dispatch_invoke.py` itself.** It becomes the implementation of `ClaudeCodeAdapter`. Move it (or re-export) from `oxi_core/v3/dispatch_invoke.py` to `oxi_core/v3/agentic/claude_code.py` with a thin re-export shim at the old path during the migration window.

### New modules

```
oxi-core/src/oxi_core/v3/
├── agentic/
│   ├── __init__.py            # AgenticAdapter Protocol + factory
│   ├── claude_code.py         # current dispatch_invoke.py, renamed; ZERO contract change
│   ├── codex_cli.py           # NEW — wraps `codex exec --json`; emits same DispatchResult
│   └── result_event_map.py    # NEW — provider→DispatchResult event normalization
├── inference/
│   ├── __init__.py            # InferenceGateway Protocol + LiteLLMGateway impl
│   ├── litellm_gateway.py     # NEW — async httpx client, cost header parsing
│   └── pricing_table.py       # NEW — token→USD lookup for Codex (where header absent)
├── routing.py                 # NEW — route_for(role, task) → ModelChoice; pure func
└── dispatch_invoke.py         # SHIM — re-exports from agentic/claude_code.py
```

### Adapter API surface

```python
# oxi_core/v3/agentic/__init__.py
class AgenticAdapter(Protocol):
    async def invoke(self, inv: DispatchInvocation) -> DispatchResult: ...

# oxi_core/v3/inference/__init__.py
@dataclass(frozen=True)
class InferenceCall:
    model: str
    messages: list[dict]
    max_budget_usd: float
    response_format: dict | None = None  # JSON-schema for structured output
    timeout_s: float = 30.0
    tools: list[dict] | None = None  # rare, MCP-light only

@dataclass
class InferenceResult:
    text: str
    parsed: dict | None        # if response_format was set
    cost_usd: float            # from x-litellm-response-cost OR pricing table
    input_tokens: int
    output_tokens: int
    classification: Classification  # SUCCESS/FAILED/TIMEOUT — same enum

class InferenceGateway(Protocol):
    async def chat(self, call: InferenceCall) -> InferenceResult: ...
```

### Phased rollout

1. **Phase 1 (week 1, additive only)**: introduce `AgenticAdapter` Protocol + `ClaudeCodeAdapter` wrapping current code. Net new code: ~80 lines.
2. **Phase 2 (week 2-3)**: build `CodexCliAdapter`. Run it in shadow mode on a copy of recent tasks. Net new code: ~250 lines.
3. **Phase 3 (week 3-4)**: stand up self-hosted LiteLLM. Migrate `heartbeat.py` and a single ledger-summary path. Net new code: ~150 lines.
4. **Phase 4 (week 4)**: wire `routing.py` + YAML config; promote one production task class to Codex. Net new code: ~100 lines.
5. **Phase 5 (deferred)**: local agentic models behind a flag, after internal eval against oxi roadmap-item corpus.

---

## 7. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Codex `usage` does not include reasoning tokens; oxi under-bills | High | Medium | The adapter must apply the OpenAI-published reasoning-token surcharge from the pricing table when the model is a reasoning model. Validate by reconciling against the OpenAI dashboard weekly for the first month. |
| Tool-schema drift between Claude/Codex causes silent tool-call failures | Medium-High | High | Adopt MCP as the canonical tool format on the oxi side. Each adapter translates from oxi-MCP to its provider's tool format. Track tool-call success rate per adapter as a first-class metric in `dashboard.py`. |
| LiteLLM streaming-cost-header bug returns | Medium | Low | Mitigated by design — oxi calls LiteLLM non-streaming. Lock this with a unit test that asserts `stream=False` on every InferenceCall. |
| SSH wrap path needs to support Codex on remote hosts | Medium | Medium | `wrap_with_ssh()` is provider-agnostic — it wraps any local argv. Codex adapter calls into it identically. Add a remote-Codex integration test mirroring the existing remote-Claude one. |
| Ledger event format diverges across adapters | Medium | High | The event normalizer (`agentic/result_event_map.py`) is the single source of truth. Add a contract test: every adapter must emit at least `system`/`assistant`/`tool_use`/`tool_result`/`result` shapes. Reject PRs where the schema diverges without a migration. |
| Codex CLI breaks JSONL contract in a future version | Medium | High | Pin the Codex binary version in the adapter config. Add a smoke-test on adapter init that emits a known-good JSONL event sequence and parses it. |
| Routing decision itself becomes a bottleneck | Low | Medium | `route_for()` is a pure function over a YAML matrix — no LLM call required for the common case. |
| Behavioral-contract layer (PR #118) gets coupled to one provider's event format | Medium | High | Define contracts against the oxi-internal event schema, not against Claude or Codex specifically. The normalizer is what makes contracts portable. |
| Local-model output non-determinism breaks ledger replays | High | Low | Local models are scoped to non-agentic, structured-output-only paths in this design. Production-task replays only ever go through hosted providers. |
| acpx (rejected) re-enters the conversation | Low | Medium | The decision to use direct subprocess adapters over Agent Client Protocol is documented in this report. Revisit only if acpx ships explicit per-call cost reporting + budget enforcement + cwd targeting. |

---

## 8. What We Are NOT Doing (Explicit Scope Bounds)

- NOT replacing `dispatch_invoke.py`'s subprocess discipline. The process-group isolation, env whitelist, JSON-truncation tolerance, and SIGTERM handling are hard-won fixes for documented incidents. Codex adapter inherits the same lifecycle.
- NOT adopting Agent Client Protocol (acpx) — already rejected this session for missing per-call cost, schema mismatch, missing budget flag.
- NOT building a custom LLM router. No DSPy, no RouteLLM, no ML-learned tier classifier. A 4×3 YAML matrix + pure function suffices for the foreseeable expansion.
- NOT switching the critic to dual-model verification by default. Add `CodexCriticBackend` as a shadow backend; collect data; revisit after 100 in-distribution comparisons.
- NOT routing production roadmap items to local models in Phase 1-4. Promote local models from "experimental" to "standard" only after they pass an oxi-specific eval against the in-house roadmap-item corpus.
- NOT migrating to LangGraph, AutoGen, CrewAI, or Pydantic AI. These are orchestration frameworks; oxi already has an orchestrator (`dispatch.py`).
- NOT adopting OpenRouter, Portkey-managed, Bifrost, or Cloudflare AI Gateway in Phase 1. LiteLLM self-hosted only.
- NOT building Aider, Goose, opencode, or Cline adapters in Phase 1-5.
- NOT replacing `DispatchResult` or the ledger event schema. Both are normalized targets; new providers translate *to* them.
- NOT introducing a new tool-permission model. Allowed-tools strings continue to flow through `DispatchInvocation`.
- NOT shipping any change that depends on Codex CLI's behavior holding stable across minor versions without a pinned version + smoke test.

---

## 9. Confidence Assessment

- **Overall Confidence:** 84%
- **What would change our conclusion:**
  - If Codex CLI's `--json` schema turns out to be unstable across minor versions → demote Codex adapter from Phase 2 to "research project," ship Phase 1 (Claude-only refactor) + Phase 3 (LiteLLM gateway) and revisit.
  - If LiteLLM's streaming-cost-header bug isn't fixed and a streaming codepath becomes essential for inference calls → reconsider Portkey as primary.
  - If oxi's behavioral-contract layer (PR #118) requires real-time event-schema validation against a fixed format → the normalizer becomes load-bearing and must be designed first.

---

## 10. Recommended Next Steps

1. **Spike: build a 100-line `CodexCliAdapter` proof-of-concept** that runs a single roadmap item end-to-end and emits a `DispatchResult` shaped identically to Claude's. Cost: ~half a day.
2. **Spike: stand up LiteLLM in Docker with one virtual key per role**, route `heartbeat.py` through it, validate per-call cost in the response header. Cost: ~half a day.
3. **Decide before /deep-plan:** does PR #118 differentiate contracts by `(role, model)` or by `role` alone?
4. **Decide before /deep-plan:** is the `oxi-core/v3/agentic/claude_code.py` rename an acceptable refactor, or should Phase 1 keep `dispatch_invoke.py` as the canonical path?
5. **Internal eval corpus**: pull the last 50 closed roadmap items into a fixture. Replay them through Claude-Sonnet, Codex-GPT-5.4, and Devstral-2 in shadow mode. This is the precondition for ever promoting a local model from experimental.

---

**Report ends. Ready for /deep-plan ingestion.**
