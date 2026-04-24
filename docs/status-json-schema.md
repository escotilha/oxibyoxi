# oxi status --json schema

`oxi status --json` (and the alias `oxi v3 status --json`) emits a
single JSON object to stdout. This document is the authoritative schema
reference for `schema_version: 1`.

Consumers should check `schema_version` before reading any other field.
A bump in `schema_version` signals a backward-incompatible change (key
removed or type changed). New keys added to an existing version are
always backward-compatible — consumers must tolerate unknown keys.

---

## Invocation

```sh
# Machine-readable snapshot
oxi status --json

# Via alias
oxi v3 status --json

# Pipeline example — check for hard-stopped budget
oxi status --json | python -c "
import json, sys
d = json.load(sys.stdin)
if d['budget']['verdict'] == 'hard_stop':
    sys.exit(1)
"
```

---

## Top-level object

| Field            | Type    | Description                                              |
|------------------|---------|----------------------------------------------------------|
| `schema_version` | integer | Schema version. Currently `1`.                           |
| `generated_at`   | string  | UTC timestamp when the payload was built. ISO 8601: `YYYY-MM-DDTHH:MM:SSZ`. |
| `instance`       | string  | `adapter.naming().instance_name`.                        |
| `plan_tier`      | string  | `adapter.plan_tier()` — e.g. `"standard"`, `"max_20x"`. |
| `repo`           | string  | `adapter.github_repo()` — e.g. `"owner/repo"`.          |
| `task_counts`    | object  | Task count grouped by status. See [task_counts](#task_counts). |
| `tasks`          | array   | Most-recently-updated tasks (up to 50). See [tasks](#tasks). |
| `budget`         | object  | Budget state snapshot. See [budget](#budget).            |
| `heartbeat`      | object  | Orphan-reap snapshot. See [heartbeat](#heartbeat).       |
| `killswitch`     | object  | Killswitch file state. See [killswitch](#killswitch).    |

---

## task_counts

An object mapping each task status to its count. Only statuses that
have at least one task are included — an empty DB yields `{}`.

```json
{
  "task_counts": {
    "planned": 3,
    "dispatched": 1,
    "merged": 12,
    "failed": 0
  }
}
```

Known status values: `planned`, `dispatched`, `merged`, `failed`,
`abandoned`. Forks may introduce additional statuses.

---

## tasks

An array of task objects, ordered by `updated_at` descending, limited
to 50 entries (the most recently updated tasks). Use the DB directly
for deeper history.

| Field              | Type             | Description                                  |
|--------------------|------------------|----------------------------------------------|
| `identifier`       | string           | Stable roadmap key, e.g. `"T1-3"`.           |
| `tier`             | integer          | Roadmap tier (0, 1, 2, …).                   |
| `title`            | string           | Human-readable title.                        |
| `status`           | string           | Current task status.                         |
| `pr_number`        | integer or null  | GitHub PR number, or `null` if none opened.  |
| `cost_usd`         | number           | Accumulated spend in USD.                    |
| `last_progress_at` | string or null   | UTC timestamp of last progress beacon.       |
| `dispatched_at`    | string or null   | UTC timestamp when task entered `dispatched`. |
| `merged_at`        | string or null   | UTC timestamp when task was merged.          |
| `failed_at`        | string or null   | UTC timestamp when task entered `failed`.    |
| `failure_reason`   | string or null   | Short failure reason, or `null`.             |
| `created_at`       | string           | UTC timestamp of row creation.               |
| `updated_at`       | string           | UTC timestamp of last row update.            |

Timestamps use SQLite's native format: `"YYYY-MM-DD HH:MM:SS"` (UTC,
space-separated). Future versions may normalise to ISO 8601 — consumers
should accept both.

---

## budget

Snapshot of the engine's budget state at the time of the request.

| Field                | Type   | Description                                          |
|----------------------|--------|------------------------------------------------------|
| `verdict`            | string | `"ok"`, `"warn"`, or `"hard_stop"`.                  |
| `today_spend_usd`    | number | Total USD spent in the current UTC day.              |
| `daily_soft_warn_usd`| number | Soft-warn threshold from `adapter.budget()`.         |
| `daily_hard_cap_usd` | number | Hard-cap from `adapter.budget()`.                    |
| `window_start`       | string | Start of the current UTC day. ISO 8601: `YYYY-MM-DDTHH:MM:SSZ`. |

`verdict` semantics:

- `"ok"` — under the soft-warn threshold. Engine is spending normally.
- `"warn"` — between soft-warn and hard-cap. A `budget_soft_warn` event
  has been emitted to the ledger.
- `"hard_stop"` — at or over the hard cap. The engine will not initiate
  new Claude-spending operations until the next UTC day or until an
  operator calls `oxi v3 unkill` (or clears budget events manually).

---

## heartbeat

A structural snapshot of how the heartbeat reaper sees the DB. These
counts reflect the current DB state; they do NOT run an actual reap
pass (no DB writes).

| Field                 | Type    | Description                                          |
|-----------------------|---------|------------------------------------------------------|
| `dispatched_total`    | integer | Total tasks in `dispatched` status.                  |
| `dispatched_without_pr` | integer | Dispatched tasks with no PR — these are candidates for heartbeat reaping if they exceed the grace period. |
| `dispatched_with_pr`  | integer | Dispatched tasks with an open PR — protected from heartbeat reaping (anti-pattern #4). |
| `abandoned_last_24h`  | integer | Tasks abandoned in the last 24 hours.                |

---

## killswitch

State of the killswitch file on disk.

| Field    | Type             | Description                                                   |
|----------|------------------|---------------------------------------------------------------|
| `active` | boolean          | `true` if the killswitch file exists (engine will stop after current tick). |
| `reason` | string or null   | Contents of the killswitch file (trimmed), or `null` if empty or absent. |
| `path`   | string           | Absolute path to the killswitch file on the current host.     |

---

## Example payload

```json
{
  "schema_version": 1,
  "generated_at": "2026-04-24T18:30:00Z",
  "instance": "oxi-self",
  "plan_tier": "max_20x",
  "repo": "escotilha/oxi",
  "task_counts": {
    "planned": 2,
    "dispatched": 1,
    "merged": 8,
    "failed": 1
  },
  "tasks": [
    {
      "identifier": "T1-3",
      "tier": 1,
      "title": "oxi v3 status --json flag",
      "status": "dispatched",
      "pr_number": null,
      "cost_usd": 0.0,
      "last_progress_at": "2026-04-24 18:28:10",
      "dispatched_at": "2026-04-24 18:28:10",
      "merged_at": null,
      "failed_at": null,
      "failure_reason": null,
      "created_at": "2026-04-24 18:00:00",
      "updated_at": "2026-04-24 18:28:10"
    }
  ],
  "budget": {
    "verdict": "ok",
    "today_spend_usd": 1.23,
    "daily_soft_warn_usd": 2.0,
    "daily_hard_cap_usd": 20.0,
    "window_start": "2026-04-24T00:00:00Z"
  },
  "heartbeat": {
    "dispatched_total": 1,
    "dispatched_without_pr": 1,
    "dispatched_with_pr": 0,
    "abandoned_last_24h": 0
  },
  "killswitch": {
    "active": false,
    "reason": null,
    "path": "/home/ops/.oxi/KILLSWITCH"
  }
}
```

---

## Versioning policy

- **Additions** (new top-level keys, new fields inside existing sections) are
  backward-compatible and do not bump `schema_version`.
- **Removals or type changes** of existing fields bump `schema_version`.
- Consumers MUST tolerate unknown keys at every level.
- Consumers SHOULD check `schema_version` and reject payloads with an
  unsupported version rather than silently misreading them.
