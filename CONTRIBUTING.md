# Contributing

## Setup

```bash
git clone https://github.com/KK-18-dot/tazuna
cd tazuna
uv venv && uv pip install -e '.[dev]'      # or: python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check src tests
```

Tests never touch a real provider: they run in a throwaway HOME with a PATH that contains only recorded shims. Keep it that way. If you add a provider definition, add a test that asserts the exact argv the shim receives.

## What fits

tazuna does four things: render policy, route roles, record results, grade evals. Contributions that make those four smaller, safer, or clearer are welcome. Things that belong elsewhere:

- skill libraries, agent personas, workflow engines (put them in a pack, or in your harness)
- schedulers, daemons, multiplexer integrations
- anything that requires a runtime dependency

## Provider definitions

Built-in providers live in `src/tazuna/providers/*.toml`. When a CLI changes its flags, update the table, note the CLI version in the commit message, and add or adjust the argv test. Mark a provider `experimental = true` until someone has run the smoke suite against it end to end.

## Releasing

1. Bump `version` in `pyproject.toml` and `__version__` in `src/tazuna/__init__.py`; add a `CHANGELOG.md` entry.
2. `git tag -a vX.Y.Z -m "vX.Y.Z" && git push --tags`, then create a GitHub release from the tag.
3. `.github/workflows/release.yml` builds the sdist and wheel, smoke-tests the wheel in a clean environment, and publishes both to PyPI through trusted publishing with provenance attestations. The trusted publisher and the `pypi` environment are already configured; the repository variable `PYPI_TRUSTED_PUBLISHER` (`true`) gates the publish job.
4. Check the result: `pipx install --force tazuna==X.Y.Z && tazuna --version` (or the same with `python -m pip` in a fresh virtual environment).

## Commits and pull requests

- `type: description` (feat / fix / docs / test / refactor / chore). The body says why, including alternatives you rejected.
- Run `pytest` and `ruff check` before opening a PR; CI runs both on Linux and macOS.
- Document user-visible changes in `CHANGELOG.md`.
