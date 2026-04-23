## Headline

<!-- One sentence. One change per PR per anti-pattern #1. If this PR does more than one thing, split it. -->

## Why

<!-- Link the roadmap item, issue, or incident. What problem does this solve? -->

## What changed

<!-- Bullet list of user-visible or developer-visible changes. Omit internal refactor noise. -->

## How to verify

<!-- What the reviewer runs to see that this works. Commands, URLs, expected outputs. -->

## Anti-patterns checklist

- [ ] One headline change only
- [ ] No project-specific string literals in `oxi-core/src/`
- [ ] `scripts/lint-for-leaks.sh` passes locally
- [ ] No `--no-verify`, no force-push to `main`
- [ ] No secrets committed
- [ ] Tests added or updated for the behavior changed
- [ ] Release-notes entry added (if user-visible change)
