# Assets

Screenshots and other binary artefacts referenced by the docs.

## `oxi-tick-screenshot.png` (placeholder)

The README's hero image. Captured by an operator running `oxi v3 tick --real-claude` against a live repo.

**Capture instructions** (so anyone on the team can reproduce):

1. Set up `sample-project` with three short Tier-0 items in `roadmap.md`.
2. Run `OXI_ADAPTER=oxi_adapter_reference:ReferenceAdapter oxi v3 tick --real-claude --times 3` in a terminal sized to ~120×30.
3. After the run, screenshot the terminal (macOS: `Cmd+Shift+4`, then space-click the window for the system chrome).
4. Save to `docs/assets/oxi-tick-screenshot.png`.

The README references `docs/assets/oxi-tick-screenshot.png` directly. If the file doesn't exist, GitHub renders a broken-image icon — that's intentional during the screenshot-pending window so the slot is visibly reserved.

## Why this is a separate commit

Real screenshots embed timestamps, costs, and PR numbers from the run that produced them. Committing one in the same PR as the README rewrite means the two have to be redone together if anything in the layout changes. Keeping them separate lets the operator refresh the screenshot whenever it goes stale without touching the README scaffolding.
