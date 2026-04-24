# Install runbook — zero to first tick

New operators: follow every step in order. The whole flow takes under five minutes on a machine that already has Python 3.11+ and the `gh` CLI installed.

## Prerequisites

| Tool | Minimum version | Check |
|---|---|---|
| Python | 3.11 | `python --version` |
| pip | any recent | `pip --version` |
| git | any | `git --version` |
| gh CLI | 2.x | `gh --version` |
| Anthropic API key | — | set in env or `~/.config/anthropic/api_key` |

You need a GitHub repo and a local checkout of it. oxi ships PRs against that repo; it never touches unrelated repositories.

---

## Step 1 — install oxi-core

```bash
pip install oxi-core
```

Verify:

```bash
oxi --version
# oxi 0.1.0a2
```

If `oxi` is not found after install, your pip's `bin/` directory is not on `PATH`. Find it with `pip show -f oxi-core | grep Scripts` (Windows) or `python -m site --user-base` (macOS/Linux) and add `<base>/bin` to `PATH`.

---

## Step 2 — scaffold your adapter

An **adapter** is a ~70-line Python package that tells oxi about your project: where the repo lives, what GitHub slug to push PRs to, what budget caps to enforce, and which Claude plan tier you are on.

Run the 8-step wizard from your project's root directory:

```bash
cd /path/to/your-project
oxi init
```

The wizard asks:

1. **Project name** — human label used in dashboard and briefs (e.g. `My App`)
2. **Adapter slug** — PyPI-safe package suffix, lowercase hyphens (e.g. `my-app`)
3. **GitHub repo** — `owner/name` slug (e.g. `acme/my-app`)
4. **Repo root** — absolute path; defaults to `$PWD`
5. **Roadmap location** — relative path inside the repo (default `roadmap.md`)
6. **Claude plan tier** — `standard`, `max_5x`, or `max_20x`
7. **Budget caps** — daily soft-warn, daily hard-cap, per-task Opus/Sonnet ceilings
8. **Dispatch policy** — max concurrent sessions; auto-merge toggle (default **off**)

Example session (press Enter to accept bracketed defaults):

```
oxi init — 8-step adapter scaffold
========================================

Project name (what's your project called?): My App
Adapter slug (lowercase, hyphens; used in PyPI name) [my-app]:
GitHub repo (owner/name): acme/my-app
Repo root (absolute path to local checkout) [/path/to/your-project]:
Roadmap location (relative path inside the repo) [roadmap.md]:
Claude plan tier (e.g. standard, max_5x, max_20x) [standard]:

Budget caps (USD):
  daily soft warn [5.0]:
  daily hard cap [20.0]:
  per-task Opus cap [2.0]:
  per-task Sonnet cap [0.50]:

Dispatch:
  max concurrent dispatches [3]:
  auto-merge approved PRs (requires a trusted critic) [y/N]:

oxi init: scaffolded 4 files into /path/to/your-project/oxi-adapter-my-app
```

The wizard writes a ready-to-install Python package at `./oxi-adapter-<slug>/`.

---

## Step 3 — install the adapter

```bash
cd oxi-adapter-my-app
pip install -e .
cd ..
```

The `-e` (editable) flag lets you edit `adapter.py` without reinstalling. The package declares an `oxi.adapters` entry-point so oxi discovers it automatically — no registration snippet needed.

---

## Step 4 — confirm the adapter loads

```bash
oxi status
```

Expected output (values match what you entered in the wizard):

```
oxi 0.1.0a2
  instance:  My App
  plan tier: standard
  repo:      acme/my-app

  budget (today): $0.00 spent / $5.00 warn / $20.00 hard — ok

task counts by status:
  (no tasks)

recent events:
  (none)
```

If you see `oxi: no adapter registered`, confirm `pip install -e .` ran inside the adapter directory and that your current Python environment matches the one where you installed `oxi-core`.

If you have two adapters installed (e.g. the reference adapter and your own), pin yours:

```bash
OXI_ADAPTER=oxi_adapter_my_app:Adapter oxi status
```

---

## Step 5 — write your roadmap

oxi parses a markdown roadmap. The format is strict:

- Section headers must be `## Tier N` (N = 0, 1, 2, …)
- Items must be `**T<tier>-<n> · <title>**` (note the `·` middle-dot separator)
- Optional italic subtitle on the next line: `_one sentence description_`

Example `roadmap.md`:

```markdown
## Tier 0

**T0-1 · add a greet function**
_a pure function that returns "hello, {name}"_

**T0-2 · add a changelog stub**

## Tier 1

**T1-1 · expose greet in __all__**
```

Tier 0 items dispatch before lower tiers. Keep 10–15 open items at a time — the planner replenishes automatically as items ship.

Verify the parser can read your roadmap before running a full tick:

```bash
oxi v3 plan --dry-run
```

Expected output:

```
roadmap: /path/to/your-project/roadmap.md
parsed 3 item(s) — ok

  T0   T0-1  add a greet function
             a pure function that returns "hello, {name}"
  T0   T0-2  add a changelog stub
  T1   T1-1  expose greet in __all__
```

If the item count is 0 or lower than expected, check:

- Section headers use `## Tier N`, not `## T0` or `## Tier-0`
- Item lines use `**T0-1 · title**` (middle-dot `·`, not hyphen `-` or dash `—`)
- No item appears before the first `## Tier N` heading

---

## Step 6 — run a smoke tick (no Claude spend)

A reconciliation-only tick reads the roadmap, seeds tasks into the DB, and runs all maintenance passes (heartbeat, PR watcher, budget check) without invoking Claude:

```bash
oxi v3 tick --times 1
```

Expected output:

```
oxi v3 tick: 1 cycle(s)
cycle 1/1
  seed_from_roadmap: seeded 3 new task(s)
  heartbeat: 0 session(s) reaped
  pr_watcher: 0 PR(s) reconciled
  auto_merge: 0 PR(s) evaluated
tick complete
```

After this succeeds, run `oxi status` again — task counts should reflect the seeded items.

---

## Step 7 — run a real dispatch tick (spends Claude budget)

> **Read before continuing:**
>
> - Budget caps are enforced. The engine halts at `daily_hard_cap`. Start with conservative defaults (e.g. $5/day hard cap, $0.50 per task).
> - `auto_merge` is **off** by default. Review every PR the engine opens before merging — the critic is a second model pass, not a guarantee.
> - If something goes wrong, `oxi v3 kill --reason "stopping for review"` immediately halts dispatch. Resume with `oxi v3 unkill`.

Confirm your Anthropic API key is set:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Then dispatch:

```bash
oxi v3 tick --real-claude
```

oxi will:

1. Pick the highest-priority pending task (Tier 0 first)
2. Provision a git worktree in `<repo>/.oxi/worktrees/`
3. Spawn a `claude -p` session in that worktree
4. Wait for the session to commit, push, and open a PR
5. Run the critic review pass against the PR diff
6. (If `auto_merge=True` and critic approves) merge the PR

Watch the PR appear in your GitHub repo. Once you have seen at least one PR open and reviewed it, you can decide whether to enable `auto_merge` in your adapter.

---

## Adapter template — annotated

The file the wizard writes at `src/oxi_adapter_<slug>/adapter.py` implements the full 10-method `Adapter` protocol. Fields marked `# TUNE` are the ones most operators change after the initial scaffold:

```python
# TUNE: swap for a remote DispatchHost if dispatching off your laptop
def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
    oxi_dir = _REPO_ROOT / _OXI_DIR_NAME
    return (
        DispatchHost(
            name="local",
            ssh_alias=None,        # None = local; set an SSH alias for remote
            max_concurrent=3,      # TUNE: raise if the host is powerful
            worktree_root=str(oxi_dir / "worktrees"),
        ),
    )

# TUNE: tighten caps when first testing; loosen once confident
def budget(self) -> BudgetCaps:
    return BudgetCaps(
        daily_soft_warn=5.0,       # log a warning above this
        daily_hard_cap=20.0,       # halt dispatch above this
        per_task_opus=2.0,         # ceiling per Opus-based task
        per_task_sonnet=0.50,      # ceiling per Sonnet-based task
    )

# TUNE: set to True only after watching the critic reject a bad PR
def policy(self) -> DispatchPolicy:
    return DispatchPolicy(
        auto_merge=False,
    )
```

The full protocol reference is in `oxi-core/src/oxi_core/adapter.py`. The reference adapter at `adapters/_reference/` in the oxi source tree is a complete working example.

---

## Verification checklist

After following this runbook, confirm each item:

- [ ] `oxi --version` prints a version string
- [ ] `oxi status` shows your instance name, plan tier, and repo slug
- [ ] `oxi v3 plan --dry-run` reports the correct item count from your roadmap
- [ ] `oxi v3 tick --times 1` runs without error and shows seeded task count
- [ ] At least one PR opened by `oxi v3 tick --real-claude` is visible in your GitHub repo

---

## Common problems

### `oxi: no adapter registered`

The adapter package is not installed in the same Python environment as `oxi-core`. Run `which python` and `which oxi` — they must resolve to the same environment. If you are using a virtualenv, activate it before `pip install -e .`.

### `oxi: multiple adapters installed`

Two packages declare an `oxi.adapters` entry-point. Pin the one you want:

```bash
OXI_ADAPTER=oxi_adapter_my_app:Adapter oxi status
```

### `roadmap not found`

`adapter.roadmap_location()` returns a path that does not exist relative to `adapter.paths().repo_root`. Check the wizard answers you entered, or edit `adapter.py` directly. Then re-run `oxi v3 plan --dry-run` to confirm.

### Zero tasks after tick

Run `oxi v3 plan --dry-run` and check the item count. If it is 0, the roadmap format is wrong — see [Step 5](#step-5--write-your-roadmap). If it is non-zero but tasks are not seeding, check `oxi status` for a `budget_hard_stop` event that may be blocking dispatch.

### Budget hard-stop

`oxi status` will show `✗ budget … hard — HARD_STOP`. Increase `daily_hard_cap` in your adapter and rerun `pip install -e .`. Then clear the stop event — this CLI surface is coming in a future release; for now edit the DB directly or delete and recreate `.oxi/oxi.db` (tasks will re-seed from the roadmap on the next tick).

### Engine went quiet (deadman alert)

If the engine has not dispatched in N minutes, the deadman module fires a `NotificationBackend` callback. The default backend logs to stderr. Wire your own backend (Slack, email) by implementing `NotificationBackend` in your adapter.

---

## Alpha caveats

- **Auto-merge off by default.** The wizard scaffolds `auto_merge=False`. Keep it off until you have watched the critic reject at least one obviously-bad PR.
- **Prompt injection is a known class.** oxi embeds roadmap text and PR diffs into prompts. Treat `roadmap.md` with the same trust as source code.
- **Dashboard is localhost-only.** `oxi dashboard` binds to `127.0.0.1:8765`. Widening the bind requires a reverse proxy with authentication; oxi has no built-in auth.
- **Pre-alpha quality.** Budget caps bound damage to whatever you set `daily_hard_cap` to. Run `oxi v3 kill` if anything looks wrong; resume with `oxi v3 unkill`.

---

## Next steps

- `oxi brief` — print a markdown recap of the last 24 hours of activity
- `oxi dashboard` — open the localhost HTML dashboard
- `oxi v3 kill / unkill` — pause and resume the engine
- [`docs/adapter-protocol.md`](../adapter-protocol.md) — full protocol reference
- [`docs/first-fork-checklist.md`](../first-fork-checklist.md) — what "first-fork-ready" means and current gap status
