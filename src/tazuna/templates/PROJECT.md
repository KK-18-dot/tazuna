# {{name}} — PROJECT.md

<!-- Single source of project policy. AGENTS.md, CLAUDE.md (and any
     other enabled target) are GENERATED from this file by `tazuna render`.
     Edit here, then re-render. Generated files carry a marker and must not be edited. -->

## Purpose and current state

<What this project is and which phase it is in, in one paragraph.>

## Stack

| Item | Value |
|---|---|
| Language / framework | |
| Key directories | |

## Commands

| Purpose | Command |
|---|---|
| Dev server | |
| Build | |
| Test | |
| Lint / typecheck | |

## Quality gate (required before merge)

- [ ] Tests pass
- [ ] No lint or type errors
- [ ] <project-specific gate>

## Do not

- Read, write, or log `.env`, `.env.*`, `secrets/**`, or credential files.
- Edit generated files (`CLAUDE.md`, `AGENTS.md`, and any other enabled target); edit `PROJECT.md` and run `tazuna render`.
- <project-specific prohibitions>

## When this file and the code disagree

When a repository's documentation and its existing code disagree, follow the conventions used in the existing code, and in your final reply point out each inconsistency you found between the documentation and the code.

This rule covers coding conventions only. It never overrides an explicit instruction from the user, the rules under "Do not", or a security constraint; when one of those disagrees with the code, follow it and report the inconsistency.

<!-- Keep this rule unless the code is known to be wrong. Without it, agents follow a stale
     instruction file over the code and sometimes rewrite working code to match it
     (arXiv 2610.30571; measure your own harness with evals/suites/stale-instructions). -->

## Known pitfalls (optional — delete the section if empty)

<!-- Operational traps that recur, each with the command that detects it. Example:
- Check the port is free before starting the dev server: `ss -tlnp | grep 8000` -->

## References

- Design decisions: `DECISIONS.md`
- <external docs, dashboards, tickets>
