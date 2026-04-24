# Install

Zero to first PR in under ten minutes on a machine that has Python 3.11+ and the `gh` CLI installed.

## Prerequisites

| Tool | Minimum | Check |
|---|---|---|
| Python | 3.11 | `python --version` |
| pip | any | `pip --version` |
| git | any | `git --version` |
| gh CLI | 2.x | `gh --version` |
| Anthropic API key | — | `echo $ANTHROPIC_API_KEY` |

You need a GitHub repo and a local checkout of it. oxi opens PRs against that repo.

---

## Step 1 — Install oxi-core

oxi is in alpha. pip's default resolver ignores pre-releases, so pass `--pre` or pin the version:

```bash
pip install --pre oxi-core
# or pin explicitly:
pip install oxi-core==0.1.0a4
```

Verify:

```bash
oxi --version
# oxi 0.1.0a4
```

If `oxi` is not found after install, the pip `bin/` directory is not on `PATH`. Find it with:

```bash
python -m site --user-base
# then add <base>/bin to PATH
```

---

## Step 2 — Scaffold your adapter

An **adapter** is a ~70-line Python package that tells oxi about your project: where the repo lives, what GitHub slug to push PRs to, what budget caps to enforce, and which Claude plan tier you are on.

Run the 8-step wizard from your project root:

```bash
cd /path/to/your-project
oxi init
```

The wizard asks:

1. **Project name** — human label used in dashboard and briefs
2. **Adapter slug** — PyPI-safe package suffix, lowercase hyphens (e.g. `my-app`)
3. **GitHub repo** — `owner/name` slug (e.g. `acme/my-app`)
4. **Repo root** — absolute path; defaults to `$PWD`
5. **Roadmap location** — relative path inside the repo (default `roadmap.md`)
6. **Claude plan tier** — `standard`, `max_5x`, or `max_20x`
7. **Budget caps** — daily soft-warn, daily hard-cap, per-task Opus/Sonnet ceilings
8. **Dispatch policy** — max concurrent sessions; auto-merge toggle (default **off**)

Example session (press Enter to accept defaults):

```
oxi init — 8-step adapter scaffold
========================================

Project name: My App
Adapter slug [my-app]:
GitHub repo: acme/my-app
Repo root [/path/to/your-project]:
Roadmap location [roadmap.md]:
Claude plan tier [standard]:

Budget caps (USD):
  daily soft warn [5.0]:
  daily hard cap [20.0]:
  per-task Opus cap [2.0]:
  per-task Sonnet cap [0.50]:

Dispatch:
  max concurrent dispatches [3]:
  auto-merge approved PRs [y/N]:

oxi init: scaffolded 4 files into /path/to/your-project/oxi-adapter-my-app
```

---

## Step 3 — Install the adapter

```bash
cd oxi-adapter-my-app
pip install -e .
cd ..
```

The `-e` flag lets you edit `adapter.py` without reinstalling. The package declares an `oxi.adapters` entry-point so oxi discovers it automatically — no registration snippet needed.

---

## Step 4 — Confirm the adapter loads

```bash
oxi status
```

Expected output:

```
oxi 0.1.0a4
  instance:  My App
  plan tier: standard
  repo:      acme/my-app

  budget (today): $0.00 spent / $5.00 warn / $20.00 hard — ok

task counts by status:
  (no tasks)

recent events:
  (none)
```

If you see `oxi: no adapter registered`, confirm `pip install -e .` ran inside the adapter directory and that the same Python environment is active.

If you have two adapters installed, pin yours:

```bash
OXI_ADAPTER=oxi_adapter_my_app:Adapter oxi status
```

---

## Step 5 — Write your roadmap

See the [Roadmap format](roadmap-format.md) page for the required syntax.

Then verify the parser can read it:

```bash
oxi v3 plan --dry-run
```

Expected:

```
roadmap: /path/to/your-project/roadmap.md
parsed 3 item(s) — ok

  T0   T0-1  add a greet function
             a pure function that returns "hello, {name}"
  T0   T0-2  add a changelog stub
  T1   T1-1  expose greet in __all__
```

If the item count is 0, the roadmap format is wrong — check the [Roadmap format](roadmap-format.md) page.

---

## Step 6 — Smoke tick (no Claude spend)

A reconciliation-only tick reads the roadmap, seeds tasks, and runs maintenance passes without invoking Claude:

```bash
oxi v3 tick --times 1
```

Expected:

```
oxi v3 tick: 1 cycle(s)
cycle 1/1
  seed_from_roadmap: seeded 3 new task(s)
  heartbeat: 0 session(s) reaped
  pr_watcher: 0 PR(s) reconciled
  auto_merge: 0 PR(s) evaluated
tick complete
```

Run `oxi status` — task counts should show seeded items.

---

## Step 7 — Real dispatch (spends Claude budget)

> Before running: budget caps are real. The engine halts at `daily_hard_cap`. Start conservative. `auto_merge` is **off** by default — review every PR the engine opens before merging.

Set your API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Then dispatch:

```bash
oxi v3 tick --real-claude
```

oxi picks the highest-priority pending task, provisions a worktree, spawns a `claude -p` session, waits for the session to push and open a PR, and runs the critic review pass. Watch the PR appear in your GitHub repo.

If anything looks wrong, stop immediately:

```bash
oxi v3 kill --reason "stopping for review"
```

Resume when ready:

```bash
oxi v3 unkill
```

---

## Verification checklist

- [ ] `oxi --version` prints a version string
- [ ] `oxi status` shows your instance name, plan tier, and repo slug
- [ ] `oxi v3 plan --dry-run` reports the correct item count
- [ ] `oxi v3 tick --times 1` seeds tasks without error
- [ ] At least one PR opened by `oxi v3 tick --real-claude` is visible in GitHub

---

## Next steps

- [Adapter](adapter.md) — tune budget, concurrency, plan tier
- [Running dispatch](running-dispatch.md) — scheduling, kill/unkill, brief
- [Safety rails](safety-rails.md) — what oxi does to limit blast radius
