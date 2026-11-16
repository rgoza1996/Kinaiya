# Conservative selection-oracle uncertainty (#643)

Baseline: `7299f8b9e31b045382892a224c6a0b51c523e54b`. Requirement:
[#643](https://github.com/rgoza1996/Kinaiya/issues/643), following
[#614](https://github.com/rgoza1996/Kinaiya/issues/614). Read the
[inspection contract](../../decisions-trace-api.md) and
[selected-provider oracle contract](../../decisions-selected-provider-oracles.md).

The first hosted 3.12 failure in PR #636/run 43765825209 is preserved: 14,988
passed, one failed, nine skipped, 49 live deselected, two subtests, 2782.35s.
The negative exhaustion control saw a `started` inspection row after the original
bounded currentness refusal and failed its terminal-error assertion. The particular
hosted finish failure remains unproven.

Three frozen actual Session/SQLite/JournalClient cases initially give **two
failures and one pass in 4.29s**. Holding the finish before commit preserves an
unknown native row and zero provider requests; holding only its acknowledgement
after commit leaves matching terminal error proof and permits the existing one
successful provider request. No production deadline or predicate is replaced.

The positive selection oracle now emits a specific hard failure for the unknown
finish after all existing source/model/call/output/charge/no-dispatch checks. It
does not retry that observation or report successful classification. The negative
control proves either three confirmed errors or a bounded conservative stop on
that exact unknown capture; it does not catch unrelated assertions. Positive
selection still requires the original completed capture, exact input/output and
one provider request. Production source is byte-identical to the baseline.

Final runs, reported separately because they overlap:

- **110 passed on Python 3.12.3**, 19.44s, across six related integration modules.
- **110 passed on Python 3.13.18**, 20.18s, across the same modules.
- Both retain one existing Starlette deprecation warning; no failures or skips.
- Ruff check, format (1,048 files) and diff checks pass.

The 3.13 interpreter is the previously verified official private build, with the
same executable hash. Its cleared temporary venv was recreated in a persistent
task directory from the unchanged hash-locked binary dependencies; `pip check`
passes and runtime imports resolve to this worktree. No SDK, runtime default,
dependency lock, permission, retry, model or native accounting changes occurred.
Exact-head hosted CI remains separate. Commands, source/log hashes and the
first/final results are in [verification.json](verification.json).
