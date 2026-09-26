# Evals

tazuna evals answer one question reproducibly: *does this route, on these tasks, produce results that pass deterministic checks?* They run through the same `run` path as real work, so they also test the contract itself.

## Layout

A suite is a directory; each case is a subdirectory:

```
evals/suites/smoke/
  README.md
  handoff-contract/
    task.md          # the task text (required)
    case.toml        # expectations and per-case overrides (optional)
    check.sh         # code grader; exit 0 = pass (optional)
  read-only-run/
    seed/            # copied into a fresh git repo before the run (optional)
    task.md
    case.toml
    check.sh
```

`case.toml` keys:

```toml
role = "impl"                 # default role when --role is not given
read_only = true
timeout = 300
expect_exit = 0
expect_status = "done"        # see docs/handoff.md for the vocabulary
expect_output_regex = "..."   # searched in the provider log (DOTALL)
expect_files = ["src/x.py"]   # must exist in the workdir after the run
```

`check.sh` runs with the workdir as `$1` and cwd, and the environment `TAZUNA_LOG`, `TAZUNA_HANDOFF`, `TAZUNA_EXIT`, `TAZUNA_STATUS`. Use it for anything a regex cannot express: run the project's tests, assert only certain files changed, check the HANDOFF sections. When it fails, the last line of its output becomes the failure reason in `results.jsonl` and `report.md`, with token shapes masked.

## Running

```bash
tazuna eval run evals/suites/smoke --role dry                # offline
tazuna eval run evals/suites/smoke --role impl --trials 3    # real provider, 3 trials per case
tazuna eval run my-suite --only 'auth|orders' --out results/2026-09-13
```

Each trial gets a fresh workdir (`seed/` copied, `git init`, one commit) under the results directory, so `changed` in the HANDOFF is meaningful and a bad run cannot touch your repository. Results are appended to `results.jsonl`; `report.md` is regenerated at the end. The command exits 1 if any trial did not pass, including trials lost to quota or login problems, because nothing was measured for them.

Each row of `results.jsonl` carries `run_id`, the id of the run in the ledger (`runs.jsonl`), so a result can be joined with its ledger row, its log and its HANDOFF.

## Reporting and baselines

```bash
tazuna eval report results/2026-09-13/results.jsonl
tazuna eval report results/2026-09-13/results.jsonl --baseline results/2026-09-01/results.jsonl
```

The report is a markdown table per case: route, pass count, pass rate, the number of excluded runs (quota, login or cancelled), and, with `--baseline`, the baseline rate and a **signed** difference so regressions and improvements surface together. Freeze a baseline whenever you change a role, a provider definition, or `PROJECT.md`, and compare against it before adopting the change.

In CI, let the comparison decide:

```bash
tazuna eval report results/new/results.jsonl --baseline results/base/results.jsonl --fail-on-regression
```

`--fail-on-regression` prints the report as usual and exits 1 when any case present in both files has a lower pass rate than in the baseline (each regression is also printed on stderr). It also exits 1 when the new results cannot speak for the baseline, and names why on stderr as `incomplete: …`: a trial was cancelled or excluded, or a baseline case is missing. Otherwise an interrupted suite, a provider that signals its own evaluator, or a failed trial whose output happens to contain a quota or login word would pass by leaving runs out. Rerun after an outage instead of gating on partial results. New cases that the baseline does not have are reported but not compared, and a case whose every baseline run was excluded is named as `not compared: …` without failing. It requires `--baseline` (exit 2 otherwise).

## Design rules

- **Code graders first.** A check script that runs the project's own tests is worth more than any rubric. Add an LLM judge only for axes code cannot grade (write it as a `check.sh` that calls `tazuna run --role review --read-only` and parses the answer).
- **One lever per suite.** Keep suites that measure behaviour (scope discipline, honesty about tests) separate from suites that measure capability, so a change in one does not blur the other.
- **Exclude non-capability failures.** Runs with status `quota-exhausted`, `auth-required` or `cancelled` say nothing about the route's capability, so `eval report` leaves them out of pass rates and lists them in the `excluded` column and section. A cancelled trial (Ctrl-C, SIGTERM) also stops `eval run`: the row is written, the report is rendered for what ran, and the command exits 130 / 143 without starting the next trial. `timeout` is counted as a failure: a route that is too slow for the case did not deliver. Raise the case's `timeout` if the limit itself is wrong.
- **Trials.** Use `--trials 3` before calling a route "reliable" for a case.

## The bundled smoke suite

`evals/suites/smoke` runs offline with the mock provider and asserts the HANDOFF contract, read-only discipline, and seeded workdirs. It is part of the test suite; run it after changing tazuna. Point it at a real role to confirm a provider honours the contract end to end.
