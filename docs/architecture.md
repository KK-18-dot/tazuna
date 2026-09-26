# Architecture

tazuna is four thin layers over one configuration file. Each layer has one input, one output, and one command.

```
             PROJECT.md (+ .tazuna/appendix/<target>.md)
                  │  render.py  ─ marker, {{model:…}} expansion, hand-written guard
                  ▼
   CLAUDE.md  AGENTS.md  .cursor/rules/project.mdc  GEMINI.md  …   (generated targets)

tazuna.toml ─ config.py ─────┐
user config ─────────────────┤ layered, validated, fail-closed
packs/*/pack.toml ───────────┤
built-in providers/targets ──┘
                  │
                  ▼  routing.py ─ role → provider/model/effort/timeout/read-only/fallback
                  │
                  ▼  run.py ─ boundary checks, argv from provider template, forbidden-flag check,
                  │           env allowlist, timeout / cancel + process group kill
                  │  gitstate.py ─ snapshot before/after (HEAD + content hashes + git metadata),
                  │                git run with no hooks, fsmonitor, pager or filter drivers
                  ▼
   provider CLI (claude | codex | cursor-agent | gemini | ollama | your own | mock)
                  │
                  ▼  handoff.py ─ self-report parse, failure classification, sanitize,
                  │              HANDOFF.md (0600, O_NOFOLLOW), runs.jsonl (0600, one
                  │              O_APPEND write per line), secret masks for the log
                  ▼
   evals.py ─ fixture → fresh git workdir → run → check.sh / case.toml → results.jsonl + report.md
   doctor.py ─ OK/WARN/NOTE over all of the above
   guard.py ─ generated-file guard for harness hooks
```

## Principles

**One source, generated artifacts, drift detection.** The policy is written once. Every harness file is a rendered copy with a marker. `render --check` is a CI gate, `guard` is a harness hook, and `doctor` reports drift. Editing a generated file is never the answer.

**Static routing, explicit values.** Roles are declarative. Model and effort are always passed explicitly to the provider when a role defines them, so a headless run never inherits an interactive session's model. Validation happens before launch; unknown roles, unsupported efforts, retired models, and missing providers stop with exit 2.

**Measured results, typed claims.** HANDOFF facts come from git and the process; the provider's completion claim is recorded but visibly labelled as a claim. Exit codes never depend on the claim, so automation stays predictable.

**Structural safety, not pattern matching.** The environment is an allowlist, the output path must sit inside the working directory, `$HOME` is refused as a target, delegation depth is capped, and commands that disable a harness's own approvals are rejected. These are enforced by tazuna; the provider's own sandbox handles the rest.

**Graceful absence.** Missing provider CLIs are NOTE-level unless a role depends on them; `init` only generates roles for what it finds; the mock provider makes every command usable offline.

**Small surface.** Ten subcommands, zero runtime dependencies, one config format. Extension is a directory of the same TOML tables.

## Module map

| module | responsibility |
|---|---|
| `config.py` | layered TOML loading, schema validation, env allowlist, private state directory |
| `render.py` | policy → targets, marker, adopt, check, `max_bytes` |
| `routing.py` | role resolution, provider availability, `routes` rows |
| `run.py` | boundaries, argv/env assembly, forbidden-flag check, execution, timeout, cancellation, fallback |
| `gitstate.py` | hardened git calls (`safe_git`), before/after snapshots, the `changed` lines |
| `handoff.py` | preamble, self-report parsing, failure classification, secret masking, HANDOFF.md, ledger |
| `evals.py` | suites, workdir seeding, checks, reports |
| `doctor.py` | diagnostics with OK/WARN/NOTE semantics (reuses `run.py`'s forbidden-flag check and `gitstate.py`'s git helper) |
| `guard.py` | generated-file guard and Claude Code hook adapter |
| `cli.py` | argparse front end |
| `providers/*.toml`, `targets.toml`, `templates/` | package data |

## Lineage

tazuna distils a private, multi-provider development control plane that ran for several months across Claude Code, Codex, and Cursor. The parts that proved their worth were the contract layer (one policy file rendered everywhere), a bridge that made delegation return a uniform HANDOFF plus exit code, role-based routing kept in one ledger, and code-graded evals. Everything tied to one machine (scheduling, multiplexer integration, personal quotas and model choices, hook stacks) was left out.

Design points borrowed from public projects, with credit: the typed completion vocabulary and "do not trust the report" stance popularised by Superpowers; AGENTS.md as a harness-neutral target as practised by Codex and ECC; deterministic code graders and frozen baselines with signed diffs from common eval practice.
