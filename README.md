# oxi

**Forkable autonomous coding orchestrator.** Reads a roadmap, dispatches parallel Claude Code sessions, opens PRs, gates merges through a critic, ships a daily brief. One binary, one command, project-agnostic.

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
└── tests/                       # 460+ tests, fake claude + fake GitHub

adapters/
├── _reference/                 # ships as `oxi-adapter-reference`; drives sample-project
└── _template/                  # oxi init scaffolds from this

sample-project/                 # throwaway fixture for end-to-end tests
```

## Design principles

- **One binary, one command.** `oxi init`, then `oxi v3 tick`. No sprawling CLI surface.
- **Everything project-specific lives in an adapter.** Core has zero strings naming any specific project.
- **Fake the world in tests.** 460+ tests use `fake_claude.py` + `FakeGitHubClient`; no real Claude or GitHub contact in CI.
- **Atomic state transitions.** Every status update stamps `last_progress_at` in the same transaction. Reapers never trust `created_at`.
- **Protocols over implementations.** `CriticBackend`, `GitHubClient`, `NotificationBackend` are pluggable. Forks substitute any of them.
- **No premature abstraction.** Three similar lines beats one generic helper that handles three cases.

## Roadmap

See [`docs/PLAN.md`](docs/PLAN.md). Phase 1 (engine + smoke test) and Phase 2 (safety rails) are complete. Phase 3 is in progress. The first published release will be `0.1.0-alpha`.

## Status

**Pre-alpha.** The engine runs; 460+ tests pass; the fake-claude end-to-end smoke test closes the full loop. Never dogfooded on a real repo — that's Phase 2 per the plan.

If you're willing to run an unproven orchestrator against your project: you can. Budget caps bound the damage at whatever you set `daily_hard_cap` to.

## License

MIT at `1.0`. Everything here is safe to fork, modify, redistribute.

## Contributing

Open an issue first. Don't open PRs yet — the engine's output quality isn't yet verified against a real dogfooding cycle, and unsolicited PRs would be reviewed on a best-effort basis.

## Anti-patterns

[`docs/anti-patterns.md`](docs/anti-patterns.md) documents nine constraints the project enforces via CI. Read it before filing a "why doesn't oxi do X" issue — X might be deliberately excluded.

## Acknowledgements

oxi's design is informed by a prior in-house orchestrator; the prior system's failure modes are documented in [`docs/origin-feature-gap-2026-04-24.md`](docs/origin-feature-gap-2026-04-24.md). No code, strings, or identifiers from that system cross into oxi — a CI-enforced forbidden-string list at `scripts/lint-for-leaks.sh` is the gate.
