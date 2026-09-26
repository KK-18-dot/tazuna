# Routing

## The one rule

`tazuna run --role NAME` does exactly what `tazuna routes NAME` prints. There is no runtime heuristic, no "auto" model, and no silent inheritance from your interactive session's settings. If a value is not in the role, the provider's declared default, or an explicit flag, the run does not start.

That is deliberate. Mixed-provider setups fail in two quiet ways: a headless run inherits an expensive interactive model and burns quota, or a CLI silently falls back to a default when it rejects a flag. Static roles plus fail-closed validation remove both.

## Resolution order

For each of model, effort, timeout, read-only:

1. explicit flag on `run` (`--model`, `--effort`, `--timeout`, `--read-only`)
2. the role's field in `tazuna.toml`
3. the provider's `default_model` / `default_effort` / `[run] timeout`
4. otherwise: no model or effort argument is passed (the CLI's own default), timeout 900 s

Efforts are validated against the provider's `efforts` list before anything runs. Cursor has none (effort lives in the model id suffix); Claude Code and Codex accept `low|medium|high|xhigh` (+`max` for Claude).

## Matching routes to your plan

tazuna does not call model APIs; it runs provider CLIs under whatever login or billing those CLIs already have. That makes the plan you are on a routing input:

- **Subscription CLIs** (Claude Code, Codex, Cursor, Gemini CLI on a consumer or team plan): quota is per plan, so give each provider its own role and use `fallback` to move work when one plan is exhausted (status `quota-exhausted` triggers it).
- **API-key billing**: configure the CLI for API use as its vendor documents; cost is per token, so prefer low effort and small models for `impl`-style roles and reserve strong models for `review`.
- **Local only** (`ollama`): free and offline; mark roles `read_only = true` because `ollama run` cannot edit files.
- **Mixed teams**: keep the shared `tazuna.toml` provider-neutral (roles named by purpose) and let each person map providers and model ids in their user config.

Model ids in the `init` template are placeholders. Use the ids your plan enables; unknown ids fail at the CLI with status `failed`, never silently.

## Choosing routes

A practical starting ladder, from cheap to expensive:

| role | when | typical route |
|---|---|---|
| `dry` | check the pipeline, write evals, demo | mock |
| `local` | private data, offline, classification and triage | ollama + small local model, read-only |
| `impl` | bounded implementation with a clear done condition | fastest capable coding CLI, low effort |
| `impl_hard` | cross-module changes, tricky bugs | same provider, frontier model, high effort |
| `review` | second opinion, read-only, no edits | strongest judgment model, `read_only = true` |
| `bulk_edit` | mechanical edits across many files | an IDE-agent CLI at default effort |

Raise the **model** when a judgment task is still wrong after more context and higher effort. Raise **effort** when the provider skipped files, skipped tests, or verified shallowly. Lower the model for routine, low-risk, high-volume work. "Important" or "long" is not by itself a reason to escalate.

## Fallback

`fallback = "other_role"` retries the same task once on another route, only when the first run failed for a reason unrelated to the task: the provider reported quota exhaustion or a missing login (matched against the provider's `quota_markers` / `auth_markers` in the last 64 KB of the log). Ordinary failures, timeouts and cancelled runs are never retried automatically. Both runs are recorded in the ledger, and the HANDOFF names the original route and its log.

A fallback never loosens read-only. If the first run was read-only (from the role or `--read-only`), the fallback runs read-only too; otherwise the fallback role's own `read_only` applies.

`--no-fallback` disables it for one run.

## Availability

`routes` and `doctor` check whether each provider's binary is on `PATH`. A role whose provider is missing is `unavailable`; running it exits 2 with a message rather than attempting a launch. `init` only generates roles for CLIs it finds, plus `dry`.

## Observability

`tazuna runs` lists the ledger; each row has provider, role, effective model and effort, exit, status, duration, changed files, and the log path. Because routing is static, the ledger's `role` column is enough to answer "which route is consuming which provider" without any extra instrumentation.
