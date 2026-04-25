# oxi

**Point Claude Code at a markdown roadmap. Walk away. Come back to PRs.**

oxi turns `roadmap.md` into shipped code. Each tick, it picks the next planned task, spawns a `claude -p` session in a fresh git worktree, watches the worker open a PR, runs a second-model critic on the diff, and merges (or rejects) according to your policy. Every guardrail you'd want — budget hard-cap, killswitch, heartbeat reaper, ship-recovery, prompt-injection isolation, parameterized SQL, env-whitelisted subprocess — is on by default.

You write a ~70-line adapter that tells oxi about your project (repo, budget, plan tier). Core has zero strings naming any specific project. Forking is `pip install --pre oxi-core && oxi init`.

## Install

```bash
pip install --pre oxi-core      # alpha — --pre is required
cd my-project
oxi init                        # 8-prompt wizard scaffolds your adapter
cd oxi-adapter-myproject && pip install -e .
oxi status                      # ✓ adapter loaded
oxi v3 tick --real-claude       # spends budget, ships PRs
```

The five-minute install runbook is at [install](manual/install.md). Full operator manual at [manual/](manual/index.md).

## Status

Beta (`0.1.0b1` on PyPI). The engine built most of itself today via dogfood — 59 PRs merged, $20.15 of dispatch spend, every safety rail proven in production. See [release notes](release-notes/index.md) for what's in this cut.

## What it does

```text
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  roadmap │→→→│  planner │→→→│ dispatch │→→→│pr_watcher│→→→│auto_merge│
│   .md    │   │          │   │ claude -p│   │          │   │  critic  │
└──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
                    ↑              ↓              ↓              ↓
                    └─────── fronts, heartbeat, ship_recovery ───┘
```

## Where to go next

- New to oxi? Start at [Install](manual/install.md), then [Adapter](manual/adapter.md).
- Curious about safety? See [Safety rails](manual/safety-rails.md).
- Want to know how it works? See [Architecture](architecture.md).
- Forking your own? See [First-fork checklist](first-fork-checklist.md).
