# oxi — User Manual

**Version:** 0.1.0a4 · **Status:** Alpha (first-fork-ready)

oxi is a forkable autonomous coding orchestrator. It reads a markdown roadmap, dispatches parallel Claude Code sessions to implement each item, opens pull requests, runs a critic review, and keeps a daily spend ledger — all in a single headless loop you can install in one command.

---

## What oxi does

1. **Reads your roadmap** — a `roadmap.md` with tiered items (`T0`, `T1`, …).
2. **Picks the next item** — Tier 0 before Tier 1, highest-priority first.
3. **Provisions a worktree** — isolated git workspace under `.oxi/worktrees/`.
4. **Spawns a Claude Code session** (`claude -p`) in that worktree with a focused brief.
5. **Waits for a PR** — the session commits, pushes, and opens a GitHub PR.
6. **Runs a critic pass** — a second model invocation reviews the diff.
7. **Optionally merges** — if `auto_merge=True` and critic approves, the PR merges.
8. **Records cost** — every session's spend is tracked against your budget caps.

You review PRs at step 6. The engine never touches `main` without your sign-off, or without an explicit `auto_merge=True` policy you configured.

---

## Who this is for

Solo engineers and small teams who want to ship roadmap items faster by having Claude implement them, with human review at PR time. oxi is **not** a chat assistant, a CI/CD system, or a multi-tenant SaaS — it is a local daemon you run against your own repo.

---

## Sections

- [**Install**](install.md) — from `pip install` to first PR in under ten minutes
- [**Roadmap format**](roadmap-format.md) — how to write items oxi can parse
- [**Adapter**](adapter.md) — configuring oxi for your project
- [**Running dispatch**](running-dispatch.md) — tick, kill, unkill, status
- [**Dashboard**](dashboard.md) — the localhost HTML dashboard
- [**Budget caps**](budget-caps.md) — daily limits and per-task ceilings
- [**Safety rails**](safety-rails.md) — auto_merge discipline, path isolation, env whitelist, SQL safety
- [**Dogfood loop**](dogfood-loop.md) — using oxi to ship oxi
- [**Doc lint**](doc-lint.md) — lychee + markdownlint: broken-link detection and style enforcement
- [**Rollback**](rollback.md) — stopping and reverting
- [**Troubleshooting**](troubleshooting.md) — common failure modes
