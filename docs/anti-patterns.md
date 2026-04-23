# Anti-patterns

Rules that every contributor must respect. Enforced by CI, tests, or runbook checklists — never by honor system alone.

The rules exist because each one has cost real engineering time on prior projects. Re-deriving them is expensive; encoding them once is cheap.

## 1. One release ships one headline change

Releases that bundle a rename **and** a refactor **and** a new feature create unbounded regression surface. When something breaks, nobody can bisect which change was responsible.

**Rule:** every release has one headline change. Release notes enumerate it in one sentence.

**Enforcement:** PR template requires a "headline" field. PRs that touch more than one subsystem get split during review. Release-notes CI check fails if the headline field is empty or lists multiple items.

## 2. DB path default matches systemd flag

Production outages are caused by CLI defaults opening a different database than systemd units open. Interactive commands then operate on stub data, while the timer operates on real data, and the two diverge silently.

**Rule:** every CLI entrypoint resolves its DB path through one function. That function is tested against the exact path any shipped systemd unit references.

**Enforcement:** `tests/test_db_paths.py` reads the default from `oxi_core.db` and the `--db` flag from any `*.service` unit under `adapters/`, asserts equality. Runs in CI.

## 3. Plan tier is explicit, not defaulted

Plan tier (e.g., "max_20x", "max_5x", "standard") is the kind of setting that catastrophically misbehaves when wrong — too low and the engine starves, too high and it blows budget. Silent defaults for this class of setting are the wrong pattern.

**Rule:** plan tier is an adapter method with no core default. Missing → engine refuses to start with a clear error.

**Enforcement:** `oxi_core.adapter.Adapter.plan_tier()` returns `str`, no default. `oxi v3 status` prints the active tier at the top of every output. Startup logs the tier to stdout and to the ledger.

## 4. No project-specific string literals in core

Core code that knows the name of one specific project will leak that name into prompts, dashboards, emails, and commit messages. Every such leak makes the next fork harder.

**Rule:** zero string literals in `oxi-core/src/` that refer to any specific project, user, repo, path, email, or IP.

**Enforcement:** `scripts/lint-for-leaks.sh` runs in CI on every PR, every merge to `main`, and as a release gate. See the [Sanitization](#sanitization) section for the forbidden-string list.

## 5. Rename refactors run a full grep pass first

When renaming a module, table, or path, hardcoded string literals inside source code are the first thing to miss. SQL written as raw strings, error messages, log keys, systemd unit references — all escape a find-replace IDE refactor.

**Rule:** renames are performed by a single script that produces a diff preview. The script greps the full codebase (including tests, docs, scripts, and CI configs) for the legacy identifier before committing. Reviewer validates the grep output is empty post-rename.

**Enforcement:** `scripts/rename.sh <old> <new>` is the standard tool. PRs that rename things without using it get rejected.

## 6. Venv rebuild on install-dir move

Moving a Python install directory requires rebuilding the venv because pip-installed console scripts have absolute shebangs. Symlinks don't fix this.

**Rule:** install runbook explicitly includes venv rebuild + `which oxi` verification steps.

**Enforcement:** `docs/runbooks/install.md` and `docs/runbooks/upgrade.md` include verification checklists. `scripts/smoke/install.sh` rebuilds the venv from scratch in CI to prove the documented flow works.

## 7. Rollback is one command

If rollback takes more than one command, operators hesitate, which makes the window of broken production longer.

**Rule:** every release that touches deployable artifacts ships with a `scripts/rollback.sh` that reverts the install in one step. Tested in CI by installing N-1, upgrading to N, rolling back to N-1, and asserting the system is functional.

**Enforcement:** CI job `test-rollback.yml` runs on every release tag.

## 8. Secrets never land in committed files

Tokens, passwords, OAuth credentials, PFX files — committed once, leaked forever (git history retains them even after force-push, and force-pushes on shared branches are forbidden anyway).

**Rule:** secrets live only in env vars. Locally loaded from OS keychain. Remotely from GHA Secrets or the platform's secret store.

**Enforcement:** `gitleaks` in CI. Pre-commit hook runs `gitleaks protect` locally.

## 9. No `--no-verify`, no `--force-push` to shared branches

Hooks exist because they caught a bug once. Skipping them reintroduces the class of bug. Force-pushing to `main` or any branch another contributor tracks destroys work silently.

**Rule:** no `--no-verify`. No force-push to `main`. Force-push to an individual feature branch is allowed only if the author owns it solo.

**Enforcement:** branch protection on `main` rejects force-pushes and non-fast-forward updates. Pre-commit hook logs when `--no-verify` is used (the hook can't block itself, but the log shows up in review).

---

## Sanitization

oxi is designed as an independent product. The author has studied other orchestrators while designing it. The sanitization list below is a CI-enforced grep filter that fails the build if any forbidden string appears in `oxi-core/src/`.

### Why this matters

Even one leaked string literal undermines oxi's positioning as an independent product. The list grows as new identifiers are found; it never shrinks.

### Where the check runs

- Every PR.
- Every merge to `main`.
- As a release gate before any PyPI upload.

### Scope of the check

- `oxi-core/src/**/*.py` — all engine source.
- `adapters/_reference/**/*.py` — the reference adapter must also be generic.
- `adapters/_template/**/*` — the scaffold must be generic.
- `docs/**/*.md` — documentation must be generic.

Exempted (explicitly):
- `docs/PLAN.md` — the plan references prior work as context; that's expected and read-only history.
- `docs/anti-patterns.md` — this file, which discusses sanitization meta.
- Release notes that cite external CVEs or upstream projects by name.

### Forbidden strings — initial list

Maintained in `scripts/lint-for-leaks.sh`. When a new leak is found in review, add it to the list in the same PR that fixes the leak. Never remove entries.

Categories on the list:

1. **Project names** — any project the author has worked on whose name should not appear in oxi.
2. **Company names** — same reason.
3. **Private paths** — filesystem paths specific to any private host.
4. **Private IPs** — Tailscale IPs, VPS IPs, any non-oxi-specific network location.
5. **Private email addresses** — author's private addresses (oxi commits can use a public-facing address).
6. **Private repo slugs** — `<owner>/<repo>` paths for any repo other than `escotilha/oxi`.

The concrete list lives in the script. Treat it as append-only.

### If the check fails

1. Read the output — it names the file, line, and forbidden string.
2. Rewrite the offending code without the string. Usually this means "read it from the adapter."
3. Re-push. CI re-runs.
4. If the offending string is already supposed to be allowed (e.g., a legitimate upstream project name in a changelog), add an explicit exemption to the script and justify in the PR description.

---

## How to propose a new anti-pattern

Found a pattern that cost you time? Propose it as an entry here:

1. Open a PR adding the entry under a new section.
2. Describe the pattern in two or three sentences.
3. Specify the enforcement mechanism. "Written down" is not enforcement.
4. Link the incident or reasoning that motivated the rule, if non-obvious.

Anti-patterns without enforcement mechanisms do not land.
