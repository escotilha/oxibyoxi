# Visual polish plan

**Status:** Plan. Seven roadmap items (T1-19 to T1-25) implement it.
**Scope:** UI surfaces only — CLI, dashboard, wizard, README. Zero engine changes.
**Why:** Oxi's engine is rock-solid; the user-facing surfaces are functional but undifferentiated. README has no screenshot, dashboard has no visual hierarchy, CLI mixes glyphs (`⚠`, `✗`, plain words). Closing this gap is high-ROI: every operator sees these surfaces every day, and the project's pitch ("walk away, come back to PRs") is undersold without a screenshot of the loop running.

---

## The seven items, in ROI order

Each ships as its own PR, dispatched by `oxi v3 tick`. Sequenced so each PR's diff stays small and reviewable.

### T1-19 — Rich-ify `oxi status` and `oxi v3 tick`

**Why first:** Daily-use surface. Every operator hits `oxi status` and watches `oxi v3 tick` output. Today both are `print(f"...")` blocks (~80 lines in `oxi-core/src/oxi_core/cli.py`). Rich gives us tables, panels, in-place updates, and auto-degrades to plain text on non-TTY without us writing the fallback.

**Surface:**
- `oxi status` — replace the manual `print` block (`cli.py` `cmd_status`) with `rich.table.Table` for status histogram + recent events, `rich.panel.Panel` for the budget block. Keep the `--json` path untouched.
- `oxi v3 tick` — wrap each iteration in a `rich.panel.Panel` with a header (`tick #1 · 14:22:10`) and a 5-line summary at the end (workers dispatched, PRs opened, $ used, failures, next-tick hint).

**Constraints:**
- Add `rich` to `oxi-core/pyproject.toml` runtime deps.
- Honour `NO_COLOR` and non-TTY (Rich does this natively via `Console(force_terminal=False, no_color=...)`).
- Don't break the existing `_color.py` API — call sites that use it stay unchanged for now (T1-22 cleans up).

**Out of scope:** Dashboard (T1-20), glyphs (T1-21), wizard (T1-23). One file, one purpose.

---

### T1-20 — Dashboard hero row + budget bar

**Why second:** Operators check the dashboard every time they wonder "is the engine alive?" Today's layout buries the answer behind a 5-column table. The hero row puts the four numbers that matter at the top.

**Surface:**
- `oxi-core/src/oxi_core/v3/dashboard.py` — `render_html()` adds a hero row above the existing summary div: 4 stat tiles (workers active, PRs open, merged today, budget remaining).
- Budget tile gets a horizontal fill bar: green when >50% remaining, amber 20-50%, red <20%. Pure HTML/CSS, no JS.
- Drop the `<h2>Status (last Nh)</h2>` table into a sub-panel below the hero. Keep all data, change visual hierarchy.

**Constraints:**
- Server-rendered, single inline `<style>` block. No JS framework, no build step. Match existing convention.
- HTML-escape all numeric/string interpolation (existing `html.escape` discipline).
- The health banner (`HEALTH_BANNER`) stays at the very top, above the hero.
- All data already available via `brief.generate()` and existing queries — no new SQL.

**Out of scope:** Glyphs in the task table (T1-21), accent color (T1-24).

---

### T1-21 — Status glyph standardization

**Why third:** Mechanical sweep, but the consistency multiplies the value of T1-19 and T1-20. Today: `⚠` for killswitch in CLI, `✗` for budget hard-stop, plain `merged`/`failed`/`abandoned` words for task status, `[retry]` text badge in dashboard.

**Pick five glyphs and use them everywhere:**

| Status | Glyph | Used for |
|---|---|---|
| Queued / planned | `○` | tasks waiting to dispatch |
| Running / dispatching | `●` | tasks in flight, PRs awaiting critic |
| Merged / success | `✓` | merged PRs, healthy budget, engine alive |
| Failed / abandoned | `✗` | failed dispatch, hard-stop, abandoned tasks |
| Killswitched / paused | `⏸` | killswitch active, engine paused |

**Surface:**
- New module `oxi-core/src/oxi_core/v3/_glyphs.py` exporting the five constants + a `glyph_for_status(s: str) -> str` helper.
- Migrate call sites in `cli.py`, `dashboard.py`, `brief.py` to use the constants. No new content — pure substitution.
- Dashboard: status column shows the glyph + the status text. (Removing the column entirely is a future PR; T1-21 stays mechanical.)

**Constraints:**
- Glyphs are Unicode. CLI must respect `NO_COLOR` (glyphs themselves are colorless; that's fine).
- Update tests that match exact text — there are several in `tests/test_dashboard*.py` and `tests/test_cli*.py` that assert on rendered strings.

---

### T1-22 — README screenshot + before/after rewrite

**Why fourth:** Sells the project. Today's README is 190 lines of dense prose with one ASCII pipeline diagram and zero visual proof. Bun's README leads with a single terminal cast; Turborepo uses a before/after framing. We do both.

**Surface:**
- `README.md` — restructure the top 60 lines:
  1. Title + one-sentence pitch (existing).
  2. **One terminal screenshot** of `oxi v3 tick --real-claude` shipping a real PR. PNG with macOS-style window chrome, committed as `docs/assets/oxi-tick-screenshot.png`.
  3. **Before / after** block: side-by-side fenced code (left: empty `roadmap.md` and zero merged PRs; right: same `roadmap.md` and 5 merged PRs after one tick).
  4. Install + quick-start (existing, lightly edited).
- Move the architecture tree (currently lines 110-150 of README) into `docs/architecture.md` (file already exists — fold in if there's no overlap).

**Constraints:**
- Screenshot is a one-time artifact captured by the operator. T1-22 prepares the slot in the README and lands a placeholder; the actual PNG is committed in the same PR by the human reviewer (operator-only work — `claude` can't take a screenshot).
- All existing prose content preserved or moved — no information lost.
- Pass `scripts/lint-for-leaks.sh`.

**Out of scope:** Logo (T1-25), accent color (T1-24).

---

### T1-23 — Wizard step counter + review screen

**Why fifth:** First-impression surface. `oxi init` is what every new operator runs. Today it's a flat sequence of `input()` calls with no visible progress and no preview before files hit disk.

**Surface:**
- `oxi-core/src/oxi_core/wizard.py` — `_prompt()` gains a step counter prefix (`[3/8] GitHub repo (owner/name):`). Question count comes from a constant.
- After all 8 answers, render a "review and confirm" panel listing every captured value + the paths about to be created. Operator confirms (`y/N`) before any disk write.
- Preserve the `input_fn` injection pattern used by tests.

**Constraints:**
- Step counter must work with the test harness (which passes a custom `input_fn`).
- Confirm step has a `--yes` / `--force` flag bypass for non-interactive scaffolding.
- Existing tests in `tests/test_wizard.py` need updates for the new prefix and the confirm step.

---

### T1-24 — Accent color

**Why sixth:** Brand consistency. Today the dashboard uses three different blues (`#0057b8`, `#003d82`, plus default link blue) and three reds for the health banner. We pick one accent and use it everywhere.

**Decision:** Orange (`#E66A2C` or similar deep amber). Evokes "ox" (the name), and pairs well with the green/amber/red semantic colors already used for status. Avoids the blue-on-blue collision and reads warm/active rather than cold/corporate.

**Surface:**
- Single CSS variable in `dashboard.py`'s inline `<style>`: `--accent: #E66A2C`. Use it for h1 underline, link color, hero-row tile underlines.
- README: heading rules (none currently; light touch).
- CLI: bold accent for `oxi` in headers (Rich `[bold #e66a2c]oxi[/]`), respecting `NO_COLOR`.

**Constraints:**
- Ship the hex as a documented constant in `oxi-core/src/oxi_core/v3/_color.py` (or a sibling) so future PRs reference it instead of hardcoding.
- Do not change any *semantic* color (green/amber/red for status). Accent is for brand surfaces only.

**Out of scope:** Logo (T1-25). Color is the foundation; logo lands on top.

---

### T1-25 — Logo + `oxi --version` ASCII art

**Why last:** Fun, optional, smallest behavioral footprint. Lands on top of the accent color from T1-24.

**Surface:**
- A small ASCII glyph (3-5 lines, < 20 chars wide) used in three places:
  1. `oxi --version` — print the glyph, then version + Python + platform.
  2. Dashboard `<h1>` — render glyph as monospace banner above the instance name.
  3. README — top of file, before the title.
- Glyph itself: simple, stable in any terminal. Suggestion to refine in implementation: a stylized ox-horn curve, or `[ oxi ]` framed by horns. Designer call. Implementation PR ships the chosen glyph.

**Constraints:**
- Pure ASCII (or BMP Unicode), no escape codes baked in. Color applied via Rich at print time.
- Stable rendering at 80-col widths and on dashboards' system-ui fallback.
- Don't break the existing `oxi --version` machine-readable line — keep `oxi {version}` as the last line so scripts grepping it still work.

---

## What this plan deliberately does NOT do

- **No engine changes.** Every PR touches CLI/dashboard/wizard/README files only. Engine state machine, dispatch, critic, budget, ship_recovery — all untouched.
- **No new dependencies beyond Rich.** Rich is the one runtime addition. No prompt_toolkit, questionary, click, typer, textual.
- **No design system / token library.** One accent hex + five status glyphs is the entire vocabulary.
- **No live-updating dashboard.** Server-rendered HTML stays exactly as it is. We polish, we don't rebuild.
- **No tests for visual output.** We update assertion strings where they break, but we don't write snapshot tests for color/glyph rendering.

## Sequence rationale

T1-19 (Rich) lands first because it adds the dependency and proves the auto-degrade story. T1-20 (dashboard hero) is independent and pure HTML/CSS — can land in parallel if the operator wants. T1-21 (glyphs) consolidates the vocabulary across both surfaces, so it sequences after both. T1-22 (README) is documentation-only and depends on nothing. T1-23 (wizard) is independent. T1-24 (color) ships the brand foundation; T1-25 (logo) lands on top.

The realistic queue is: T1-19, T1-20 in parallel → T1-21 → T1-22, T1-23 in parallel → T1-24 → T1-25.

## Success criteria

For each PR, the critic should accept if:
- Diff is < 300 lines (T1-19 may push 400 with tests).
- All existing tests pass.
- New behavior is exercised by at least one test (or, for README/screenshot, the file lints clean).
- `scripts/lint-for-leaks.sh` passes.
- No engine-state changes, no new SQL, no schema migration.

For the operator (Pierre), the visible delta after all seven ship:
- README has a screenshot above the fold and a before/after block.
- `oxi status` looks like a Rich-rendered status panel, not a `print` block.
- `oxi v3 tick` brackets each iteration with a header + summary box.
- Dashboard's top half is a 4-tile hero with a colored budget bar.
- One accent color and five status glyphs used consistently across CLI, dashboard, and brief.
- `oxi --version` shows a small ASCII logo.

That's the bar.
