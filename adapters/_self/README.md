# oxi-adapter-self

Dogfood adapter — drives oxi operating on its own repo. **Internal package.** Not published to PyPI; not intended for forks.

## What this is for

After shipping `0.1.0a2`, the next integration test is `oxi` dispatching `claude -p` against `escotilha/oxi` itself to work the roadmap items. This adapter wires that loop up.

## Configuration

| Field | Value | Rationale |
|---|---|---|
| `instance_name` | `oxi-dogfood` | Distinct from `reference` so events are easy to filter. |
| `daily_soft_warn` | $5 | Early warning before the hard cap fires. |
| `daily_hard_cap` | $20 | Ceiling per Pierre's direction. Runaway loop can't exceed this. |
| `per_task_opus` | $2 | Per-task ceiling for Opus 4.7 sessions. |
| `per_task_sonnet` | $0.50 | Per-task ceiling for Sonnet sessions. |
| `github_repo` | `escotilha/oxi` | The repo oxi dispatches against. |
| `roadmap_location` | `docs/roadmap.md` | Where the dogfood polish items live. |
| `auto_merge` | `False` | Every PR requires Pierre's approval for now. Flip only after critic track record is established. |
| `plan_tier` | `20x` | Pierre's Max 20x plan. |
| `dispatch_host` | `local`, `max_concurrent=1` | Serial dispatch on the Mac Mini. No fan-out yet. |

## Use

```python
from oxi_adapter_self import SelfAdapter
from oxi_core.adapter import register_adapter

register_adapter(SelfAdapter(repo_root=Path("/path/to/oxi")))
```

Then `oxi v3 tick --real-claude --times 1` picks up the adapter and runs one engine tick.

## Off-limits

This adapter is explicitly **not** for running oxi against any other repo. Forks implementing their own adapter follow the reference adapter pattern, not this one. The budget caps and `auto_merge=False` here are tuned for Pierre's single-user dogfood loop.
