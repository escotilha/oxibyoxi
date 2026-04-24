# Rollback

This page covers how to stop the engine, undo a bad PR, and recover from a broken state.

---

## Stop the engine immediately

```bash
oxi v3 kill --reason "something looks wrong"
```

This creates a killswitch file. The engine will not dispatch new sessions while the killswitch is set. In-flight sessions finish naturally (their PRs open as normal); no new dispatches start.

Verify the engine is stopped:

```bash
oxi status
# recent events will show: killswitch_set
```

When you are ready to resume:

```bash
oxi v3 unkill
```

---

## Revert a bad PR

oxi opens PRs from worktree branches. If a PR merged something you want to revert:

```bash
# Find the PR number in oxi status or the dashboard
gh pr list --repo acme/my-app --state merged

# Revert the merge commit
git revert -m 1 <merge-commit-sha>
git push origin main
```

Or use the GitHub UI: open the PR → "Revert" button.

The task record in oxi's DB is not automatically updated when you revert a PR. The item will not be re-dispatched unless you manually reset its status — a CLI surface for this is planned for a future release.

---

## Recover from a hard budget stop

`oxi status` shows `✗ budget … HARD_STOP`:

1. Raise `daily_hard_cap` in `adapter.py`
2. The new cap takes effect on the next invocation (no reinstall needed if installed with `-e`)
3. Clear the stop: in the current alpha, the simplest path is to wait for the next calendar day (the ledger resets at midnight UTC) or delete `.oxi/oxi.db` (tasks will re-seed from the roadmap)
4. Resume: `oxi v3 tick --real-claude`

---

## Roll back oxi-core itself

To downgrade oxi-core to a previous version:

```bash
pip install oxi-core==0.1.0a3
oxi --version   # confirm downgrade
oxi status      # confirm adapter still loads
```

If the DB schema changed between versions, a downgrade may fail. In that case:

```bash
# Wipe state (tasks re-seed from the roadmap on the next tick)
rm .oxi/oxi.db
oxi v3 tick --times 1   # reconciliation-only, re-seeds from roadmap
```

---

## Wipe all engine state

If you want to start fresh (e.g., you reorganized the roadmap significantly):

```bash
oxi v3 kill --reason "resetting state"
rm .oxi/oxi.db
# optionally remove worktrees from stale dispatches:
git worktree list | grep .oxi/worktrees
git worktree remove .oxi/worktrees/<tag>
oxi v3 tick --times 1   # re-seeds from roadmap
oxi v3 unkill
```

Tasks in `merged` status are re-seeded on the next tick if they still appear in the roadmap. Remove shipped items from `roadmap.md` before wiping the DB to avoid re-dispatching them.

---

## Alpha caveat

Rollback automation (`scripts/rollback.sh`) is a planned deliverable. In the current alpha, the steps above are manual. Anti-pattern #7 requires that every release ship a one-command rollback — that tooling will land before the stable 1.0 release.
