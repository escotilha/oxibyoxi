# Adapter

An **adapter** is a ~70-line Python package that configures oxi for your project. It implements the `Adapter` protocol — ten methods that tell the engine where the repo lives, what budget limits to enforce, how many sessions to run concurrently, and whether to auto-merge approved PRs.

The wizard (`oxi init`) scaffolds a working adapter. This page explains every method so you can tune the scaffold after the initial setup.

---

## File layout

The wizard creates:

```
oxi-adapter-my-app/
├── pyproject.toml          declares the oxi.adapters entry-point
└── src/
    └── oxi_adapter_my_app/
        ├── __init__.py
        └── adapter.py      implement all 10 methods here
```

Install with `pip install -e .` (editable so you can edit `adapter.py` without reinstalling).

---

## The 10 methods

### `naming() → NamingConfig`

Human-readable identity shown in the dashboard, briefs, and log output.

```python
def naming(self) -> NamingConfig:
    return NamingConfig(
        instance_name="My App",       # shown in dashboard title and oxi status
        session_code_regex=r"[a-z]{2}\d*",  # validates worktree session tags
        branch_prefixes=("feat/", "fix/", "chore/"),  # recognized by session-end hook
    )
```

### `paths() → PathsConfig`

On-disk paths the engine writes to. Fields set to `None` fall through to defaults computed relative to `repo_root`.

```python
def paths(self) -> PathsConfig:
    return PathsConfig(
        repo_root="/path/to/your-project",  # absolute path to local checkout
        oxi_dir=".oxi",                     # subdirectory for state files
        db_path=None,       # default: <repo_root>/.oxi/oxi.db
        brief_path=None,    # default: <repo_root>/.oxi/brief.md
    )
```

### `budget() → BudgetCaps`

Spend limits in USD. The engine logs a warning above `daily_soft_warn` and halts dispatch above `daily_hard_cap`. Per-task ceilings abort individual sessions that run over.

```python
def budget(self) -> BudgetCaps:
    return BudgetCaps(
        daily_soft_warn=5.0,    # TUNE: log a warning above this
        daily_hard_cap=20.0,    # TUNE: halt dispatch above this
        per_task_opus=2.0,      # TUNE: ceiling per Opus-based task
        per_task_sonnet=0.50,   # TUNE: ceiling per Sonnet-based task
    )
```

Start conservative. Raise caps once you have watched several dispatches complete and know your typical per-task cost.

### `github_repo() → str`

The `owner/name` slug oxi pushes branches and opens PRs against.

```python
def github_repo(self) -> str:
    return "acme/my-app"
```

### `roadmap_location() → str`

Path to the roadmap markdown, relative to `paths().repo_root`.

```python
def roadmap_location(self) -> str:
    return "roadmap.md"
```

### `branch_prefixes() → tuple[str, ...]`

Branch prefixes the session-end hook recognizes as oxi-managed branches.

```python
def branch_prefixes(self) -> tuple[str, ...]:
    return ("feat/", "fix/", "chore/")
```

### `dispatch_hosts() → tuple[DispatchHost, ...]`

Hosts where the engine spawns Claude sessions. `ssh_alias=None` means local. Set an SSH config alias for remote dispatch.

```python
def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
    oxi_dir = Path(self.paths().repo_root) / ".oxi"
    return (
        DispatchHost(
            name="local",
            ssh_alias=None,         # None = local machine
            max_concurrent=3,       # TUNE: raise for faster hardware
            worktree_root=str(oxi_dir / "worktrees"),
        ),
    )
```

### `promote_recipe() → PromoteRecipe | None`

Optional daily-promote configuration. Return `None` to skip the promote phase entirely (the default for most projects).

```python
def promote_recipe(self) -> PromoteRecipe | None:
    return None  # no daily promote
```

### `plan_tier() → str`

The Claude plan tier the engine assumes. **This method has no default** — the engine refuses to start if it returns an empty string (anti-pattern #3: plan tier must be explicit).

```python
def plan_tier(self) -> str:
    return "standard"   # or "max_5x" or "max_20x"
```

| Tier | Token rate | When to use |
|---|---|---|
| `standard` | 1× | Solo projects, low volume |
| `max_5x` | 5× | Teams, moderate throughput |
| `max_20x` | 20× | High-velocity orgs |

### `policy() → DispatchPolicy`

Behavior toggles. `auto_merge` is the most important one.

```python
def policy(self) -> DispatchPolicy:
    return DispatchPolicy(
        auto_merge=False,       # TUNE: set True only after watching the critic
        tier_zero_absorb=True,  # new T0 items pause lower-tier work for one tick
    )
```

See [Safety rails — auto_merge discipline](safety-rails.md#auto_merge-discipline) before enabling `auto_merge=True`.

---

## Adapter discovery

oxi finds your adapter automatically via the `oxi.adapters` entry-point declared in the scaffold's `pyproject.toml`:

```toml
[project.entry-points."oxi.adapters"]
my-app = "oxi_adapter_my_app:Adapter"
```

If you have two adapter packages installed in the same virtualenv, pin the one you want:

```bash
OXI_ADAPTER=oxi_adapter_my_app:Adapter oxi status
```

The env var always takes priority over entry-point discovery.

---

## Editing the adapter

After editing `adapter.py`, no reinstall is needed if you installed with `pip install -e .`. The next `oxi` invocation reads the updated file directly.

After changing budget caps or plan tier, verify with:

```bash
oxi status
```

After changing roadmap location, verify with:

```bash
oxi v3 plan --dry-run
```
