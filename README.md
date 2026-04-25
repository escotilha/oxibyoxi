# oxi

**Point Claude Code at a markdown roadmap. Walk away. Come back to PRs.**

oxi turns `roadmap.md` into shipped code. Each tick: it picks the next planned task, spawns a `claude -p` session in a fresh git worktree, watches the worker open a PR, runs a second-model critic on the diff, and merges (or rejects) according to your policy. Every guardrail you'd want — budget hard-cap, killswitch, heartbeat reaper, ship-recovery, prompt-injection isolation, parameterized SQL, env-whitelisted subprocess — is on by default.

You write a ~70-line adapter that tells oxi about your project (repo, budget, plan tier). Core has zero strings naming any specific project. Forking is `pip install --pre oxi-core && oxi init`.

```bash
pip install --pre oxi-core      # beta — --pre still required while on 0.1.0b*
cd my-project
oxi init                        # 8-prompt wizard scaffolds your adapter
cd oxi-adapter-myproject && pip install -e .
oxi status                      # ✓ adapter loaded
oxi v3 tick --real-claude       # spends budget, ships PRs
```

The five-minute install runbook is at [`docs/runbooks/install.md`](docs/runbooks/install.md). Full operator manual at [`docs/manual/`](docs/manual/).

**Status:** beta (`0.1.0b1` on PyPI). The engine built most of itself today via dogfood — 59 PRs merged, $20.15 of dispatch spend, every safety rail proven in production. See [release notes](docs/release-notes/v0.1.0b1.md) for what's in this cut.

## What it does

```
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

## Install

```bash
pip install oxi-core oxi-adapter-reference
```

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

```
oxi-core/                        # ships as `oxi-core` on PyPI
├── src/oxi_core/
│   ├── adapter.py              # 10-method Protocol every fork implements
│   ├── cli.py                  # oxi, oxi init, oxi status, oxi v3 tick ...
│   ├── db.py                   # SQLite schema + append-only migrations
│   ├── planner.py              # roadmap → tasks
│   ├── prompts.py              # templated planner/dispatch/critic prompts
│   ├── wizard.py               # oxi init
│   └── v3/
│       ├── dispatch.py         # state-machine driver
│       ├── dispatch_invoke.py  # claude -p subprocess wrapper
│       ├── dispatch_pool.py    # host selection
│       ├── heartbeat.py        # reaper for stalled tasks
│       ├── ship_recovery.py    # rescue uncommitted work
│       ├── pr_watcher.py       # reconcile DB with GitHub PR state
│       ├── auto_merge.py       # critic-gated merge
│       ├── critic.py           # CriticBackend + ClaudeCriticBackend
│       ├── budget.py           # daily-cap enforcement
│       ├── deadman.py          # silence detector
│       ├── oauth_watch.py      # credential-expiry monitor
│       ├── cto_verdict.py      # structured /cto report parser
│       ├── notification.py     # pluggable notification backends
│       ├── brief.py            # daily recap markdown
│       ├── dashboard.py        # localhost HTTP dashboard
│       ├── engine_state.py     # killswitch + plan_tier
│       ├── kill.py             # killswitch file handling
│       ├── worktree_provision.py   # git worktree lifecycle
│       ├── github_client.py    # GitHubClient protocol + gh CLI impl
│       ├── tail_dispatch.py    # live-tail stream-json logs
│       ├── ingest_roadmap.py   # roadmap → fronts table
│       └── seed_from_roadmap.py    # auto-replenish queue
│
└── tests/                       # 860+ tests, fake claude + fake GitHub

adapters/
├── _reference/                 # ships as `oxi-adapter-reference`; drives sample-project
└── _template/                  # oxi init scaffolds from this

sample-project/                 # throwaway fixture for end-to-end tests
```

## Design principles

- **One binary, one command.** `oxi init`, then `oxi v3 tick`. No sprawling CLI surface.
- **Everything project-specific lives in an adapter.** Core has zero strings naming any specific project.
- **Fake the world in tests.** 860+ tests use `fake_claude.py` + `FakeGitHubClient`; no real Claude or GitHub contact in CI.
- **Atomic state transitions.** Every status update stamps `last_progress_at` in the same transaction. Reapers never trust `created_at`.
- **Protocols over implementations.** `CriticBackend`, `GitHubClient`, `NotificationBackend` are pluggable. Forks substitute any of them.
- **No premature abstraction.** Three similar lines beats one generic helper that handles three cases.

## Roadmap

See [`docs/roadmap.md`](docs/roadmap.md) for current items. Phase 1 (engine), Phase 2 (safety + dogfood), Phase 3 (operator polish) all shipped. Beta is live (`0.1.0b1`); next milestone is `0.2.0` for any breaking adapter-protocol changes. Release history at [`docs/release-notes/`](docs/release-notes/).

## Status

**Beta** (`0.1.0b1`). The engine has dogfooded itself end-to-end — 59 PRs merged in one day with the engine writing ~40 of them, $20.15 of dispatch spend that hit the daily-hard-cap rail exactly as designed, full first-fork install path verified against PyPI from a fresh venv. 860+ tests pass against a fake-claude + fake-GitHub harness.

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
