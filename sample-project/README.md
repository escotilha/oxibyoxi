# sample-project

Throwaway Python package that the reference oxi adapter drives in end-to-end tests and demos.

This is not a real product. It exists so the orchestrator's loop can be exercised against something that looks like a real project but carries no semantic weight.

## Layout

```
sample-project/
├── pyproject.toml          Standalone package, pytest configured
├── roadmap.md              Three toy tasks the adapter reads
├── src/sample_project/     Empty module plus one trivial function
└── tests/                  Pytest suite against the module
```

## Run tests

```bash
pip install -e ".[dev]"
pytest -q
```
