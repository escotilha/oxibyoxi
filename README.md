```text
 \_//
  oxi
```

# oxi

**Point Claude Code at a markdown roadmap. Walk away. Come back to PRs.**

oxi turns `roadmap.md` into shipped code. Each tick: it picks the next planned task, spawns a `claude -p` session in a fresh git worktree, watches the worker open a PR, runs a second-model critic on the diff, and merges (or rejects) according to your policy. Every guardrail you'd want — budget hard-cap, killswitch, heartbeat reaper, ship-recovery, prompt-injection isolation, parameterized SQL, env-whitelisted subprocess — is on by default.

<!-- Screenshot of `oxi v3 tick --real-claude` shipping a PR. Captured by an
operator on a real run; this slot reserves layout so the README's first
visual is a proof-by-screenshot, not prose. See docs/assets/README.md. -->

![oxi v3 tick — engine shipping a PR end-to-end](docs/assets/oxi-tick-screenshot.png)

## Before / after

Two terminals, same `roadmap.md`.

```text
─── before ────────────────────────────────────────────
$ cat roadmap.md
**T0-1 · add a greet function**
**T0-2 · add a changelog stub**
**T1-1 · expose greet in __all__**

$ gh pr list --state merged
(no merged PRs)

$ git log --oneline -1
8b3a2c1 chore: scaffold project
```

```text
─── after ─────────────────────────────────────────────
$ oxi v3 tick --real-claude --times 3
── tick 1/3 · 14:22:10 · REAL CLAUDE ──
  dispatch: classification=success cost=$0.31
  pr_watcher: stamped=1 merged=1
✓ tick done · merged=1

── tick 2/3 · 14:24:42 · REAL CLAUDE ──
  dispatch: classification=success cost=$0.27
  pr_watcher: stamped=1 merged=1
✓ tick done · merged=2

── tick 3/3 · 14:26:01 · REAL CLAUDE ──
  dispatch: classification=success cost=$0.18
  pr_watcher: stamped=1 merged=1
✓ tick done · merged=3

$ gh pr list --state merged
#42  feat: add greet function
#43  docs: add changelog stub
#44  feat: expose greet in __all__
```

## Install

oxi requires **Python 3.11+**. macOS ships 3.9, so most Mac users need to install a newer Python first (`brew install python@3.13`). On Ubuntu/Debian: `sudo apt install python3.13 python3.13-venv`.

```bash
# from inside your project's checkout
python3.13 -m venv .venv && source .venv/bin/activate
pip install --pre oxi-core      # beta — --pre still required while on 0.1.0b*
oxi init                        # 8-prompt wizard scaffolds your adapter
cd oxi-adapter-myproject && pip install -e .
oxi status                      # ✓ adapter loaded
oxi v3 tick --real-claude       # spends budget, ships PRs
```

> **Hit `Could not find a version that satisfies the requirement oxi-core`?** Your pip is bound to Python <3.11. Check with `python3 --version`; on macOS the stock `python3` is 3.9. Install 3.11+ as above and create the venv with the explicit interpreter (`python3.13 -m venv .venv`).

The five-minute install runbook is at [`docs/runbooks/install.md`](docs/runbooks/install.md). Full operator manual at [`docs/manual/`](docs/manual/).

**Models.** The agentic worker (the thing that writes code and opens PRs) runs Claude — that's what the wizard's "plan tier" question is about. The non-agentic side (planner, classifier, ranker, dedup, summary) goes through an `InferenceGateway` and already supports OpenRouter and xAI/Grok via LiteLLM; configure it with [`docs/runbooks/litellm-gateway.md`](docs/runbooks/litellm-gateway.md). Multi-provider agentic workers (Codex, Aider) are tracked on the [roadmap](docs/roadmap.md) as the T2-30 series.

**Status:** beta (`0.1.0b1` on PyPI). The engine has been dogfooding itself for two days — 100+ PRs merged across the build, every safety rail proven in production, parallel dispatch + auto-merge live, daily-cap raised to $100 once the engine showed it could spend it productively. See [release notes](docs/release-notes/v0.1.0b1.md) for what's in this cut.

## How it works

```text
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  roadmap │→→→│  planner │→→→│ dispatch │→→→│pr_watcher│→→→│auto_merge│
│   .md    │   │          │   │ claude -p│   │          │   │  critic  │
└──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
                    ↑              ↓              ↓              ↓
                    └─────── fronts, heartbeat, ship_recovery ───┘
                         budget · deadman · oauth_watch
```

Every tick: planner reads your markdown roadmap, dispatch spawns `claude -p` in a git worktree, the worker opens a PR, pr_watcher tracks it, auto_merge runs a critic review, merge lands. Heartbeat rescues stalled sessions; ship_recovery rescues uncommitted work; budget enforcement prevents runaway spend; deadman shouts when the engine goes quiet.

Every project-specific value — repo path, budget cap, plan tier, dispatch host — is behind an **adapter**. Your fork writes its own ~70-line adapter class; core is untouched.

## Quick start

```bash
# 1. Scaffold an adapter for your project (interactive, 8 prompts)
oxi init

# 2. Install it editable
cd oxi-adapter-<your-project>
pip install -e .

# 3. Confirm the adapter loads
oxi status

# 4. Run a reconciliation tick (no Claude spend)
oxi v3 tick --times 1

# 5. When you're ready to spend real Claude budget:
oxi v3 tick --real-claude
```

`oxi init` asks for your project name, GitHub repo, roadmap location, budget caps, plan tier, and dispatch policy. It writes a fully-configured adapter package you can `pip install -e .` immediately.

## CLI

| Command | Effect |
|---|---|
| `oxi init [destination]` | Scaffold a new adapter (8-step wizard) |
| `oxi status` | Budget + task counts + recent events |
| `oxi brief [--hours N]` | Markdown daily recap |
| `oxi v3 tick [--times N]` | Reconciliation pass (no Claude) |
| `oxi v3 tick --real-claude` | Dispatch + critic-gated merge (spends budget) |
| `oxi v3 kill [--reason R]` | Halt the engine (killswitch file) |
| `oxi v3 unkill` | Resume |
| `oxi dashboard [--port P]` | Localhost HTML dashboard |

## Roadmap format

oxi parses markdown roadmaps:

```markdown
## Tier 0

**T0-1 · add a greet function**
_a pure function that returns "hello, {name}"_

**T0-2 · add a changelog stub**

## Tier 1

**T1-1 · expose greet in __all__**
```

Tier 0 items dispatch first. The wizard lets you put the roadmap anywhere (default: `roadmap.md` at repo root).

## Safety

Every feature that spends money or changes state is gated:

- **Budget**: `adapter.budget().daily_hard_cap` is enforced. Runaway loop → one-time `budget_hard_stop` ledger event, dispatch stops, critic stops. Resume by bumping caps and clearing the event (`oxi` CLI surface for this coming).
- **Critic**: `auto_merge.policy().auto_merge` is off by default in new adapters. You opt in explicitly.
- **Deadman**: if the engine hasn't dispatched in N minutes, a `NotificationBackend` fires escalating INFO → WARN → ALERT → DEAD. Forks wire their own backend (Slack / email / PagerDuty); default logs to stderr.
- **OAuth watch**: checks your Claude credentials file for expiry, fires lead-time warnings before dispatch fails.
- **Ship recovery**: if a Claude session writes code then exits before committing (compaction, crash, rate limit), the next tick stages + commits + pushes the uncommitted changes.

## Architecture

The internals — adapter protocol, state machine, module layout, dependency graph — are documented in [`docs/architecture.md`](docs/architecture.md). The 5-minute summary: `oxi-core/` ships as `oxi-core` on PyPI; everything project-specific lives in adapters under `adapters/`; tests in `oxi-core/tests/` use a fake-claude + fake-GitHub harness so CI never touches real services.

## Design principles

- **One binary, one command.** `oxi init`, then `oxi v3 tick`. No sprawling CLI surface.
- **Everything project-specific lives in an adapter.** Core has zero strings naming any specific project.
- **Fake the world in tests.** 940+ tests use `fake_claude.py` + `FakeGitHubClient`; no real Claude or GitHub contact in CI.
- **Atomic state transitions.** Every status update stamps `last_progress_at` in the same transaction. Reapers never trust `created_at`.
- **Protocols over implementations.** `CriticBackend`, `GitHubClient`, `NotificationBackend` are pluggable. Forks substitute any of them.
- **No premature abstraction.** Three similar lines beats one generic helper that handles three cases.

## Roadmap

See [`docs/roadmap.md`](docs/roadmap.md) for current items. Phase 1 (engine), Phase 2 (safety + dogfood), Phase 3 (operator polish) all shipped. Beta is live (`0.1.0b1`); next milestone is `0.2.0` for any breaking adapter-protocol changes. Release history at [`docs/release-notes/`](docs/release-notes/).

## Status

**Beta** (`0.1.0b1`). The engine has dogfooded itself end-to-end — 100+ PRs merged across two consecutive days with the engine writing most of them, parallel dispatch active (3-5 concurrent workers, RAM-probed), auto-merge enabled for engine-originated PRs, daily-cap raised to $100 once the rails were proven, full first-fork install path verified against PyPI from a fresh venv. 940+ tests pass against a fake-claude + fake-GitHub harness.

The 0.1.0b* line commits to no breaking changes within minor. 0.2.0+ may break the adapter protocol; check release notes.

Budget caps bound damage at whatever you set `daily_hard_cap` to. Auto-merge defaults off — opt in explicitly per `DispatchPolicy`.

## License

MIT at `1.0`. Everything here is safe to fork, modify, redistribute.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for branch conventions, PR format, the dogfood-first rule, and how to author your own adapter. Issue templates at [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) walk you through bug reports (with a ledger-dump snippet) and feature requests.

Public and in beta — external PRs welcome. The [public-flip checklist](docs/public-flip-checklist.md) records the gates the project crossed before going public; it's now a historical artefact rather than an open task list.

## Anti-patterns

[`docs/anti-patterns.md`](docs/anti-patterns.md) documents nine constraints the project enforces via CI. Read it before filing a "why doesn't oxi do X" issue — X might be deliberately excluded.

## Acknowledgements

oxi's design is informed by a prior in-house orchestrator; the prior system's failure modes are documented in [`docs/origin-feature-gap-2026-04-24.md`](docs/origin-feature-gap-2026-04-24.md). No code, strings, or identifiers from that system cross into oxi — a CI-enforced forbidden-string list at `scripts/lint-for-leaks.sh` is the gate.
