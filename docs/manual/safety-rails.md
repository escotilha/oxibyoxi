# Safety rails

oxi applies several structural defenses to limit blast radius. This page documents what each rail does, what it does not protect against, and what operators must do themselves.

---

## auto_merge discipline

`auto_merge=False` in the wizard-scaffolded adapter. Keep it off until you have:

1. Watched the engine open at least five PRs
2. Seen the critic reject at least one obviously-bad PR
3. Verified your CI pipeline catches the class of bugs the critic would miss

Only then set `auto_merge=True` in your adapter.

**What auto_merge does:** After CI passes on a PR, the engine runs a second model invocation (the critic) that reviews the diff. If the critic approves, the engine calls `gh pr merge`. If the critic rejects, the PR stays open for human review.

**What auto_merge does not do:** It does not protect against a crafted roadmap item or PR diff that fools the critic. The critic is a second model pass, not a formal verifier. Always treat the roadmap file with the same trust as source code — review changes to it.

---

## Argv-form subprocess

oxi uses `subprocess` with an explicit argument list, never `shell=True`. Task titles, branch names, and commit messages pass as argv elements, not as shell strings. They cannot be interpreted as shell syntax even if they contain metacharacters like `;`, `&&`, `$(...)`, or backticks.

This is verified by audit. If you find a `shell=True` call in `oxi-core/src/`, please open a private security advisory.

---

## Environment whitelist

When the engine spawns a `claude -p` subprocess, it does not inherit the supervisor's full environment. Only a small allowlist reaches the worker:

```
PATH, HOME, USER, LANG, TMPDIR, ANTHROPIC_API_KEY
```

Plus any additional variables the operator explicitly adds via adapter config.

Cloud credentials, database URLs, GitHub tokens, and any other secrets the supervisor process holds are **not** forwarded to worker sessions. A worker session that is compromised or misdirected cannot exfiltrate those values.

---

## Parameterized SQL

Every SQLite call uses `?` placeholders. No f-string or `%`-format SQL interpolation exists in the codebase. Task identifiers, event payloads, status values, and any other user-controlled data cannot inject SQL.

---

## HTML escaping

The dashboard escapes every user-controlled field before rendering. Task titles, statuses, failure reasons, and PR numbers cannot inject JavaScript. XSS is structurally blocked at the rendering layer, not by a content-security-policy header.

---

## Budget hard-cap

`budget.check` refuses to spend past `adapter.budget().daily_hard_cap`. A runaway dispatch loop that invokes Claude repeatedly in a tight cycle cannot exceed the cap — each dispatch checks the ledger before spawning.

The ledger itself (the SQLite DB) is the trust boundary. If an untrusted process can write to `oxi.db`, it can delete the `budget_hard_stop` event and unblock dispatch. Set the DB file to mode `0600` owned by the engine user.

---

## Process-group isolation

The engine spawns `claude -p` with `start_new_session=True`. This puts the worker in its own process group. If the supervisor needs to kill a worker (e.g., on budget abort), it sends `SIGTERM` to the entire process group, which includes any subprocesses the worker spawned. A worker's Bash-tool timeout cannot SIGTERM the supervisor.

---

## Dashboard is localhost-only

`oxi dashboard` binds to `127.0.0.1:8765` by default. Widening the bind requires a reverse proxy with authentication. oxi has no built-in auth. See [Dashboard — widening the bind](dashboard.md#widening-the-bind-advanced).

---

## No auto-merge by default

The wizard scaffolds `auto_merge=False`. Forks opt in explicitly. There is no code path that merges PRs without the operator having set `auto_merge=True` in their adapter.

---

## Known limitations (accepted risks)

### Prompt injection from roadmap and PR diffs

oxi embeds roadmap text (task titles, subtitles) and PR diff content directly into prompts sent to `claude -p`. A malicious entry in `roadmap.md`, or a crafted diff in a PR under review, can attempt to manipulate the worker session or the critic. There is no complete fix for this class — it is a fundamental tension of every autonomous coding agent.

**Operator responsibility:** Treat `roadmap.md` as source code. Review changes to it. If you enable `auto_merge`, you are trusting (a) the roadmap, (b) all PR contributors, and (c) your CI pipeline to be comprehensive enough that a crafted diff that fools the critic still fails CI.

### SQLite is the trust boundary

All state integrity — budget caps, killswitch, task state machine, ledger events — assumes `oxi.db` is not writable by untrusted processes. A process with write access can bypass any of these controls.

**Operator responsibility:** Set the DB to mode `0600`. Do not share the DB across users.

### pr_overlap fails open on GitHub errors

The `pr_overlap` gate checks whether a planned task's files would collide with any open PR. If the GitHub API call fails (rate limit, network error, permissions), the gate returns "no overlap" rather than blocking. This is intentional — the engine prefers over-dispatch to over-block when the signal is unavailable.

**Operator responsibility:** Watch for `pr_overlap: skipping overlap check` in logs. The downstream critic and CI catch actual bad merges.
