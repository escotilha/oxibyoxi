# Installing OXi (instructions for AI coding agents)

If you are an AI coding agent (Claude Code, Cursor, Aider, Codex, Grok with shell access, or similar) helping a user install OXi, follow this document exactly.

This file is **structured for machine consumption** — every step has a verification command and an expected outcome. Do not skip verifications. If a verification fails, stop and report the failure to the user; do not proceed to the next step.

The user-facing README at `README.md` is the human-readable equivalent; the two files describe the same install path with different audiences.

---

## What you are installing

OXi is an autonomous coding engine: a Python CLI (`oxi`) that reads a markdown roadmap, dispatches `claude -p` workers in git worktrees to implement items, watches the resulting PRs, and merges them after a critic review.

The user needs:
- A GitHub repo they own and have a local checkout of
- Python 3.11+
- The `gh` CLI installed and authenticated
- An Anthropic API key (or Claude Code session) for the agentic worker

**Do not begin the install** if the user does not have a GitHub repo of their own. Ask them which repo OXi should target before starting.

---

## Step 1 — verify Python 3.11 or newer

Run:

```bash
python3 --version
```

**Expected:** `Python 3.11.x` or higher (3.13 recommended).

**If the version is below 3.11** (very common on macOS — Apple ships 3.9 as `/usr/bin/python3`):

- macOS: `brew install python@3.13` then use `/opt/homebrew/bin/python3.13` explicitly for the rest of this install. The user's shell `python3` will keep pointing at 3.9 until they activate a venv.
- Ubuntu/Debian: `sudo apt install python3.13 python3.13-venv`
- Fedora: `sudo dnf install python3.13`

**Verify the new interpreter works** before proceeding:

```bash
/opt/homebrew/bin/python3.13 --version    # macOS
# or wherever python3.13 landed on Linux
```

Use the explicit path (not `python3`) for every subsequent step until step 2 activates a venv.

---

## Step 2 — create and activate a venv

The venv must be created with the 3.11+ interpreter from step 1. Do not install OXi to the system Python or to user site-packages — both produce harder-to-diagnose failures.

```bash
cd /path/to/users/repo
/opt/homebrew/bin/python3.13 -m venv .venv      # macOS; substitute path on Linux
source .venv/bin/activate
```

**Verify:**

```bash
which python && python --version
```

**Expected:** path inside `.venv/bin/` and version 3.11+.

---

## Step 3 — install oxi-core from PyPI

OXi is in beta. The `--pre` flag is required because pip's default resolver ignores pre-releases.

```bash
pip install --pre oxi-core
```

**Verify:**

```bash
oxi --version
```

**Expected:** `oxi 0.1.0b1` or higher (current beta tag at time of writing is `0.1.0b1`; later betas are `0.1.0bN`).

**Common failure: `ERROR: Could not find a version that satisfies the requirement oxi-core`** with a line above mentioning `Requires-Python >=3.11`. This means pip is bound to Python <3.11 — the venv was created with the wrong interpreter. Delete `.venv/` and redo step 2 with the explicit `python3.13` path.

**Common failure: `oxi: command not found`** after a successful install. This means the venv is not activated in the current shell. Run `source .venv/bin/activate` again.

---

## Step 4 — scaffold an adapter via the wizard

Run the 8-step wizard from inside the user's repo:

```bash
oxi init
```

The wizard prompts for:

1. **Project name** — human-readable label
2. **Adapter slug** — lowercase hyphens, becomes the package name `oxi-adapter-<slug>`
3. **GitHub repo** — `owner/name` form (e.g. `acme/widgets`); ask the user
4. **Repo root** — absolute path; defaults to current directory
5. **Roadmap location** — relative path inside the repo, defaults to `roadmap.md`. **Important:** if the user's roadmap is at `docs/roadmap.md` (common for engine repos), say so here. Wrong path = zero tasks ingest.
6. **Plan tier** — `standard`, `max_5x`, or `max_20x` (Claude plan)
7. **Budget caps** — daily soft warn, daily hard cap, per-task Opus, per-task Sonnet
8. **Dispatch policy** — max concurrent dispatches, auto-merge on/off

**Be aware of two known wizard gaps** (filed as roadmap items but not yet fixed):

- The wizard scaffolds into `$PWD/oxi-adapter-<slug>/`, so if you run `oxi init` from one directory above the repo, the scaffold lands outside the repo. **Run `oxi init` from inside the repo root.**
- The wizard does not stat the roadmap path — if the user enters `roadmap.md` but their actual file is at `docs/roadmap.md`, the install completes silently but seeds zero tasks. **Ask the user where their roadmap file lives before answering prompt 5.**

After the wizard finishes, it prints next steps. Pay attention to the `OXI_ADAPTER` line — it's the env-var pin needed in step 6.

---

## Step 5 — install the scaffolded adapter

```bash
cd oxi-adapter-<slug>
pip install -e .
cd ..
```

**Verify:**

```bash
ls oxi-adapter-<slug>/.env
```

**Expected:** the file exists. The wizard wrote it; it contains `OXI_ADAPTER=oxi_adapter_<slug>:Adapter`.

---

## Step 6 — load the adapter pin into the shell

The wizard's `.env` file is not auto-loaded by plain shells. Source it:

```bash
set -a && source oxi-adapter-<slug>/.env && set +a
```

(Tools like `direnv` and `dotenv` would auto-load this; assume the user has neither.)

**Verify:**

```bash
echo "$OXI_ADAPTER"
```

**Expected:** `oxi_adapter_<slug>:Adapter`

---

## Step 7 — confirm the adapter loads

```bash
oxi status
```

**Expected output shape:**

```
oxi 0.1.0b1
  instance:  <project-name>
  plan tier: <tier>
  repo:      <github-repo>
  killswitch: off

  budget (today): $0.00 spent / $X warn / $Y hard — ok

task counts by status:
  (no tasks)

recent events:
  (none)
```

**Common failure: `oxi: multiple adapters installed`** — another adapter (e.g. the bundled `_self` dogfood adapter) is also installed in this venv. The `.env` pin from step 6 should resolve it; verify the env var is actually set in the current shell with `env | grep OXI_ADAPTER`.

**Common failure: typo in the GitHub repo slug** — `oxi status` will show the typo'd value. Edit `oxi-adapter-<slug>/src/oxi_adapter_<slug>/adapter.py`, find `def github_repo(self)`, fix the return value, and re-run `oxi status`. The adapter is editable-installed so the change takes effect immediately.

---

## Step 8 — verify the roadmap parses

```bash
oxi v3 plan --dry-run
```

**Expected:** `found N items` where N is the number of `**T<tier>-<id> · <title>**` lines in the user's roadmap. If N is 0, the planner sees the file but its format is wrong; the user needs to follow the format documented in their roadmap header (see the `oxibyoxi` repo's own `docs/roadmap.md` as a reference).

**If you get `roadmap not found`:** the path in `roadmap_location()` doesn't match the actual file location. Edit `oxi-adapter-<slug>/src/oxi_adapter_<slug>/adapter.py`, find `def roadmap_location(self)`, fix the return value (e.g. change `roadmap.md` to `docs/roadmap.md`), and re-run `oxi v3 plan --dry-run`.

---

## Step 9 — run a reconciliation tick (no spending)

```bash
oxi v3 tick --times 1
```

**Expected:** `tick done. abandoned=0 auto_recovered=0`. This proves the engine plumbing works without spending Anthropic budget.

**Note: this command does NOT seed tasks from the roadmap.** Today, only `oxi v3 saturate` ingests + seeds. This is filed as a known issue (T0-106 in the engine's own roadmap). When the user's `oxi status` shows `(no tasks)` after this step, that's expected behavior on the current beta, not a bug in the install.

---

## Step 10 — hand off to the user

The install is complete. **Do not run `oxi v3 tick --real-claude` or `oxi v3 saturate` yourself** — those commands spend Anthropic budget and open PRs against the user's real repo. They are the user's call to make.

Tell the user:

- The install is verified up through "engine loads, adapter works, roadmap parses, no-spend tick succeeds."
- To start the engine: `oxi v3 saturate --concurrency 1 --max-cost-per-day <USD>`. This loops continuously, ingests their roadmap, dispatches workers, opens PRs.
- To stop: `Ctrl-C` (clean shutdown) or `oxi v3 kill --reason "..."` (writes a killswitch file).
- The dashboard: `oxi dashboard` opens a localhost HTML view.
- The runbook with full context: `docs/runbooks/install.md` in the OXi repo.

---

## Things you should NOT do

- **Do not run `oxi v3 tick --real-claude` or `oxi v3 saturate`** during install. Those spend money.
- **Do not commit the user's `.env` file.** It contains the adapter pin and may later contain API keys. The OXi project ignores `.env` by default; respect that.
- **Do not skip a verification.** "It probably worked" is how the user ends up debugging a broken install three steps later.
- **Do not invent flags.** If a command errors with `unrecognized arguments`, you have the wrong CLI surface — re-read the help with `oxi --help` or `oxi v3 --help`. Do not guess.
- **Do not modify code in the OXi repo itself** during install. The user's adapter lives in `oxi-adapter-<slug>/`; that is the only place install-time edits should land.

---

## Reporting failures

If a step fails and the failure is not in the "common failures" list under that step, tell the user:

1. The exact command that failed
2. The exact error output
3. Which step number you were on
4. What you tried before reporting

Do not speculate about root causes you cannot verify by running another command. Do not retry the same command without reading the error.

---

## Verifying you're following the right version of these instructions

This file lives at `https://github.com/escotilha/oxibyoxi/blob/main/AGENTS.md`. If the user gave you a link to a different fork or branch, check that the install steps you're following match the OXi version listed in `pyproject.toml` of that branch. Mismatched versions are a real source of install bugs.
