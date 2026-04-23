# `oxi-adapter-reference`

Reference adapter for oxi. Drives the throwaway `sample-project/` fixture — used for end-to-end smoke tests, doc examples, and dogfooding the wizard.

Forks shouldn't depend on this package. It exists as a working example of what an adapter implementation looks like.

## Install

```bash
pip install oxi-core oxi-adapter-reference
```

## Use

```python
from oxi_adapter_reference import ReferenceAdapter
from oxi_core.adapter import register_adapter

register_adapter(ReferenceAdapter(repo_root="/path/to/sample-project"))
```

After registration, any oxi engine module calls `get_active_adapter()` and reads its configuration from the reference adapter.

## What it configures

- `instance_name`: `"reference"`
- `github_repo`: `"owner/sample-project"` (placeholder — there is no real remote for the fixture)
- `roadmap_location`: `"roadmap.md"` (the file that ships with sample-project)
- `plan_tier`: `"standard"`
- Budget caps: conservative defaults, low enough that accidentally running the adapter against a real project would hit caps quickly
- Dispatch hosts: one local host, concurrency 1
