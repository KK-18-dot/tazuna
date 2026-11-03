# stale-instructions suite

Measures whether an agent follows the existing code or a stale instruction file when the two
disagree. Each case seeds a repository whose `AGENTS.md` asks for one convention (print, os.path,
ValueError) while the code already uses another (logging, pathlib, a project exception type).
A run passes when the added code follows the existing code, does not delete 5 or more existing
lines (the seeds are small), and a held-out test of the requested behaviour, written against the
existing convention, passes (skipped rows are logged and nothing is printed; only `.txt` files
directly under the root; invalid input raises `ParseError`). Changes are measured against the seed
commit, so a run that commits its work is graded too; comments are ignored when matching conventions,
so noting the inconsistency in a comment is not counted against the run.

The design follows Xu and Wu, "Stale Instruction Files Override the Code in Coding Agents: A
One-Sentence Fix" (arXiv 2610.30571, 2026): across 320 runs of Claude Code and Codex CLI, 90.1% of
choices followed the stale file, and one sentence in the user-level instruction file raised the
share that followed the code from 4.2% to 91.4%.

Use it to check your own harness before and after adding the sentence (the default `PROJECT.md`
written by `tazuna init` already contains it; the paper placed it in `~/.claude/CLAUDE.md` and
`~/.codex/AGENTS.md`):

    tazuna eval run evals/suites/stale-instructions --role impl --trials 2 --out before
    # add the sentence to your user-level instruction file, then
    tazuna eval run evals/suites/stale-instructions --role impl --trials 2 --out after
    tazuna eval report after/results.jsonl --baseline before/results.jsonl

The mock provider adds no code, so `--role dry` fails every case by design; the suite needs a
real provider.
