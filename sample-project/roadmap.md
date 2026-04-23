# sample-project roadmap

Throwaway work items that the orchestrator reads during end-to-end smoke tests. Deliberately trivial — this is a test fixture, not a real product backlog.

## Tier 0

**T0-1 · add a greet function**
_a pure function that returns "hello, {name}"_

**T0-2 · add a changelog stub**
_create `CHANGELOG.md` with a single "## unreleased" heading_

## Tier 1

**T1-1 · expose the greet function in `__all__`**
_update the package `__init__` so `from sample_project import greet` works_
