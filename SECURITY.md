# Security policy

tazuna launches provider CLIs with write access to a directory you choose. Its own guarantees are listed in [docs/security.md](docs/security.md).

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Use GitHub's private vulnerability reporting on this repository ("Security" tab → "Report a vulnerability"). Include the tazuna version, the provider CLI and version involved, and a minimal reproduction.

You will get an acknowledgement within a week. Fixes are released as patch versions and noted in `CHANGELOG.md`.

## Scope

In scope: anything that lets a task, a provider's output, a pack, or a configuration file escape the documented boundaries (environment allowlist, write scope, forbidden flags, depth limit, HANDOFF write path), or that leaks secrets through logs, HANDOFF.md, or the ledger.

Out of scope: vulnerabilities in the provider CLIs themselves; report those upstream.
