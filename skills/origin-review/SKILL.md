---
name: origin-review
description: Re-run the ORIGIN → oxi feature-gap comparison, producing a dated markdown report under docs/. Enforces the sanitization discipline so no ORIGIN-specific string reaches the report. Use when ORIGIN has shipped new modules and we want to evaluate which to adopt.
user-invocable: true
model: sonnet
effort: medium
---

# Skill: origin-review

Compare ORIGIN (the predecessor autonomous orchestrator) to oxi and produce a report of features oxi should consider adopting.

ORIGIN's location, name, and any project-specific identifiers are deliberately not written here — the skill fetches them at runtime from the operator's environment (via the `ORIGIN_REPO_PATH` env var) or from a one-time prompt. The skill file itself contains **zero** forbidden strings, so the skill is shippable to forks even though forks won't invoke it.

## When to invoke

- ORIGIN has shipped a new module or substantial change.
- Oxi has completed a phase and the operator wants to re-evaluate what's worth porting.
- The previous report (most recent file under `docs/origin-feature-gap-*.md`) is >30 days old.

Do NOT invoke this skill as part of normal development work. It is a maintenance / planning tool.

## Inputs

- **`ORIGIN_REPO_PATH`** — absolute path to the ORIGIN repo on the operator's machine. Read from env; if unset, the skill prompts once.
- **oxi main branch** — read from the current checkout.
- **Prior report (if any)** — most recent `docs/origin-feature-gap-*.md`. Used to short-circuit modules that haven't changed since the last review.

## Outputs

A new markdown file at `docs/origin-feature-gap-YYYY-MM-DD.md` following the structure:

1. Header with date + ORIGIN commit ref.
2. Section 1: already in oxi.
3. Section 2: portable, worth adopting (with phase estimate + adaptation notes).
4. Section 3: ORIGIN-specific, stay out (with reason).
5. Section 4: top 3 recommendations for the current or upcoming phase.

The file must be referenced from `docs/origin-feature-gap-2026-04-24.md §Appendix` (the first invocation's report) or equivalent index so it remains discoverable.

## Procedure

### Step 1: Preflight

1. Resolve `ORIGIN_REPO_PATH`. If unset, ask the operator once; do not hardcode.
2. `cd "$ORIGIN_REPO_PATH" && git log -1 --pretty=%H` — capture the ref being reviewed.
3. Confirm `docs/anti-patterns.md §Sanitization` is present in oxi; read the forbidden-strings list from `scripts/lint-for-leaks.sh`.

### Step 2: Inventory

1. List every Python module in ORIGIN under its main source directory (commonly `src/<package>/` and `src/<package>/v3/`).
2. List every Python module in oxi under `oxi-core/src/oxi_core/` and `oxi-core/src/oxi_core/v3/`.
3. Build a per-module comparison:
   - If oxi has a file with the same name **and** comparable surface area → **Section 1**.
   - If oxi has no equivalent → candidate for **Section 2** or **Section 3**.

### Step 3: Classify candidates

For each ORIGIN module oxi does not have:

1. Read its docstring + key functions. Identify the behavior it implements.
2. Grep for ORIGIN-specific strings from the forbidden-strings list in `scripts/lint-for-leaks.sh`.
3. Classify:
   - **ORIGIN-specific** → Section 3 with a one-line "why it's tied to the project." Mechanism is often still portable; capture that under the closest Section 2 entry or in its own Section 2 entry if novel.
   - **Portable, clean** → Section 2. Note adapter fields the re-implementation will use.
   - **Portable, needs redesign** → Section 2 with a "needs redesign" marker and a sentence on what changes.
4. Never copy ORIGIN source text into the report. Describe the behavior in your own words, with references by file path and approximate line range only.

### Step 4: Recommend

1. Pick the top 3 items from Section 2 that close the largest gap for the next phase.
2. Estimate days per item. Lean conservative.
3. Order by value / cost.

### Step 5: Write

1. Write to `docs/origin-feature-gap-YYYY-MM-DD.md` using the template in the first report (`docs/origin-feature-gap-2026-04-24.md`).
2. Run `./scripts/lint-for-leaks.sh` — the new report **must pass** (it is under `docs/` which is in the scan set).
3. Commit with a subject like `docs(origin-review): report YYYY-MM-DD`.

### Step 6: Follow-ups

For each Section 2 item the operator approves, open an issue on the oxi repo. The issue body should be the Section 2 entry verbatim plus a checklist:

- [ ] Verify ORIGIN behavior (read the module, ignore any tests that name specific projects)
- [ ] Draft adapter fields needed
- [ ] Write the oxi version fresh (no copy-paste)
- [ ] Add leak-lint entries if the review turned up new forbidden strings
- [ ] Tests against fake_claude / FakeGitHubClient / in-memory DB only
- [ ] Ruff + leak-lint + pytest green on PR

## Invariants

The skill MUST:

- Never copy ORIGIN source code or unique strings into any oxi file.
- Pass `scripts/lint-for-leaks.sh` on the report it writes.
- Reference `docs/anti-patterns.md §Sanitization` for the forbidden-strings list.
- Produce a dated report, not an overwrite. Historical reports are a record of what ORIGIN looked like over time.

The skill MUST NOT:

- Add ORIGIN path literals, env var names, or SSH targets to any file.
- Recommend porting features that require real Claude or real GitHub invocation as part of the skill run itself.
- Skip the leak-lint check before committing the report.

## Related

- `docs/anti-patterns.md §Sanitization` — the forbidden-strings list this skill must respect.
- `docs/PLAN.md` — the roadmap that determines which phase a feature belongs in.
- `scripts/lint-for-leaks.sh` — the CI gate every report must pass.
- First report: `docs/origin-feature-gap-2026-04-24.md`.
