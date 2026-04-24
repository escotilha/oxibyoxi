# Dogfood loop

oxi ships itself — the engine's own roadmap is driven by oxi. This pattern (using oxi to develop oxi) validates the install experience from the operator's perspective and surfaces regressions immediately.

## What the dogfood loop does

1. The oxi repo contains a `roadmap.md` with open items for the engine itself
2. A dispatch tick picks the next T0 item (e.g., "add T0-3 test coverage for the planner")
3. A Claude session in `.oxi/worktrees/<tag>/` implements the item and opens a PR
4. The critic reviews the PR diff; the operator reviews and merges
5. The merged change is part of the next oxi release
6. New features land via this loop before being documented as stable

## Why this matters for operators

The dogfood loop means every oxi release has been exercised by the engine itself. If `oxi init` breaks, the engine cannot scaffold its own next feature. If the roadmap parser silently drops items, the planner seeds zero tasks and the loop stalls — visibly.

This is not a guarantee of quality, but it is a meaningful smoke test. Adapters built from the wizard template benefit from this because any template regression would also break the dogfood loop.

## Running oxi against the oxi repo

This is only relevant if you have cloned the oxi source repo and want to run the engine against itself. Most operators do not need this.

```bash
cd /path/to/oxi
# The self-adapter is at adapters/_reference/ (drives a toy sample project)
# For the actual dogfood loop, oxi uses a private adapter not included in the public repo
pip install -e oxi-core/
oxi status
```

## The pattern for your own project

Apply the same loop to your own project:

1. Keep `roadmap.md` updated with the next 10–15 items you want implemented
2. Run `oxi v3 tick --real-claude` on a schedule (e.g., morning and evening)
3. Review PRs as they open; merge the good ones
4. Add new roadmap items as merged ones close

The engine replenishes the task queue from the roadmap automatically — you only need to maintain the markdown file. Items that are merged are marked done; new items in the file are seeded on the next tick.

## Tier-0 absorb

When you add a new T0 item to the roadmap (a bug fix, a blocker), the engine pauses lower-tier dispatch for one tick after seeding it. This gives the T0 item priority without you having to manually reorder the queue.

To turn this off:

```python
def policy(self) -> DispatchPolicy:
    return DispatchPolicy(
        tier_zero_absorb=False,  # don't pause lower-tier on new T0 items
    )
```
