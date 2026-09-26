# Configuration

tazuna reads TOML from four layers. Later layers override earlier ones table-by-table.

| Layer | Where | Purpose |
|---|---|---|
| built-in | package data | provider definitions, render targets, `[run]` defaults |
| user | `$XDG_CONFIG_HOME/tazuna/config.toml` (default `~/.config/tazuna/config.toml`) | personal provider choices; never committed |
| packs | directories listed under `[packs] paths` | shareable bundles of providers, targets, roles, appendices |
| project | `tazuna.toml` in the project root | the team's policy file name, targets, models, roles |

`tazuna doctor` prints the layers it loaded.

## `[project]`

```toml
[project]
name = "my-service"
policy = "PROJECT.md"    # the single source that gets rendered
```

## `[render]`

```toml
[render]
targets = ["claude-shim", "codex"]   # the default written by `tazuna init`
```

Built-in targets and their output paths:

| target | path |
|---|---|
| claude-shim | `CLAUDE.md` with one `@AGENTS.md` import instead of a copy (needs `codex`; default) |
| claude | `CLAUDE.md` with its own copy of the policy (use instead of `claude-shim`, not with it) |
| codex | `AGENTS.md` (also read by Cursor and many other agents; default) |
| cursor | `.cursor/rules/project.mdc` (with frontmatter) |
| gemini | `GEMINI.md` |
| copilot | `.github/copilot-instructions.md` |
| windsurf | `.windsurfrules` |
| cline | `.clinerules` |

Each target appends `.tazuna/appendix/<target>.md` if that file exists. Define or override a target:

```toml
[targets.mytool]
path = "docs/MYTOOL.md"
frontmatter = "---\nkind: rules\n---\n"
appendix = ".tazuna/appendix/mytool.md"
max_bytes = 65536        # optional; see below
```

### `max_bytes`

Some harnesses read an instruction file only up to a fixed size and silently drop the rest. Codex CLI reads at most `project_doc_max_bytes` of `AGENTS.md` (32768 bytes in Codex CLI 0.153.4), so the built-in `codex` target sets `max_bytes = 32768`. The other built-in targets set no limit; add one when the harness you use documents a limit.

When the rendered file (frontmatter, marker, policy and appendix together) is larger than `max_bytes`:

- `tazuna render` still writes it and prints a `WARNING` line (exit 0);
- `tazuna render --check` reports `TOO-LARGE` and exits 1, like drift;
- `tazuna doctor` reports a WARN.

If you raised the limit in the harness itself (for Codex, `project_doc_max_bytes` in its `config.toml`), raise it here too, for example `[targets.codex] max_bytes = 65536` in `tazuna.toml`. The value must be a positive integer.

## `[models]`

Aliases used by roles and by `{{model:ALIAS}}` tokens in `PROJECT.md`. Values are literal ids for the provider that runs them; which ids work depends on your plan with that provider (subscription tier or API access), so treat the ids `init` writes as examples and put personal choices in the user config layer. `retired` lists ids that must never reappear (checked by `doctor`). An id may use letters, digits and `. _ : / -` plus an optional `[1m]` suffix, and must not start with `-`, because it is passed as the value after a flag such as `--model`.

```toml
[models]
fast = "claude-sonnet-5"
strong = "claude-opus-5"
local = "gemma4:e2b"
retired = ["claude-opus-4-8"]
```

## `[roles.*]`

```toml
[roles.impl]
provider = "codex"        # required; a defined provider
model = "codex_default"   # alias or literal id; optional if the provider has default_model
effort = "low"            # must be listed in the provider's `efforts`
timeout = 900             # 1..7200 seconds; default [run] timeout
read_only = false         # true = provider is asked not to modify files
fallback = "impl_claude"  # role retried once on quota-exhausted / auth-required
```

Role names match `^[a-z][a-z0-9_-]{0,31}$`.

## `[providers.*]`

Built-in: `claude`, `codex`, `cursor`, `gemini` (experimental), `ollama`, `mock`. Any table with the same name overrides the built-in field by field; new names add providers.

```toml
[providers.opencode]
description = "OpenCode CLI"
binary = "opencode"                              # what `doctor` looks for (default: command[0])
command = ["opencode", "run", "--model", "{model}"]
model_args = []                                  # appended when a model is set
effort_args = []                                 # appended when an effort is set
efforts = []                                     # empty = effort unsupported
read_only_args = []                              # appended when read_only
write_args = []                                  # appended when not read_only
write_mode = ""                                  # value of {permission_mode} when writing
read_only_mode = ""                              # value of {permission_mode} when read-only
task_via = "arg"                                 # "stdin" (default) or "arg"
task_stdin_marker = ""                           # e.g. "-" for CLIs that need it
env_extra = []                                   # extra env names forwarded (never secret-like)
quota_markers = ["rate limit"]                   # lower-cased substrings → status quota-exhausted
auth_markers = ["not logged in"]                 # → status auth-required
default_model = ""                               # used when a role sets none
default_effort = ""
experimental = true
```

Placeholders available in `command` and the `*_args` lists: `{model}`, `{effort}`, `{task}`, `{dir}`, `{permission_mode}`, `{empty_mcp}` (path to an empty MCP config), and, inside packs, `{pack_dir}`.

With `task_via = "arg"`, `{task}` is substituted where it appears; if it appears nowhere the task is appended as the last argument.

tazuna refuses (exit 3) any provider argv element that switches off the harness's own approvals or sandbox:

- a flag name or config key containing `dangerously` (`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `-c dangerously_bypass_approvals_and_sandbox=true`), or the flags `--yolo`, `--force`, `-f`, `-y` (also written `--flag=value`);
- the values `bypassPermissions`, `danger-full-access`, `yolo`, as a separate element (`--permission-mode bypassPermissions`) or as any `=`-separated part (`--sandbox=danger-full-access`, `--config=sandbox_mode=danger-full-access`); `bypassPermissions` and `danger-full-access` are also refused anywhere inside an element, such as JSON passed to `--settings`. Quotes and surrounding whitespace are stripped and case is ignored.

The check covers `command`, `model_args`, `effort_args`, `read_only_args`, `write_args`, `write_mode`, `read_only_mode` and `task_stdin_marker`, after placeholder expansion. The task text is not inspected. `tazuna doctor` applies the same check to every provider table (built-in, user, pack, project) and reports a WARN, so a bad definition shows up before anyone runs it. `-f` and `-y` are refused for every provider, because Cursor and Gemini CLI use them as short forms of `--force` and `--yolo`; if your own CLI uses them for something harmless, use its long option instead.

### Mock provider

`kind = "mock"` runs nothing. Useful for tests, evals of the pipeline itself, and trying tazuna without any CLI.

```toml
[providers.mock]
response = "custom output\nTAZUNA-STATUS: DONE"
exit_code = 0
[providers.mock.writes]
"notes/out.md" = "content written when not read_only"
```

Environment overrides: `TAZUNA_MOCK_RESPONSE`, `TAZUNA_MOCK_EXIT`, `TAZUNA_MOCK_SLEEP`.

## `[run]`

```toml
[run]
timeout = 900
min_output_bytes = 40   # exit 0 with less output → exit 4
max_depth = 2           # TAZUNA_DEPTH guard for nested delegation
env_extra = []          # names forwarded to providers in addition to the allowlist
```

Allowlist always forwarded: `HOME PATH USER LOGNAME SHELL TERM LANG LC_ALL LC_CTYPE COLORTERM NO_COLOR TMPDIR XDG_*`. Names containing KEY, TOKEN, SECRET, PASS, CRED, AUTH, COOKIE, SESSION, PRIVATE, BEARER, ACCESS or ending in `_PAT` are refused in `env_extra`.

## `[packs]`

```toml
[packs]
paths = ["packs/example-pack", "~/tazuna-packs/team"]
```

See [packs.md](packs.md).

## Environment variables

| Variable | Effect |
|---|---|
| `TAZUNA_STATE_DIR` | where logs, `runs.jsonl`, and eval results go (default `$XDG_STATE_HOME/tazuna`); created with mode 0700 when missing |
| `XDG_CONFIG_HOME` | location of the user config layer |
| `TAZUNA_DEPTH` | set by tazuna for providers it launches; do not set by hand |
| `TAZUNA_PARENT` | set by tazuna for providers it launches (the id of the launching run) and recorded as `parent` by a nested `tazuna run`. Scripts may set it to their own id (1-200 characters of `A-Z a-z 0-9 . _ : / @ + -`); other values are ignored with a warning. Not a secret |
| `TAZUNA_MOCK_RESPONSE`, `TAZUNA_MOCK_EXIT`, `TAZUNA_MOCK_SLEEP` | override the mock provider (see above) |
