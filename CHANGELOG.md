# Changelog

## 0.3.0 — 2026-09-26

### Upgrade notes

- The project is renamed from `agentplane` to `tazuna` (手綱, "the reins"). There is no compatibility layer: the package, the command, the module, the config file, the environment variables and the self-report line all change, and the old names are not read. To move a project:
  - `pipx uninstall agentplane && pipx install tazuna` (or the same with pip).
  - Rename `agentplane.toml` to `tazuna.toml`, a `.agentplane/` directory to `.tazuna/`, and `~/.config/agentplane/` to `~/.config/tazuna/`. `tazuna doctor` warns while any of these is left, and a missing `tazuna.toml` next to an `agentplane.toml` is reported as a rename, not as "run init".
  - Replace `agentplane` with `tazuna` in CI, Makefiles, scripts and hook commands (`tazuna guard --hook claude`), and `AGENTPLANE_` with `TAZUNA_` in scripts and eval `check.sh` files.
  - Replace `AGENTPLANE-STATUS:` with `TAZUNA-STATUS:` wherever it is written or matched: policy text that tells agents how to report, pack provider scripts, `[providers.mock] response`, and eval `expect_output_regex`. An old line is not recognized, so a run that reported BLOCKED under the old name is recorded as `done`.
  - Run `tazuna render` and commit the result. The generated header now names tazuna, so `render --check` reports drift until you do; the files themselves are still recognized as generated, because the marker does not carry the tool name.
- The run ledger moves to `$XDG_STATE_HOME/tazuna`; the old ledger stays where it was.

### Fixed

- `agentplane guard --hook claude` and `agentplane guard --print-hook` failed with "unrecognized arguments" and exit 2. The hook command that `--print-hook` and the `guard` docstring describe would therefore have made Claude Code block every Edit / Write / MultiEdit call. `--hook` and `--print-hook` are now real options, the paths form is unchanged, and tests call `guard` through the CLI.
- An unknown `--hook` value no longer exits 2 (which a harness reads as a block); guard warns on stderr and allows, as the rest of the hook does.

### Added

- The quickstart shows how to install the Claude Code hook with `tazuna guard --print-hook`.

## 0.2.0 — 2026-09-17

### Upgrade notes

- The forbidden-flag check is stricter (see Fixed). A provider table that passed `-f`, `-y`, `--force=…` or a `bypassPermissions` / `danger-full-access` value is now refused with exit 3, and `doctor` names it.
- New exit codes 130 / 143 and status `cancelled`. Scripts that treated any non-zero exit as a provider failure keep working; scripts that match statuses should add `cancelled`.
- The ledger gains `parent`; `command` holds `<task>` for argument-passing providers. Existing fields keep their meaning.

### Fixed

- `changed` no longer misses real changes. The before/after snapshot now records `HEAD` and a content hash per dirty path (untracked directories expanded), so a second edit to a file that was already modified, a new file inside an already-untracked directory, and commits made by the provider (`commits: a..b (N)` plus `committed <S> <path>` lines) all show up. Paths that were dirty before and are clean now are listed too. Entries stay plain strings in the ledger.
- Run ids carry a random suffix and log files are created exclusively (`O_EXCL`), so eval trials started by one process within the same second no longer share, and overwrite, one log.
- A fallback no longer runs a `read_only = true` fallback role writable when the original route was writable. Fallback can only tighten read-only.
- Cancelling agentplane no longer leaves the provider running. SIGINT / SIGTERM / SIGHUP stop the provider's process group, and the run is still recorded (HANDOFF + ledger) with the new status `cancelled` and exit code 130 (SIGINT) or 143 (SIGTERM, SIGHUP). Cancelled runs never trigger the fallback. The task is now fed to the provider's stdin from a thread, so a provider that does not read a large task can no longer block the timeout.
- The forbidden-flag check matched exact strings only. It now also refuses `--dangerously-*=…`, `--force=…`, the short forms `-f` / `-y`, and the values `bypassPermissions`, `danger-full-access` and `yolo` as separate arguments or after `=` (covering `--permission-mode bypassPermissions`, `--sandbox=danger-full-access`, `-c sandbox_mode=danger-full-access`, `--approval-mode yolo`). Task text is no longer checked, so a task reading `--force` is not refused. `doctor` warns about such flags in any provider table.
- The ledger no longer stores the full task for `task_via = "arg"` providers (cursor, gemini): `command` records `<task>` in its place, and `task_head` is masked like the HANDOFF. Ledger lines are appended with a single `os.write` on an `O_APPEND` descriptor.
- State files were world-readable. The state directory, `logs/` and `evals/` are now created 0700, log files and `runs.jsonl` 0600, without touching the umask the provider inherits. `doctor` warns when an existing state directory or ledger is open to group/other and prints the `chmod` fix. Token shapes are masked in the log on disk after each run.
- `eval report` now implements the documented rule: runs with status `quota-exhausted`, `auth-required` or `cancelled` are excluded from pass rates and counted in a new `excluded` column. `timeout` still counts as a failure, and `docs/evals.md` now says so.
- Ctrl-C during `eval run` stopped only the current trial and the suite went on, with the cancelled trial counted as a failure. A cancelled trial now stops the suite (exit 130 / 143) after its row and the report are written, even when its HANDOFF could not be written.
- A task that is not valid UTF-8 crashed after the provider had started and left no ledger row (and, for stdin providers, a provider waiting until the timeout). It is now refused before launch with exit 2 (so is a task containing a NUL byte, which crashed arg-passing providers), whether it came as an argument, on stdin or through `--task-file` (which printed a traceback). Any error while writing the HANDOFF now ends as `handoff-write-failed` with a ledger row, and the provider's stdin is always closed.
- A provider's short final lines, including its `AGENTPLANE-STATUS` line, could be lost when a process it started detached and kept the output pipe open. Output is now logged as soon as it is readable.
- A signal that arrived while the HANDOFF and ledger row were being written could kill agentplane between the two. Both are now written first. When a signal stops a run before its fallback, the HANDOFF describes that run instead of keeping an older file.

### Security

Findings from a review of the changes above, fixed before release:

- The git commands behind `changed`, `doctor` and `status` no longer run commands a provider planted in the repository (`core.fsmonitor`, filter drivers, the post-index-change hook), and they get the allowlisted environment instead of agentplane's own. A filter driver whose name cannot be overridden makes the snapshot refuse, with a note in `changed`.
- File names in `changed` are quoted with git-style escapes when they contain control or format characters, quotes or backslashes, so a file name can no longer add sections to the HANDOFF. A trailing CR in a name is hashed correctly.
- `changed` also lists newly set `skip-worktree` / `assume-unchanged` flags and changes to git's `config`, `info/exclude`, `info/attributes` and `hooks/*`.
- With a nested `--out` (`--out dir/a/b/HANDOFF.md`), a provider that replaced `dir/a` with a symlink could make agentplane write the HANDOFF outside `--dir`. The output directory is now opened one component at a time from `--dir` without following symlinks (present since 0.1.0).
- A failing or slow snapshot (for example a huge file) no longer loses the run: files above 8 MiB are fingerprinted by size and mtime, FIFOs and devices are never read, and any snapshot error becomes a note in `changed`.
- A signal that arrives while output is still being collected cancels the run (no fallback). Processes the provider left holding its output are killed with its process group when the run is over, and later output is dropped instead of reaching the log unmasked.
- The forbidden-flag check also splits at every `=` (`--config=sandbox_mode=danger-full-access`), ignores surrounding whitespace, refuses any flag or config key containing `dangerously` (`--allow-dangerously-skip-permissions`), and refuses `bypassPermissions` / `danger-full-access` anywhere in an element.
- The empty MCP config handed to Claude Code is re-checked before each run, model ids may no longer start with `-`, `doctor` warns when the state directory belongs to another user, `check.sh` failure reasons are masked, and `eval report --fail-on-regression` names the baseline cases it could not compare.

### Added

- Run lineage: the ledger has a `parent` field, taken from `AGENTPLANE_PARENT` when it is a plain token (otherwise ignored with a warning). agentplane sets `AGENTPLANE_PARENT` to the current run id for every provider it launches, so nested delegation is traceable from the ledger, and scripts or CI can set it to their own id.
- `agentplane run --json` prints the final ledger record as one JSON object on stdout (no provider echo, no `HANDOFF:` line).
- `docs/handoff.md` documents the ledger fields as a stable interface and the `status` values as a closed vocabulary.
- `agentplane eval report RESULTS --baseline BASE --fail-on-regression` exits 1 when a case present in both files has a lower pass rate than the baseline, or when the results are incomplete (a cancelled or excluded trial, or a missing baseline case), for CI. A subset run (`--only`) against a full baseline, or a run during a provider outage, therefore fails the gate.
- Eval result rows carry `run_id`, the ledger id of the run.
- `max_bytes` on render targets. The built-in `codex` target sets 32768, the size at which Codex CLI stops reading `AGENTS.md`. An oversized target is still written with a warning, fails `render --check` as `too-large`, and is a `doctor` WARN.

### Internal

- The git snapshot and the hardened git helper moved from `agentplane.run` to `agentplane.gitstate`. The Python modules are not a stable interface; the CLI, HANDOFF and ledger are.

## 0.1.0 — 2026-09-13

First public release, available on PyPI (`pipx install agentplane`).

- `init`, `render` (`--check`, `--adopt`, `--force`, appendices, `{{model:…}}` expansion), built-in targets for Claude Code, Codex/AGENTS.md, Cursor, Gemini CLI, Copilot, Windsurf, Cline.
- `run` with role-based routing, explicit overrides, allowlisted environment, timeout, safety boundaries, typed self-report, failure classification, one-shot fallback, HANDOFF.md, JSONL ledger.
- Built-in providers: claude, codex, cursor, gemini (experimental), ollama, mock.
- `routes`, `doctor` (OK/WARN/NOTE), `status`, `runs`, `packs`, `guard` (Claude Code hook adapter).
- `eval run` / `eval report` with fixture suites, code graders, signed baseline diffs; bundled offline smoke suite.
- Packs: directories of the same TOML tables, with `{pack_dir}` and pack-relative appendices.
