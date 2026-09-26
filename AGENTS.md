<!-- GENERATED-FROM: PROJECT.md by tazuna — do not edit. Edit PROJECT.md and run `tazuna render`. -->

# tazuna — PROJECT.md

<!-- Single source of project policy. AGENTS.md (the policy) and CLAUDE.md (an import of it) are
     GENERATED from this file by `tazuna render`. Edit here, then re-render. -->

## Purpose and current state

tazuna is a provider-neutral control plane for AI coding agents: one policy file rendered into every harness, static role-based routing across provider CLIs, a uniform HANDOFF + exit-code result contract, reproducible evals, and diagnostics. This repository dogfoods itself: the files you are reading were rendered by `tazuna render`. Version 0.4.0, alpha.

## Stack

| Item | Value |
|---|---|
| Language | Python 3.11+, standard library only at runtime (tomllib, subprocess, argparse) |
| Layout | `src/tazuna/` package (`cli.py`, `config.py`, `render.py`, `routing.py`, `run.py`, `handoff.py`, `evals.py`, `doctor.py`, `guard.py`), package data in `providers/*.toml`, `targets.toml`, `templates/` |
| Tests | pytest in `tests/`; shims only, never a real provider |
| Docs | `docs/*.md`; `README.md` is the front page |
| Extension | `packs/` (see `docs/packs.md`) |
| Evals | `evals/suites/smoke` (offline) |

## Commands

| Purpose | Command |
|---|---|
| Install for development | `uv venv && uv pip install -e '.[dev]'` |
| Test | `.venv/bin/pytest` |
| Lint | `.venv/bin/ruff check src tests` |
| Self-check the contract layer | `tazuna render --check && tazuna doctor` |
| Offline eval | `tazuna eval run evals/suites/smoke --role dry` |

## Quality gate (required before merge)

- [ ] `pytest` passes on Linux and macOS (CI matrix)
- [ ] `ruff check src tests` is clean
- [ ] `tazuna render --check` and `tazuna doctor` pass in this repository
- [ ] User-visible changes are in `CHANGELOG.md` and the relevant `docs/*.md`
- [ ] A provider definition change comes with an argv test against a shim

## Do not

- Add runtime dependencies. `tomllib` and the standard library are enough.
- Read, write, or log `.env`, `.env.*`, `secrets/**`, or credential files.
- Edit generated files (`CLAUDE.md`, `AGENTS.md`); edit `PROJECT.md` and run `tazuna render`.
- Add flags to provider definitions that disable a harness's approvals or sandbox (`--dangerously-*`, `--yolo`, `--force`); `run.py` rejects them.
- Let tests reach a real provider CLI; use the shims in `tests/conftest.py`.

## Known pitfalls

- `is_generated` looks at the first eight lines; a target whose frontmatter is longer will be treated as hand-written. Keep `frontmatter` short.
- Exit codes are the automation contract. The provider's self-report refines `status` only; never let it change the exit code.
- Provider CLIs change flags between releases. Verify with `tazuna run --role X --dry-run` after upgrading a CLI, and keep `experimental = true` on definitions nobody has run end to end.

## References

- Architecture: `docs/architecture.md` · Security model: `docs/security.md` · Result contract: `docs/handoff.md`
