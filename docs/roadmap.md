# oxi dogfood roadmap

The queue the `oxi-adapter-self` dogfood loop picks from. Keep it tight — 10-15 open items at a time. Items ship as individual PRs reviewed by Pierre (`auto_merge=False` in `SelfAdapter`).

Conventions:

- **Tier 0** — blockers, safety/security, installer bugs. Dispatch first.
- **Tier 1** — user-visible polish, runbooks, CLI ergonomics.
- **Tier 2** — internal cleanup, test coverage, refactors.

Each item has: `[Tier] ID — title`, a one-line problem statement, and the acceptance criteria the worker session reads.

---

## T0-1 — install runbook: zero-to-first-tick under 5 min

**Problem:** `README.md` mentions install but there's no single-page walkthrough that takes a new fork from `pip install` to `oxi v3 tick --real-claude --times 1`. New operators hit friction in the first 10 minutes.

**Acceptance:**
- `docs/runbooks/install.md` exists
- A Python 3.11 user can copy-paste each block and land at "engine tick prints task transitions"
- Covers: pip install, adapter template, first `oxi init`, smoke `oxi v3 tick`
- Passes `scripts/lint-for-leaks.sh` (no project-specific identifiers leak through)

## T0-2 — rollback runbook for bad alpha releases

**Problem:** If a PyPI release is broken (like `0.1.0a1`'s hardcoded version), there's no documented recovery path. We got lucky this time — the fix was a republish, not a yank.

**Acceptance:**
- `docs/runbooks/rollback.md` exists
- Covers: when to yank vs republish vs release-note-only, the PyPI yank procedure, how to tell users (GitHub release notes + README banner)
- Includes the `0.1.0a1 → 0.1.0a2` incident as the worked example

## T1-3 — `oxi status --json` output

**Problem:** `oxi v3 status` prints a human-readable table. Ops scripts and the dashboard both re-parse the DB instead of reusing the status code path. A `--json` flag removes the duplication and lets operators script against the engine state.

**Acceptance:**
- `oxi v3 status --json` emits a stable JSON document: `{tasks: [...], budget: {...}, heartbeat: {...}}`
- Each task entry has: identifier, tier, status, pr_number, last_progress_at, cost_usd
- Human output still works when `--json` is absent
- Schema documented in `docs/status-json-schema.md`

## T1-4 — ci_issue_filer — surface CI failures in the ledger

**Problem:** When a dispatched claude session fails in CI after pushing the branch, the supervisor currently logs the event but doesn't surface it in the dashboard or any notification channel. The origin-feature-gap report (2026-04-24) flagged this as a Phase 3 port.

**Acceptance:**
- New module `oxi_core.v3.ci_issue_filer`
- Watches GitHub check runs on open PRs through `GitHubClient`
- Emits `ci_failure_observed` events with workflow name + failing step
- Dashboard renders the latest CI status per PR
- Tests use `FakeGitHubClient` (no live API)

## T1-5 — ANSI colors in CLI output

**Problem:** `oxi v3 tick` output is a wall of monochrome text. When debugging, you can't tell at a glance which tasks moved forward versus which failed. ANSI color codes for status transitions (green=progress, yellow=stuck, red=failed) would help.

**Acceptance:**
- `oxi v3 tick` colors state transitions: green for forward, yellow for stalled (>1h no progress), red for terminal failures
- `NO_COLOR` env var respected (https://no-color.org/)
- TTY detection: skip colors when piped
- Unit tests assert output format is stable when `NO_COLOR=1`

## T1-6 — dashboard: show last-10 events per task

**Problem:** The dashboard shows current status but no history. Drilling into "why did T2-14 get stuck?" requires a SQL query. A per-task event tail (last 10 ledger entries) would be enough for most triage.

**Acceptance:**
- Clicking a task row expands to show its last 10 events (kind + timestamp + truncated payload)
- HTML-escaped like every other field
- No new HTTP route; the expansion is static content in the same page render

## T1-7 — `oxi v3 kill` CLI ergonomics

**Problem:** The killswitch is file-based (`touch .oxi/KILLSWITCH`), which works but is non-obvious to new operators. A `oxi v3 kill` subcommand that writes the file and reports the active engine state would be friendlier.

**Acceptance:**
- `oxi v3 kill` writes the killswitch file, prints "engine killswitch set at {path}"
- `oxi v3 kill --clear` removes it, prints confirmation
- `oxi v3 status` surfaces the killswitch state in its header line

## T2-8 — extract repeat SQL patterns into a query helpers module

**Problem:** `oxi_core.v3.*` modules each build their own SQLite queries. Several patterns repeat: "select task by identifier", "insert event", "update status+last_progress_at atomically". A thin helpers module (not an ORM — just typed `query_foo(conn, ...)` functions) would cut duplication.

**Acceptance:**
- New `oxi_core.v3.query_helpers` module
- At least 5 repeated patterns extracted into typed helpers
- All call sites updated
- No behavioral change — existing tests still pass
- Each helper documented with "why it exists" (atomic-stamping guarantee, etc.)

## T2-9 — replace `str(path)` sprinkle with explicit `os.fspath`

**Problem:** The codebase has many `str(path)` casts where `os.fspath(path)` would be more precise (and let non-`PurePath` path-likes through). This is minor but tightens the interface at boundaries to subprocess and SQLite.

**Acceptance:**
- All `str(Path(...))` / `str(path)` call sites that cross into an API accepting a path swap to `os.fspath(path)`
- Docstrings updated where a path parameter was documented loosely
- No behavioral change — existing tests still pass

## T2-10 — pytest timeout guard

**Problem:** A hung test (e.g. the `test_server_*` tests in `test_dashboard.py` if the server thread wedges) will sit indefinitely in CI. `pytest-timeout` with a 30s per-test default would cut CI hangs.

**Acceptance:**
- `pytest-timeout` added to dev deps in both packages
- Default `--timeout=30` in `tool.pytest.ini_options`
- Slow tests (the integration `@pytest.mark.slow` markers) opt into `--timeout=180`
- CI still passes

---

## Done (moved to release notes)

See `docs/release-notes/` for shipped items. Recent:

- `v0.1.0a2` — dynamic `__version__` + initial alpha
- `v0.1.0a1` — first usable alpha (superseded)
- Pre-alpha security hardening (#41): path-traversal defense, pr_number XSS, `ANTHROPIC_API_KEY` plumbing, dashboard 404, SHA-pinned actions, gitleaks + pip-audit CI, Dependabot, `SECURITY.md`

---

## Notes for the dogfood engine

- The adapter (`oxi-adapter-self`) enforces `auto_merge=False`. Every PR the engine opens waits for Pierre's review.
- Budget: hard cap $20/day, $2/task Opus, $0.50/task Sonnet. Tasks that estimate beyond per-task cap get held at `queued` until operator intervention.
- Serial dispatch — `max_concurrent=1`. No fan-out until the single-task loop is stable for two weeks.
- Identifiers here (T0-*, T1-*, T2-*) are what the engine sees. Keep them stable — renaming invalidates handoff snapshots and ledger cross-references.
