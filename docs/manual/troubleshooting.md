# Troubleshooting

---

## `oxi: no adapter registered`

**Cause:** The adapter package is not installed in the same Python environment as `oxi-core`, or the entry-point was not declared correctly.

**Fix:**

```bash
# Confirm both commands use the same Python
which python
which oxi

# Both must resolve to the same environment. If using a virtualenv, activate it first.
# Then reinstall the adapter:
cd oxi-adapter-my-app
pip install -e .

# Verify:
oxi status
```

If you have a virtualenv, activate it before every `pip install -e .` and before running `oxi`.

---

## `oxi: multiple adapters installed`

**Cause:** Two packages declare `oxi.adapters` entry-points in the same virtualenv.

**Fix:** Pin the adapter you want:

```bash
OXI_ADAPTER=oxi_adapter_my_app:Adapter oxi status
```

Or uninstall the unwanted adapter:

```bash
pip uninstall oxi-adapter-other-thing
```

---

## `roadmap not found`

**Cause:** `adapter.roadmap_location()` returns a path that does not exist relative to `adapter.paths().repo_root`.

**Fix:**

```bash
# Check what your adapter returns:
python -c "
from oxi_adapter_my_app import Adapter
from pathlib import Path
a = Adapter()
root = Path(a.paths().repo_root)
loc = a.roadmap_location()
p = root / loc
print(f'repo_root: {root}')
print(f'roadmap_location: {loc}')
print(f'resolved: {p}')
print(f'exists: {p.exists()}')
"
```

Edit `adapter.py` to correct the path. Then verify:

```bash
oxi v3 plan --dry-run
```

---

## Zero tasks after tick

**Cause A:** Roadmap format is wrong — parser found no matching items.

**Fix:** Run `oxi v3 plan --dry-run` and check the item count. If it is 0, see the [Roadmap format](roadmap-format.md) page.

**Cause B:** Budget hard-stop is blocking dispatch.

**Fix:** Run `oxi status` and look for `HARD_STOP` in the budget line. See [Budget caps — recovering from a hard-stop](budget-caps.md#recovering-from-a-hard-stop).

---

## Task stuck in `dispatched`

**Cause:** The Claude session is still running, or it exited without pushing a PR.

**Check:**

```bash
oxi status
# Look at task counts and recent events

# List worktrees to see active sessions:
git worktree list
```

**Fix:** The heartbeat pass reaps sessions that exceed the configured timeout. Run another tick:

```bash
oxi v3 tick --times 1
```

If the worktree is stale and the session clearly exited:

```bash
# Remove the stale worktree manually:
git worktree remove .oxi/worktrees/<tag> --force
# The heartbeat will transition the task to FAILED on the next tick
oxi v3 tick --times 1
```

---

## Budget hard-stop immediately after raising the cap

**Cause:** The new cap took effect but the `budget_hard_stop` event from the previous run is still in the ledger. The engine reads the event and halts again.

**Fix:** In the current alpha, the cleanest resolution is to wait for the next calendar day (the ledger resets at midnight UTC), or delete `.oxi/oxi.db` and re-seed from the roadmap:

```bash
rm .oxi/oxi.db
oxi v3 tick --times 1   # reconciliation-only, re-seeds tasks
oxi v3 tick --real-claude
```

---

## `oxi` not found after install

**Cause:** pip's `bin/` directory is not on `PATH`.

**Fix:**

```bash
# Find the bin directory:
python -m site --user-base
# Add <user-base>/bin to PATH in your shell profile

# Or install in a virtualenv and activate it:
python -m venv .venv
source .venv/bin/activate
pip install --pre oxi-core
oxi --version
```

---

## Dashboard shows stale data

**Cause:** The dashboard reads from the SQLite DB; if the engine is running in another terminal, data may be a tick behind.

**Fix:** Refresh the browser. The dashboard does not auto-refresh in the current alpha.

---

## `oxi v3 tick --real-claude` exits immediately with no output

**Cause:** `ANTHROPIC_API_KEY` is not set, or the killswitch is active.

**Check:**

```bash
echo $ANTHROPIC_API_KEY   # must be non-empty
oxi status                # look for killswitch_set in recent events
```

**Fix:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
oxi v3 unkill             # if killswitch was set
oxi v3 tick --real-claude
```

---

## Engine shipped a PR I did not want

**Cause:** The roadmap item was ambiguous, or the Claude session interpreted it differently than intended.

**Fix:**

1. Close or revert the PR on GitHub
2. Add a more specific subtitle to the roadmap item to guide the next dispatch
3. If the item should not be dispatched at all, remove it from `roadmap.md`

For subtle regressions the critic did not catch, review [Safety rails — auto_merge discipline](safety-rails.md#auto_merge-discipline) and consider keeping `auto_merge=False` until you have higher confidence.
