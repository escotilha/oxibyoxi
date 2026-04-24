# Public-flip checklist

What needs to be true before `escotilha/oxi` flips from private to public.

Not a deadline; a gate. Each row is either DONE (ship-ready today), READY (needs the flip itself to activate), or TODO (actual work remaining).

## Governance

| Item | Status | Notes |
|---|---|---|
| `LICENSE` at repo root | **DONE** | MIT, matches pyproject (PR #80) |
| `CODE_OF_CONDUCT.md` | **DONE** | Contributor Covenant 2.1 (PR #80) |
| `SECURITY.md` with disclosure policy | **DONE** | Threat model + 9 security rails |
| `CONTRIBUTING.md` | **DONE** | Branching, PR format, dogfood-first rule |
| Issue templates (bug / feature) | **DONE** | Under `.github/ISSUE_TEMPLATE/` (PR #80) |
| PR template | **DONE** | `.github/PULL_REQUEST_TEMPLATE.md` |
| Code of conduct enforcement contact | TODO | Pick an email / form link. Currently SECURITY.md points to the maintainer; formalize for CoC reports. |

## Install experience

| Item | Status | Notes |
|---|---|---|
| `pip install --pre oxi-core` pulls working 0.1.0a5 | **DONE** | Verified end-to-end against fresh venv |
| `oxi init` wizard scaffolds a working adapter | **DONE** | Template ships in wheel as package-data (fixed in 0.1.0a4) |
| Entry-point auto-discovery (`oxi.adapters`) | **DONE** | T0-101, PR #55 |
| Install runbook | **DONE** | `docs/runbooks/install.md`, 5-minute walkthrough |
| Operator manual | **DONE** | 11 pages under `docs/manual/` |
| PyPI `[project.urls]` point at Documentation/Issues/Release Notes | **DONE** | Set in 0.1.0a5 |
| PyPI Documentation URL is publicly readable | **READY** | Currently points at the private GitHub tree. On flip: works automatically. Alternative: publish to GitHub Pages; not required. |

## Security scans in CI

| Scan | Status | Notes |
|---|---|---|
| pytest matrix (3.11 + 3.12) | **DONE** | |
| ruff | **DONE** | |
| pip-audit (CVE scan) | **DONE** | |
| gitleaks (secret patterns) | **DONE** | |
| lint-for-leaks (forbidden strings) | **DONE** | |
| bandit (Python SAST) | **DONE** | `.bandit.yaml` documents every skip |
| CodeQL (taint analysis) | **READY** | `continue-on-error: true` because GHAS not enabled on private repos. **On flip: remove continue-on-error so CodeQL failures block PRs.** |
| first-fork smoke test | **DONE** | PR #57 |
| Dependabot | **DONE** | Weekly pip + github-actions bumps |
| SBOM per release | TODO | T1-17 module merged; wire into release workflow. Optional pre-flip. |

## Publish surface

| Item | Status | Notes |
|---|---|---|
| PyPI `oxi-core` | **DONE** | 0.1.0a5 live |
| PyPI `oxi-adapter-reference` | **DONE** | 0.1.0a5 live |
| Tagged GitHub release with notes | **DONE** | `v0.1.0a5` tag + release notes file |
| Release notes index in `docs/release-notes/` | **DONE** | a1 → a5 enumerated |
| Semver contract documented | TODO | Short doc: "alpha = breaking changes expected; beta = no breaking changes within minor; stable = semver applies." 10 lines of writing. |
| Changelog (CHANGELOG.md) | TODO | Currently we have per-version release notes; a roll-up CHANGELOG.md is nice-to-have for scanning. |

## Operator safety

| Item | Status | Notes |
|---|---|---|
| Auto-merge off by default in reference adapter | **DONE** | `DispatchPolicy().auto_merge = False` |
| Budget hard-cap enforced (proven today: engine halted at $20.15) | **DONE** | |
| Prompt-injection disclaimer in runbook | **PARTIAL** | Mentioned in SECURITY.md; should be louder in `docs/manual/safety-rails.md` for any adopter who enables auto-merge |
| Dashboard localhost-only by default | **DONE** | |
| Killswitch / oxi v3 kill | **DONE** | T1-7 CLI ergonomics (PR #76) |
| Engine self-healing (pause after consecutive failures) | **DONE** | T1-18 (PR #72) |

## Community / growth (optional for flip, needed for adoption)

| Item | Status | Notes |
|---|---|---|
| README has a clear "what it does in 60 seconds" | PARTIAL | Current README is accurate but not a hook. Rewrite for the first-time reader. |
| Demo video / GIF | TODO | 30-second screencast of a dispatch → PR → merge. Not blocking; dramatically improves "should I try this?" conversion. |
| Second reference adapter (non-toy) | TODO | Deferred until real operator signal — speculating wastes effort. |
| Examples directory (`examples/`) | TODO | Pre-built adapter configs for common project types (Django, FastAPI, Next.js). Same deferral reason. |
| Landing page / GitHub Pages site | TODO | Not blocking. MkDocs from `docs/manual/` is one command. |

## What blocks flipping

**Zero hard blockers.** The repo could flip today. What would improve the first-impression for public visitors:

1. Louder prompt-injection disclaimer in `docs/manual/safety-rails.md` (30-minute edit)
2. Rewrite first paragraph of README as a sub-60-second pitch (15-minute edit)
3. Flip CodeQL continue-on-error → fail-closed in the same PR as the visibility flip
4. Add a semver-contract blurb (5-minute edit)

Roughly a one-hour sprint total. Everything else is post-flip work that's perfectly fine to ship after adopters start arriving.

## The actual gating question

**Not technical readiness — strategic readiness.** oxi is a tool that dispatches AI against the adopter's codebase and opens PRs. Public visibility means:

- Anyone can install, point at their repo, and spend their own API budget on bad code
- Anyone can file an issue (SECURITY.md's disclosure policy must hold up to strangers)
- Anyone can open a PR (PR template + CI scans must hold up to adversarial contributors)

Today's 55 merged PRs say the technical stack holds. What I can't assess for you: whether you have the bandwidth to review issues from strangers, reject bad PRs without conflict, and absorb "oxi broke my thing" feedback without it costing you sleep.

My recommendation stands: **Phase 2 limited-invite first** (you already did this with danyelusero — see if he accepts, see what he hits), then Phase 3 flip. But the technical surface is ready whenever you are.
