# Budget caps

oxi enforces hard spend limits so a runaway loop or misconfigured adapter cannot exceed what you authorize.

## How caps work

Every session's cost is tracked in the ledger as it runs. Before dispatching a new session, the engine checks today's total spend against two thresholds:

| Threshold | Behavior |
|---|---|
| `daily_soft_warn` | Log a warning; continue dispatch |
| `daily_hard_cap` | Halt dispatch immediately; set `budget_hard_stop` event |

Per-task ceilings abort individual sessions that exceed their limit mid-run:

| Ceiling | Applies to |
|---|---|
| `per_task_opus` | Sessions using Opus model |
| `per_task_sonnet` | Sessions using Sonnet model |

## Configuring caps

In your adapter:

```python
def budget(self) -> BudgetCaps:
    return BudgetCaps(
        daily_soft_warn=5.0,    # USD — warn above this
        daily_hard_cap=20.0,    # USD — halt above this
        per_task_opus=2.0,      # USD — abort Opus sessions over this
        per_task_sonnet=0.50,   # USD — abort Sonnet sessions over this
    )
```

After editing `adapter.py`, the new caps take effect on the next tick (no reinstall needed if installed with `-e`).

## Recommended starting caps

For an initial install where you are still calibrating typical per-task cost:

| Cap | Value |
|---|---|
| `daily_soft_warn` | $2.00 |
| `daily_hard_cap` | $5.00 |
| `per_task_opus` | $1.00 |
| `per_task_sonnet` | $0.25 |

Once you have run 10–20 tasks and know your typical cost per item, raise the caps to match your desired throughput.

## Checking today's spend

```bash
oxi status
```

The budget line shows current spend vs. thresholds:

```
  budget (today): $3.41 spent / $5.00 warn / $20.00 hard — warn
```

Status values:

| Status | Meaning |
|---|---|
| `ok` | Below soft-warn |
| `warn` | Between soft-warn and hard-cap |
| `HARD_STOP` | At or above hard-cap; dispatch is halted |

## Recovering from a hard-stop

1. Raise `daily_hard_cap` in `adapter.py`
2. Save the file (no reinstall needed if using `pip install -e .`)
3. Clear the hard-stop event — in the current alpha, delete and recreate `.oxi/oxi.db` or increase the cap and wait for the next calendar day (the ledger resets at midnight UTC)
4. Resume: `oxi v3 unkill && oxi v3 tick --real-claude`

A CLI surface for clearing hard-stop events without touching the DB is planned for a future release.

## Cost visibility

The `oxi brief` command includes a cost summary for the window:

```bash
oxi brief --hours 24
```

The dashboard also shows per-task cost in the task table.
