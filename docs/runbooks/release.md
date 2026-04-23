# Release runbook

How to cut a new oxi-core or oxi-adapter-reference release.

## Prerequisites

- `PYPI_API_TOKEN` in macOS Keychain under service name `PYPI_API_TOKEN`.
  Optional `TESTPYPI_API_TOKEN` for TestPyPI dry-runs.
- `build` and `twine` installed in the active venv: `pip install build twine`.
- Clean git working tree. The release script refuses to proceed otherwise.

## Workflow

1. Bump the version in the target package's `pyproject.toml`.
2. Commit and push. CI must be green on `main`.
3. Release:

   ```bash
   scripts/release.sh oxi-core
   # or
   scripts/release.sh adapters/_reference
   ```

   The script:
   - runs leak-lint
   - cleans `dist/` and `build/`
   - builds sdist + wheel
   - installs the wheel in a throwaway venv and imports the package (catches packaging errors)
   - reads the Keychain token, invokes `twine upload`
   - prints the PyPI URL

4. Verify the release on PyPI.

## Dry-run on TestPyPI

```bash
scripts/release.sh oxi-core --test
```

Requires `TESTPYPI_API_TOKEN` in Keychain. TestPyPI has its own account/token — sign up at https://test.pypi.org separately.

## Rollback

- **Bad release:** `pip install twine` on your machine, then
  `twine upload --skip-existing` is a no-op. To remove a bad version from PyPI, yank it from the web UI at `https://pypi.org/manage/project/<pkg>/`.
- **Yanked versions are not reused.** Bump the version and try again; never attempt to re-upload a yanked version.

## First-time name reservation (done 2026-04-23)

`oxi-core` and `oxi-adapter-reference` were reserved on PyPI with empty `0.0.0` placeholders on 2026-04-23. Future `0.1.0+` releases are real.
