# smoke suite

Offline, deterministic cases that exercise the full `tazuna run` path with the built-in mock
provider. They verify the result contract (HANDOFF.md shape, status vocabulary, ledger), not model
quality. Run them after any change to tazuna itself:

    tazuna eval run evals/suites/smoke --role dry

Point the same suite at a real role to check that a provider honours the contract end to end:

    tazuna eval run evals/suites/smoke --role impl_claude
