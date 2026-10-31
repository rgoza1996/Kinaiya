# Shared typed-cleanup budget regression

The original test fails when a controlled 75 ms progress delay occurs after
the admission wait. The replacement passes that same probe and fails both
budget mutations. This does not identify the exact original CI schedule.
Production runtime source is unchanged. The small logs contain public synthetic
test text only; the local worktree path is replaced with `<worktree>`.

To reproduce in an installed development environment from the repository root:

```sh
mkdir -p .dev-run/cleanup-budget-probe
cp evaluation/evidence/typed-cleanup-budget-2026-10-31/controlled_plugin.py.txt .dev-run/cleanup-budget-probe/controlled_plugin.py
git show 364ca4caa96f1b4b0f6b8b1af504e66ce65a1b4b:tests/integration/test_converse_text_cleanup.py > .dev-run/cleanup-budget-probe/original_test.py

CLEANUP207_PROBE=delay_after_admission PYTHONPATH="$PWD:$PWD/.dev-run/cleanup-budget-probe" python -m pytest -q --tb=short -p controlled_plugin .dev-run/cleanup-budget-probe/original_test.py::test_admission_and_new_close_share_one_request_wait_budget
CLEANUP207_PROBE=delay_after_admission PYTHONPATH="$PWD:$PWD/.dev-run/cleanup-budget-probe" python -m pytest -q --tb=short -p controlled_plugin tests/integration/test_converse_text_cleanup.py::test_admission_and_new_close_share_one_request_wait_budget
CLEANUP207_PROBE=remove_admission_debit PYTHONPATH="$PWD:$PWD/.dev-run/cleanup-budget-probe" python -m pytest -q --tb=short -p controlled_plugin tests/integration/test_converse_text_cleanup.py::test_admission_and_new_close_share_one_request_wait_budget
CLEANUP207_PROBE=reset_terminal_allowance PYTHONPATH="$PWD:$PWD/.dev-run/cleanup-budget-probe" python -m pytest -q --tb=short -p controlled_plugin tests/integration/test_converse_text_cleanup.py::test_admission_and_new_close_share_one_request_wait_budget
```

Expected exits are 1, 0, 1 and 1, respectively. Mutations affect only the test
process and never edit runtime files. The expected mutation failures are the
missing admission debit and unequal terminal allowance assertions, not import
or setup errors. See [results](results.json) and [ADR0031](../../../docs/adr/0031-typed-text-cleanup.md).
