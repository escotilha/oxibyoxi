# Contributing to oxi

oxi is in limited-invite alpha. External contributions are not yet open — this document describes the conventions in place so invited collaborators know the shape of the project, and so that when the repo opens, the contribution path is already documented.

## Before contributing

1. **Read `docs/manual/`.** The operator manual covers how oxi works, why the guardrails exist, and what's explicitly out of scope. Skim it even if you only plan to touch a small area.
2. **Read `SECURITY.md`.** The security rails are load-bearing. Changes that weaken them (removing the env whitelist, bypassing argv-form subprocess, relaxing HTML escaping, turning off the budget hard-cap) need explicit discussion before a PR lands.
3. **Read `docs/anti-patterns.md`.** It enumerates mistakes from prior orchestrators that oxi intentionally does not repeat.

## Branching

Branches use `<type>/<session-tag>-<short-topic>`:

| Type | When | Example |
|---|---|---|
| `feat/` | new capability | `feat/sa-worktree-drift-repair` |
| `fix/` | bug fix | `fix/sa-classifier-pr-success` |
| `chore/` | tooling, deps, refactors without user-visible behavior | `chore/sa-psos-parity` |
| `docs/` | documentation only | `docs/sa-first-fork-checklist` |

`<session-tag>` is a 2-letter identifier for the session (e.g. `sa`, `sb`) — helps avoid collisions when multiple sessions run concurrently. Date-based tags (`0424`) are also fine.

Always branch from `origin/main` after `git fetch origin`. Never work directly on `main`.

## Pull request format

- **Title:** `<type>(<scope>): <imperative summary>` — e.g. `fix(cli): plumb ANTHROPIC_API_KEY from env into dispatch`
- **Body:** Summary + test plan. Reference the roadmap item if applicable (e.g. `T0-101`).
- **Tests:** Every code change ships with tests or a justification in the PR body for why tests are infeasible.
- **CI must be green** before requesting review.
- **`auto_merge=False`** is the house rule for the dogfood adapter. Even for engine-produced PRs, a human reviewer must approve.

## Running the test suite

```bash
# One-time setup
python -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e "./oxi-core[dev]"
.venv/bin/pip install -e "./adapters/_reference[dev]"
.venv/bin/pip install -e "./adapters/_self[dev]"

# Run tests
cd oxi-core && ../.venv/bin/python -m pytest -q
cd ../adapters/_reference && ../../.venv/bin/python -m pytest -q
cd ../_self && ../../.venv/bin/python -m pytest -q

# Lint
../.venv/bin/ruff check .
```

## Leak-lint

Every PR runs `scripts/lint-for-leaks.sh` to catch identifiers from prior projects (previous orchestrator names, personal IPs, project-specific paths). If it fails locally, fix the string — **don't** add it to the allow-list without discussion. The sanitization discipline is why oxi can be forked safely.

## Authoring an adapter for a downstream project

If you're contributing because you adopted oxi on a project, the adapter package you wrote is typically **not** a contribution back to this repo. Adapters live in their own repositories (named `oxi-adapter-<yourproject>`), published to their own PyPI if you want, and registered via the `oxi.adapters` entry-point. The `adapters/_reference/` package here exists only to drive the throwaway `sample-project/` end-to-end tests.

If your adapter exposes a pattern that would benefit other forks (e.g. a reusable `DispatchPolicy` preset, a new `DispatchHost` type), propose it as a change to `oxi-core` — but keep the project-specific bits in your own repo.

## Security disclosure

**Do not open a public issue for security vulnerabilities.** See `SECURITY.md` for the disclosure process. In the limited-invite phase, reach the maintainer directly (Pierre Schurmann) rather than via GitHub Security Advisory.

## Dogfood-first

oxi builds itself. If you're adding a capability, first check whether the engine itself could dispatch it via `docs/roadmap.md` + `oxi v3 tick --real-claude`. Human-authored PRs are still welcome, especially for packaging, infrastructure, and cross-cutting refactors the engine can't safely do in flight. When you do hand-code, note in the PR why dogfood wasn't the right path.

## Code style

- **Python 3.11+** with `from __future__ import annotations` for modern typing.
- **ruff** is the formatter/linter. `ruff check .` must pass. Line length 100.
- **No emojis in code or docs** unless explicitly requested.
- **Don't add comments for what code does** — names should explain that. Comments exist for *why* (subtle invariants, workarounds, hidden constraints).
- **Match existing style** over your preferences. If oxi does something "the wrong way" consistently, a cleanup PR is welcome; a single-file deviation isn't.

## What's not welcome

- Changes that introduce project-specific strings (the sanitization rule exists for a reason)
- Abstractions for single-use code
- Retry loops around flaky behavior without root-cause analysis
- Backwards-compatibility shims for unreleased features
- "Improvements" to adjacent code outside the PR's scope

## Questions

If you're an invited collaborator and something is unclear, reach out directly rather than guessing. oxi is small enough that sync clarification is faster than a speculative PR.
