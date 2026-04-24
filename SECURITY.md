# Security

## Responsible disclosure

Please do not open a public issue for security vulnerabilities.

For anything you believe is security-relevant, open a GitHub private security advisory on the repo (`Security` tab → `Advisories` → `Report a vulnerability`). Expect a first response within 7 days; fix or mitigation timeline depends on severity.

This policy covers `oxi-core` and `oxi-adapter-reference` only. Forks ship their own adapters; issues in a specific fork's adapter should go to that fork.

## Threat model

oxi is a supervisor process that:

- Spawns `claude -p` subprocesses against git worktrees on the local machine
- Reads operator-supplied roadmap markdown and issues prompts built from it
- Shells to `git` and `gh` against a target repo the operator owns
- Runs an unauthenticated localhost HTTP dashboard
- Writes state to a SQLite file the operator owns

**In scope**: bugs in oxi-core or oxi-adapter-reference that allow an attacker to exfiltrate secrets, execute arbitrary code outside the trust domain the operator granted, or manipulate auto-merge into shipping content the operator would not have approved.

**Out of scope**: anything downstream of a compromised adapter, SQLite database, or Anthropic credentials — oxi assumes those are trusted inputs. Issues in `claude -p` itself should go to Anthropic.

## Known security rails

oxi's design makes certain attack classes structurally difficult:

- **Argv-form subprocess only.** No `shell=True` anywhere. Task titles, branch names, and commit messages cannot be interpreted as shell syntax even if they contain metacharacters. Verified by audit.
- **Env whitelist.** `dispatch_invoke.build_env` strips everything not in a small allowlist (`PATH`, `HOME`, `USER`, `LANG`, `TMPDIR`, `ANTHROPIC_API_KEY`, plus caller-supplied additions). Cloud credentials, database URLs, GitHub tokens the supervisor process holds do not reach worker claude sessions.
- **Parameterized SQL.** Every `sqlite3` call in the codebase uses `?` placeholders; no f-string or `%`-format SQL interpolation. No SQL injection surface for task identifiers, event payloads, or status values.
- **HTML escaping.** Dashboard output escapes every user-controlled field. XSS-safe against task titles, statuses, failure reasons, and PR numbers.
- **Budget hard-cap.** `budget.check` refuses to spend past `adapter.budget().daily_hard_cap`. A runaway loop or compromised dispatch cannot exceed the operator's configured daily limit.
- **Process-group isolation.** `dispatch_invoke.invoke` spawns claude with `start_new_session=True` so the supervisor can `os.killpg` the whole tree. A worker's Bash-tool timeout cannot SIGTERM the supervisor.
- **Forbidden-string CI gate.** `scripts/lint-for-leaks.sh` fails CI on known-sensitive identifiers (project names from prior work, private paths, VPS IPs). Complemented by `gitleaks` in CI for general secret patterns.
- **No auto-merge by default.** `adapter.policy().auto_merge` is `False` in the template the wizard scaffolds. Forks opt in explicitly; auto-merge requires both a configured critic backend and the policy flag.
- **Dashboard is localhost-only by default.** Binds to `127.0.0.1:8765`. Forks that widen the bind MUST add authentication.

## Known limitations

These are accepted risks. Operators should understand them before enabling auto-merge or binding the dashboard broadly.

### Prompt injection from roadmap / diffs

oxi embeds operator-supplied roadmap text (task titles, subtitles) and PR-diff content directly into prompts sent to `claude -p`. A malicious entry in the roadmap file, or a crafted diff in a PR under review, can attempt to manipulate the worker session or the critic.

**Mitigation**: treat the roadmap file with the same trust as source code — review changes to it. The critic is a second model invocation that reviews the resulting diff, not the prompt, which limits the reach of prompt injection but does not eliminate it. There is no complete fix for this class; it is a fundamental tension of every autonomous coding agent.

If you enable `auto_merge`, you are trusting (a) the roadmap, (b) all PR contributors, and (c) your CI pipeline to be comprehensive enough that a crafted diff that fools the critic still fails CI.

### SQLite database is the trust boundary

All state integrity — budget caps, kill-switch, task state machines, ledger events — assumes the SQLite file is not writable by untrusted processes. A process with write access to `oxi.db` can:

- Bypass the budget hard-stop by deleting the `budget_hard_stop` event
- Reset any task's state by updating `status`
- Inject arbitrary ledger events

**Mitigation**: set the DB file to mode 0600 owned by the engine user. Do not share the DB file across users.

### Dashboard authentication

The dashboard binds to localhost by default. Widening the bind to a non-loopback interface exposes task titles, PR numbers, cost data, and failure reasons to anyone reachable on that interface. There is no built-in authentication.

**Mitigation**: keep the bind localhost, or front the dashboard with a reverse proxy that handles authentication (HTTP Basic Auth, OAuth, mutual TLS).

### `pr_overlap` fails open on GitHub errors

The `pr_overlap` gate checks that a planned task's `files_touched` doesn't collide with any open PR. If the GitHub API call fails (rate limit, network, permissions), the gate returns "no overlap" rather than blocking. This is a conscious decision: better to over-dispatch than over-block when the signal is unavailable. The downstream critic and CI catch the actual bad merge.

**Mitigation**: watch for `pr_overlap: skipping overlap check` in logs. If you care, add a wrapper that treats consecutive gh failures as cause to halt dispatch.

### `--force-push` protection is per-PR, not per-org

oxi never force-pushes, but individual adapters or operator scripts might. `origin/main` is not branch-protected by the engine itself — that's GitHub side configuration. Enable branch protection in your repo settings to enforce.

## Audit history

- **2026-04-24**: Pre-alpha security audit before `0.1.0a1` release. Findings addressed in #41.
