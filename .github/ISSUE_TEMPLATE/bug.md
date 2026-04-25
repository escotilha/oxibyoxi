---
name: Bug report
about: Something broke. Report it so oxi (or you) can fix it.
title: "bug: "
labels: bug
---

## What happened

Describe the bug in one or two sentences.

## Reproduction

```text
# minimal command or adapter snippet that triggers it
```

## Expected behavior

What should have happened instead.

## Environment

- oxi-core version: `oxi --version` (e.g. `0.1.0a5`)
- Python version: `python --version`
- OS: macOS / Linux / Windows
- Install mode: `pip install --pre oxi-core` / editable / other
- Adapter: `oxi-adapter-reference` / your own / other

## Ledger events (if applicable)

Paste the relevant rows from the `event` table:

```text
# .venv/bin/python -c "from oxi_core import db; h=db.connect(); [print(r) for r in h.connection.execute('SELECT created_at, kind, task_id, payload FROM event ORDER BY id DESC LIMIT 20')]"
```

## Anything else

Links, screenshots, hunches, adjacent bugs, workarounds you already tried.
