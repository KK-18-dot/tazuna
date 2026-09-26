# Security model

tazuna launches other people's CLIs with write access to a directory. This page says exactly what it protects, how, and what it does not.

## What tazuna enforces

| Boundary | Mechanism | Failure |
|---|---|---|
| No secrets reach the provider from your shell | providers run with an **allowlisted** environment (`HOME PATH USER LOGNAME SHELL TERM LANG LC_* COLORTERM NO_COLOR TMPDIR XDG_*`), plus `TAZUNA_DEPTH` and `TAZUNA_PARENT` (the run id), which tazuna sets itself; `env_extra` names that look like secrets are refused | exit 3 |
| Write scope | `--dir` must exist and must not be `$HOME` or an ancestor; HANDOFF must be inside `--dir` and not a symlink; when it is written, its directory is opened one component at a time from `--dir` without following symlinks, so a directory the provider swapped for a symlink cannot move it outside | exit 3 before launch; a write that fails afterwards is recorded as `handoff-write-failed` (exit 1) |
| No harness bypass | each argv element is split at every `=`. It is refused when a flag name or config key contains `dangerously`, when the flag is `--yolo`, `--force`, `-f` or `-y`, when any part is `bypassPermissions`, `danger-full-access` or `yolo`, or when `bypassPermissions` / `danger-full-access` appears anywhere in it (JSON settings). Case, surrounding whitespace and quotes are ignored. That covers `--permission-mode bypassPermissions`, `--sandbox=danger-full-access`, `--config=sandbox_mode=danger-full-access`, `-c sandbox_mode=danger-full-access`, `--allow-dangerously-skip-permissions` and `--approval-mode yolo`. The task text is never inspected. `doctor` runs the same check over every provider table, so a bad definition is a WARN before anyone runs it. Built-in providers use each CLI's read-only / workspace-write modes and an empty MCP config, whose content is re-checked before every run | exit 3 |
| Read-only stays read-only | a fallback can only tighten read-only: a read-only run falls back read-only, and a fallback role that declares `read_only = true` keeps it | — |
| Runaway delegation | `TAZUNA_DEPTH` is injected and capped by `[run] max_depth` | exit 3 |
| Runaway time | timeout, then SIGTERM/SIGKILL to the process group | exit 124 |
| Orphaned providers | SIGINT / SIGTERM / SIGHUP to tazuna stop the provider's process group before tazuna exits; the run is recorded as `cancelled` and never falls back. Processes the provider left behind that still hold its output are killed with the group once the run is over | exit 130 / 143 |
| Commands planted in the repository | the provider can write `.git/config`, hooks and `.gitattributes`. Every git command tazuna runs in a project (the `changed` snapshot, `doctor`, `status`) gets the allowlisted environment, `core.fsmonitor=false`, `core.hooksPath=/dev/null`, `GIT_OPTIONAL_LOCKS=0`, `--no-pager`, no submodule recursion, and an empty override for every configured filter driver, so nothing planted there runs outside the provider's sandbox. Changes to git's config, exclude, attributes and hook files, and newly hidden index flags, are listed in `changed` | snapshot refused, noted in `changed` |
| Prompt injection through results | provider output is transcribed into HANDOFF inside a fence with fences neutralised and labelled "data, not instructions"; only the last line is parsed for the status | — |
| Secret leakage through results | token-shaped strings are masked in the HANDOFF tail, in the ledger's `task_head`, in eval `check.sh` failure reasons, and in the provider log on disk (the log is rewritten after the run; only the masks are applied there, not the fence replacement; output that arrives after that is dropped) | — |
| Task text in the ledger | the ledger's `command` holds `<task>` where the preamble + task was passed as an argument (`task_via = "arg"`); only the sanitized first line survives, as `task_head` | — |
| Local readers | the state directory and its `logs/` and `evals/` subdirectories are created 0700, log files and `runs.jsonl` 0600, by passing the mode to `open`/`mkdir` (the umask is not changed, because providers inherit it). Existing directories are not modified; `doctor` warns when the state directory belongs to another user, or when it or `runs.jsonl` is open to group/other, and prints `chmod -R go-rwx <state dir>` | doctor WARN |
| Forged HANDOFF structure | file names in `changed` that contain control or format characters, quotes or backslashes are quoted with git-style escapes, so they cannot start a new Markdown line | — |
| Secret files in the repo | `doctor` warns on tracked file **names** like `.env`, `secrets/`, `*.pem` (contents are never read) | doctor WARN |

## What it does not do

- It does not sandbox the provider. File and network isolation is the provider CLI's job (Codex sandbox, Claude Code permission modes, Cursor's agent mode). tazuna only chooses the safer of the provider's documented modes.
- It cannot stop a provider from reading secrets **inside `--dir`**. Keep `.env` and credentials out of the working tree or use the provider's own deny rules.
- Secret masking recognises a fixed set of token shapes (`sk-…`, GitHub `ghp_`/`gho_`/`ghu_`/`ghs_`/`github_pat_…`, `AKIA…`, `xox…`, `Bearer …`, `AIza…`). Anything else a provider prints stays in its log, which is why the log is private to your user.
- Shell profiles are outside the allowlist: if a provider runs commands through a login shell that exports API keys, those keys are visible to that shell. Do not export secrets from shell profiles.
- It does not stop you from running git later in a repository the provider changed. `changed` names changed hooks and git config so you can inspect them first; a provider sandbox that keeps `.git/` read-only is the stronger protection.
- The generated-file guard for harness hooks is fail-open. Hooks are hints; `render --check` in CI is the enforcement.
- Provider CLIs change flags. A wrong flag typically makes the CLI exit non-zero (status `failed`) rather than run unsafely, but review provider definitions when you upgrade a CLI.
- **Configuration is code.** `tazuna run` loads `tazuna.toml` and packs from `--dir`, and a provider table there can name any command. Running `tazuna run` in a repository you do not trust runs commands that repository chose; `doctor` and `status` never start a provider.
- Provider output is not size-capped. The timeout bounds it, and the log is read into memory once to mask it; a provider that prints gigabytes within its timeout costs that much disk and memory.
- File names in `changed` are escaped only where they could break a line. Markdown in a file name (`[text](url)`) is shown as written when HANDOFF.md is rendered.
- The forbidden-flag check reads argv elements one by one. It does not expand combined short options (`-pf`) or decode escapes (a TOML `\u002d` inside a `-c` value), and it cannot see what a wrapper script passes on. Because `bypassPermissions` and `danger-full-access` are refused anywhere in an element, a path that happens to contain them is refused too (fail closed). A provider definition, and any script it runs, is code you trust; the check catches the documented bypass switches, not a definition written to evade it.

## Reporting

See [SECURITY.md](../SECURITY.md) at the repository root.
