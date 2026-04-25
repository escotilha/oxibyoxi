# Roadmap format

oxi parses a markdown roadmap. The format is strict — the parser ignores anything that does not match the expected patterns without error, so a wrong format silently yields zero tasks.

Always validate with `oxi v3 plan --dry-run` before running a full tick.

---

## Required structure

```markdown
## Tier 0

**T0-1 · add a greet function**
_a pure function that returns "hello, {name}"_

**T0-2 · add a changelog stub**

## Tier 1

**T1-1 · expose greet in __all__**
_add the new function to the module's public API_
```

### Section headers

Must be `## Tier N` where N is a non-negative integer:

```markdown
## Tier 0    ← correct
## Tier 1    ← correct

## T0        ← wrong — ignored
## Tier-0    ← wrong — ignored
```

Items that appear before the first `## Tier N` heading are ignored.

### Item lines

Must be `**T<tier>-<n> · <title>**` on a single line:

```markdown
**T0-1 · add a greet function**     ← correct (middle-dot ·)
**T1-3 · fix the parser**           ← correct

**T0-1 - add a greet function**     ← wrong — hyphen not accepted
**T0-1 — add a greet function**     ← wrong — em-dash not accepted
- T0-1 · add a greet function       ← wrong — must be bold **...**
```

The separator is a **middle-dot** (`·`, U+00B7), not a hyphen or dash.

### Optional subtitle

The line immediately following an item line may be an italic subtitle:

```markdown
**T0-1 · add a greet function**
_a pure function that returns "hello, {name}"_
```

The subtitle is shown in the dashboard and included in the task brief sent to Claude. It is optional but recommended for any item where the title alone is ambiguous.

---

## Tier semantics

| Tier | Behavior |
|---|---|
| T0 | Dispatched before all lower tiers. A new T0 item pauses non-T0 dispatches for one tick (tier-0 absorb). |
| T1, T2, … | Dispatched in tier order after all T0 items complete. |

Keep 10–15 open items at a time. The planner replenishes automatically as items ship.

---

## Example roadmap

```markdown
## Tier 0

**T0-1 · add input validation to the login form**
_reject empty usernames and passwords with a clear error message_

**T0-2 · fix the 500 on the /api/users endpoint**

**T0-3 · add a CHANGELOG entry for 1.2.0**

## Tier 1

**T1-1 · extract the validation logic into a shared helper**
_so both the login form and signup form use the same rules_

**T1-2 · add unit tests for the validation helper**

## Tier 2

**T2-1 · set up GitHub Actions for lint + tests**
```

---

## Diagnosing parse problems

```bash
oxi v3 plan --dry-run
```

Output when the format is correct:

```text
roadmap: /path/to/project/roadmap.md
parsed 6 item(s) — ok

  T0   T0-1  add input validation to the login form
             reject empty usernames and passwords...
  T0   T0-2  fix the 500 on the /api/users endpoint
  ...
```

Output when the format is wrong:

```text
roadmap: /path/to/project/roadmap.md
parsed 0 item(s) — check format

  No items found. Verify:
  - Section headers use "## Tier N" (not "## T0" or "## Tier-0")
  - Item lines use "**T0-1 · title**" (middle-dot ·, not hyphen or dash)
  - At least one item appears after a "## Tier N" heading
```

Common mistakes:

| Symptom | Likely cause |
|---|---|
| 0 items parsed | Header uses `## T0` or `## Tier-0` instead of `## Tier 0` |
| Fewer items than expected | Some items use hyphen `-` or dash `—` instead of middle-dot `·` |
| Items in wrong tier | Identifier prefix (`T0-`, `T1-`) does not match the section |

To type the middle-dot on macOS: `Option + Shift + 9` or paste `·` from this page.
