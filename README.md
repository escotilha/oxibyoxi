# oxi

A standalone, forkable autonomous coding orchestrator.

Reads a roadmap. Plans work. Dispatches parallel Claude Code sessions against a git repo. Opens pull requests. Runs a critic gate. Merges what passes. Ships a daily brief.

## Status

Pre-alpha. Private repo. Not yet on PyPI.

See [`docs/PLAN.md`](docs/PLAN.md) for the full roadmap and [`docs/anti-patterns.md`](docs/anti-patterns.md) for the design constraints.

## Install (once released)

```bash
pip install oxi-core oxi-adapter-reference
oxi init
oxi v3 tick --times 1
```

## License

Not yet chosen. Will default to MIT at 1.0.
