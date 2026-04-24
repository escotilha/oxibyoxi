# Running dispatch

## CLI reference

```
oxi                           print banner + version
oxi status                    task counts, budget, recent events
oxi v3 tick [--times N]       run N engine cycles
oxi v3 tick --real-claude     dispatch + critic (spends budget)
oxi v3 plan --dry-run         parse roadmap, print items, no DB write
oxi v3 kill [--reason R]      set the killswitch (pauses dispatch)
oxi v3 unkill                 clear the killswitch (resumes dispatch)
oxi brief [--hours N]         print markdown recap to stdout
oxi brief --write             write recap to adapter.paths().brief_path
oxi dashboard                 start localhost HTML dashboard
```

---

## Tick modes

### Reconciliation-only (default)

Runs maintenance passes — seeds roadmap items into the DB, reaps stalled tasks, watches PR status — without invoking Claude:

```bash
oxi v3 tick --times 1
```

Safe to run any time. No API key required. No budget spend. Use to verify the roadmap seeded correctly before committing to real dispatch.

### Real dispatch

Adds dispatch + critic-gated auto_merge to each cycle:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
oxi v3 tick --real-claude
```

One cycle does:

1. `seed_from_roadmap` — create tasks for any new roadmap items
2. `heartbeat` — reap stalled sessions, transition timed-out tasks
3. `dispatch_one` — pick the highest-priority pending task, provision a worktree, spawn `claude -p`
4. `pr_watcher` — check GitHub PR status, transition tasks to `merged` or `failed`
5. `auto_merge` — if `auto_merge=True`, run the critic and merge approved PRs

Run multiple cycles:

```bash
oxi v3 tick --real-claude --times 10
```

The engine stops early if the killswitch is set or the budget hard-cap is reached.

---

## Monitoring a running tick

While a tick is running, `oxi status` in another terminal shows live task state:

```
oxi 0.1.0a4
  instance:  My App
  plan tier: standard
  repo:      acme/my-app

  budget (today): $3.41 spent / $5.00 warn / $20.00 hard — warn

task counts by status:
  dispatched    2
  pending       5
  merged        3

recent events:
  2026-04-24T12:01:05  dispatch_started          task#7
  2026-04-24T12:00:52  dispatch_started          task#6
  2026-04-24T11:58:03  pr_merged                 task#5
```

---

## Killswitch

Stop all dispatch immediately without losing task state:

```bash
oxi v3 kill --reason "reviewing PRs before continuing"
```

The engine will not dispatch new sessions while the killswitch is set. In-flight sessions (already running `claude -p`) are not interrupted — they finish naturally and their PRs open as normal; only new dispatches are blocked.

Resume:

```bash
oxi v3 unkill
```

If `oxi status` shows a `budget_hard_stop` event, you need to raise `daily_hard_cap` in your adapter and reinstall (`pip install -e .`) before running unkill.

---

## Brief

The brief is a markdown recap of engine activity over the last N hours:

```bash
oxi brief             # print to stdout
oxi brief --hours 48  # 48-hour window
oxi brief --write     # write to adapter.paths().brief_path
```

Useful for reviewing what the engine shipped overnight before starting your day.

---

## Typical daily workflow

```bash
# Morning: review what shipped overnight
oxi brief
# Open the dashboard
oxi dashboard

# After reviewing PRs, resume dispatch
oxi v3 tick --real-claude --times 20

# If you need to stop to review a PR mid-run
oxi v3 kill --reason "reviewing PR #42"
# ... review ...
oxi v3 unkill
oxi v3 tick --real-claude --times 5
```
