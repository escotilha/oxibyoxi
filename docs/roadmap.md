# oxi dogfood roadmap

The queue the `oxi-adapter-self` dogfood loop picks from. Each item is a bold line shaped `**T{tier}-{N} · {title}**` followed by an italic subtitle — the planner reads this format.

Keep it tight — 10-15 open items at a time. Items ship as individual PRs reviewed by Pierre (`auto_merge=False` in `SelfAdapter`).

Conventions:

- **Tier 0** — blockers, safety/security, installer bugs. Dispatch first.
- **Tier 1** — user-visible polish, runbooks, CLI ergonomics.
- **Tier 2** — internal cleanup, test coverage, refactors.

---

## Tier 0

**T0-1 · install runbook: zero-to-first-tick under 5 min**
_new operators need a single-page walkthrough from pip install to `oxi v3 tick`. Write docs/runbooks/install.md covering pip install, adapter template, smoke tick. Must pass scripts/lint-for-leaks.sh._

**T0-2 · rollback runbook for bad alpha releases**
_document the PyPI yank vs republish decision tree using the 0.1.0a1 to 0.1.0a2 incident as the worked example. Land docs/runbooks/rollback.md._

**T0-11 · worktree_provision drift repair**
_if the target directory exists but the checked-out branch doesn't match the expected feature branch (main, detached HEAD, or a stale sibling branch), nuke and re-provision rather than failing. Today we surface a WorktreeError and the dispatch dies. Fix: validate HEAD via rev-parse --abbrev-ref, compare to expected, repair if drifted. Add git worktree prune before every add._

## Tier 1

**T1-3 · oxi v3 status --json flag**
_emit stable JSON with tasks/budget/heartbeat so the dashboard and ops scripts can reuse the status code path. Document schema in docs/status-json-schema.md._

**T1-4 · ci_issue_filer — surface CI failures in the ledger**
_new oxi_core.v3.ci_issue_filer module watches check runs on open PRs via GitHubClient; emits ci_failure_observed events. Use FakeGitHubClient for tests._

**T1-5 · ANSI colors in oxi v3 tick output**
_green=progress, yellow=stuck, red=failed. Respect NO_COLOR and skip on non-TTY. Output must stay stable when NO_COLOR=1._

**T1-6 · dashboard last-10 events per task**
_click a task row to expand its last 10 ledger events with timestamp and truncated payload. HTML-escape everything. No new HTTP route needed._

**T1-7 · oxi v3 kill CLI ergonomics**
_subcommand that writes/removes the killswitch file with confirmation. Surface state in oxi status header line._

**T1-12 · auto_recover — retry rejected or failed PR dispatches**
_new oxi_core.v3.auto_recover module. If a PR was rejected by the critic or CI failed hard, after a cooldown window reset the task to planned and re-seed it with a note in the brief. Implementation is oxi-shape (argv subprocess, FakeGitHubClient tests). Dashboard should label recovered tasks so operators can tell a retry from a first-run._

**T1-13 · deep_fix — escalation for repeatedly-stuck tasks**
_when a task has failed or been rejected N times, escalate model tier (sonnet to opus), loosen constraints on the prompt, or hand off to a dedicated worker session with more context. Adapter-configurable N, cooldown, and escalation recipe. Not about bypassing the critic; about giving the worker a better shot next try._

**T1-14 · ledger_events — typed event-kind constants**
_new oxi_core.v3.ledger_events with string constants for every event kind emitted across the codebase (dispatch_started, dispatch_succeeded, pr_observed, handoff_written, budget_hard_stop, ...). Migrate call sites to reference the constants. Reduces the string-drift class of bugs and gives the type checker something to hold onto._

## Tier 2

**T2-8 · extract repeat SQL patterns into query helpers**
_new oxi_core.v3.query_helpers with typed wrapper functions for the five-plus repeated patterns (select-task-by-id, insert-event, atomic-status-update, etc). No behavior change._

**T2-9 · replace str(path) with os.fspath at boundaries**
_tighten path-to-str casts at subprocess and SQLite boundaries. Update loose docstrings. No behavior change._

**T2-10 · pytest-timeout guard for CI**
_add pytest-timeout to dev deps; default 30s per-test, 180s on @pytest.mark.slow. Cuts CI hangs when a server-thread test wedges._

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
