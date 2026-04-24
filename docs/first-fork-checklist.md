# First-fork readiness

What a fresh operator needs to adopt oxi on their own project. This is the gap list between "alpha shipped" (today) and "someone else can clone and use it unassisted."

## Terminology

- **Operator** — a developer who wants oxi to drive claude against *their* repo
- **Adapter** — the fork's customization point: a Python package implementing `oxi_core.adapter.Adapter`
- **Fork** — has two meanings. Either (a) a git fork of oxi itself (needed only if you want to modify the engine) or (b) an independent project that *uses* oxi via PyPI and supplies its own adapter. **Most users want (b), not (a).** The rest of this document assumes (b).

## Install path — what works today

The happy path a new operator should be able to do:

```bash
pip install oxi-core==0.1.0a2
cd my-project
oxi init                     # wizard — scaffolds oxi-adapter-myproject/
cd oxi-adapter-myproject
pip install -e .
cd ..
python -c "from oxi_adapter_myproject import MyAdapter; from oxi_core.adapter import register_adapter; from pathlib import Path; register_adapter(MyAdapter(repo_root=Path.cwd()))"
oxi status                    # confirms adapter loads
oxi v3 tick                   # reconciliation-only, no Claude
export ANTHROPIC_API_KEY=sk-ant-...
oxi v3 tick --real-claude     # spends budget, dispatches a task
```

This is **partially working today**. See gaps below.

## Status by layer

### ✓ Shipped

- PyPI packages `oxi-core==0.1.0a2` and `oxi-adapter-reference==0.1.0a2`
- `oxi init` wizard scaffolds a working adapter (8 prompts, writes 4 files)
- Adapter protocol documented (`docs/adapter-protocol.md`)
- Security rails documented (`SECURITY.md`): env whitelist, argv-form subprocess, parameterized SQL, HTML escaping, process-group isolation
- CI: gitleaks, pip-audit, lint-for-leaks, ruff, pytest matrix on py3.11+3.12
- Dashboard on `127.0.0.1:8765` with XSS-safe rendering

### ✗ Gaps blocking first fork

These must land before you hand the URL to someone else.

#### BLOCKER-1 — adapter registration is not ergonomic

After `pip install -e .` on the scaffolded adapter, the operator still has to write a Python snippet to register it before any `oxi` command works. That's a cliff.

Options (pick one):
- **(A) Entry-point discovery** — adapters declare `[project.entry-points."oxi.adapters"]` in pyproject.toml; `oxi_core._require_adapter` loads the first registered one automatically. 30 lines of code. Forks with multiple adapters installed get a clear error.
- **(B) `OXI_ADAPTER` env var** — `OXI_ADAPTER=oxi_adapter_myproject:MyAdapter oxi status`. Simpler, less magical, but operators have to remember the env var.
- **(C) Both** — entry point is the default; env var overrides.

Recommendation: (C). The env-var escape hatch is cheap insurance.

#### BLOCKER-2 — `docs/runbooks/install.md` doesn't exist

T0-1 on the roadmap. Single-page walkthrough from `pip install` → first successful `oxi v3 tick`. Without it, any new operator is guessing.

Currently PR #48 (rollback runbook) is in review but the install runbook is still planned. **Must ship before first fork.**

#### BLOCKER-3 — `docs/roadmap.md` format is silently picky

Discovered during dogfood: the planner expects `## Tier N` + `**TID · title**` with a `·` separator. A fresh operator writing their own roadmap will produce `## T0-1 — my task` (natural markdown) and seed zero tasks with no error.

Fix options:
- Document the format prominently in `oxi init` output and in `docs/adapter-protocol.md`
- OR accept both `**TID · title**` and `**TID — title**` in the parser (one extra regex branch)
- Add a `oxi v3 plan --dry-run` subcommand that parses the roadmap and reports what it found, so operators can self-diagnose

Recommendation: both (document + dry-run). The parser fork is minor but easy to implement; the dry-run is more important.

#### BLOCKER-4 — `oxi` CLI doesn't plumb the adapter

When an operator types `oxi v3 tick` after installing their adapter, the CLI says "no adapter registered" and exits. This is today's reality — the dogfood loop solves it with `scripts/dogfood-tick.py` which does the registration manually. That script is oxi-internal; forks don't get it.

Solution falls out of BLOCKER-1 (entry-point discovery). Once `cmd_status` and `cmd_tick` can auto-load the adapter from installed packages, the CLI works standalone.

### ⚠ Would-be-nice but not blocking

- **T1-3 status --json** — polish, operators can live without it
- **T1-5 ANSI colors** — polish
- **T1-6 dashboard event tail** — polish
- **T1-14 ledger_events** — internal refactor, no user-visible effect
- **T2-8 query_helpers**, **T2-9 os.fspath**, **T2-10 pytest-timeout** — internal cleanup
- **A public "compatible skills" catalog** — eventually forks will want to share skills. Not needed for first fork; needed before many-forks.

### ⚠ Alpha caveats to disclose up front

These aren't gaps but operators must be told about them in the install runbook:

- `auto_merge=False` should stay off until the operator has watched the critic reject at least one obviously-bad PR. Shipping `auto_merge=True` in their adapter before that is asking to have a bad PR merged to main.
- Budget caps (`daily_hard_cap`, `per_task_opus`) are real — the engine halts at the hard cap. Start conservative.
- Dashboard is localhost-only. Widening the bind requires a reverse proxy with auth; oxi has no built-in auth.
- Prompt injection from roadmap or PR diffs is a known unfixable class. Treat `roadmap.md` as source code, review changes to it.

## Proposed path to first-fork-ready

In priority order, these four items close the gap:

1. **T0-101 — adapter entry-point discovery** (new roadmap item)
   Auto-load registered adapters in `_require_adapter`. ~30 lines + tests. Removes BLOCKER-1 and BLOCKER-4.

2. **T0-1 — install runbook** (already on roadmap)
   Write `docs/runbooks/install.md`. The `oxi init` → `pip install -e .` → `export ANTHROPIC_API_KEY` → `oxi v3 tick --real-claude` walkthrough.

3. **T0-102 — `oxi v3 plan --dry-run`** (new roadmap item)
   Parse the roadmap, report what was found, don't touch the DB. Surfaces the format-pickiness (BLOCKER-3) at operator level rather than as silent seed=0.

4. **T0-103 — first-fork smoke test in CI** (new roadmap item)
   A CI job that: `pip install oxi-core` (from the wheel built in the same CI run), runs `oxi init` with scripted answers, then walks the happy path. Catches any regression in the install experience.

After those four ship and the operator can clone a fresh repo and get to `oxi v3 tick --real-claude` in under 10 minutes, oxi is first-fork-ready.

## What to do right now (Pierre's call)

Option A — **Ship now, document the gaps.** Publish as `0.1.0a2-alpha-preview` with this checklist prominently linked. Friendly early adopters walk the sharp edges with you and report.

Option B — **Ship after the 4 items.** 3-5 more dispatch cycles on the dogfood loop. Another week.

Option C — **Ship a limited invite now, 4 items in parallel.** Two or three known operators, unlisted install runbook, you collect feedback while the engine ships the fixes for itself.

Recommendation: **Option C**. Uses the dogfood loop (which is working) to close the install-ergonomics gap, puts the engine in two more hands, and you keep the `auto_merge=False` + review-every-PR discipline as the guardrail.
