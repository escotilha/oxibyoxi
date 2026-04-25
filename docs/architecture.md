# Architecture

**Status:** living document. Updated as the engine evolves.

This file documents the internal layout of `oxi-core`. The 5-minute summary is in [`README.md`](../README.md); this is the long version with module-by-module annotation.

## Layout

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
└── tests/                       # 940+ tests, fake claude + fake GitHub

adapters/
├── _reference/                 # ships as `oxi-adapter-reference`; drives sample-project
└── _template/                  # oxi init scaffolds from this

sample-project/                 # throwaway fixture for end-to-end tests
```

## The adapter Protocol

Every fork writes a single class implementing the 10-method `Adapter` Protocol declared in `oxi_core.adapter`. Core has zero strings naming any specific project; everything project-specific (repo path, budget caps, plan tier, dispatch host, critic policy) lives in the adapter. See [`docs/adapter-protocol.md`](adapter-protocol.md) for the per-method contract.

## The tick loop

`oxi v3 tick` runs N iterations, each of which executes — in this order — `heartbeat.reap` → `auto_recover.run` → (if `--real-claude`) `dispatch.dispatch_one` → `pr_watcher.watch` → `auto_merge.run`. Each phase is independently retryable; the DB is the source of truth, in-memory state never carries between ticks.

## State model

Tasks live in the `task` table. Status transitions are atomic: every UPDATE is paired with an `event` row in the same transaction, and `last_progress_at` is stamped on every transition. Reapers (`heartbeat`, `auto_recover`) never trust `created_at` — they only look at `last_progress_at`.

Canonical statuses: `planned` → `dispatched` → (`merged` | `failed` | `abandoned`).

## Testing

The whole codebase is fake-able. `tests/fake_claude.py` impersonates `claude -p`'s stream-json output; `tests/fakes.py::FakeGitHubClient` impersonates `gh` for PR/check lookups. CI never contacts real Claude or real GitHub. Forks should follow the same discipline — write a fake before writing a feature that hits an external service.

See [`PLAN.md`](PLAN.md) Section 4 for the historical layout and Phase 1 scope.
