# oxi-core

The engine package for oxi. Project-agnostic. Ships to PyPI as `oxi-core`.

See the repo root [`README.md`](../README.md) and [`docs/PLAN.md`](../docs/PLAN.md).

## Layout

```text
src/oxi_core/
├── cli.py                 CLI entrypoint
├── adapter.py             Adapter protocol + dataclasses
├── defaults.py            Fallback constants
├── policy.py              Skill weights, plan-tier, dispatch policy
├── wizard.py              `oxi init` 8-step bootstrap (Phase 3)
├── db.py                  SQLite schema + migrations
├── planner.py             Reads roadmap, emits task plans
├── critic.py              Pre-dispatch and pre-merge review
├── prompts.py             Templated prompts
└── v3/                    The 9-step loop modules
```

Not yet implemented. Phase 0 scaffolds empty modules; Phase 1 fills them in.
