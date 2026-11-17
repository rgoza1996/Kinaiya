# Internal Agent advice lifecycle — #566 / #567

This increment continues draft PR #689 from
`6c718b67a0d0ec51a8f483d30d677af4236488c7`. The
[operation contract](../../decisions-agent-operations.md) records implemented
internal behavior and the boundaries required before public admission. The owner
is not installed in the service; the policy inspector remains unavailable for
requests. No parent feature is complete.

## Verification collected

The same **1,559 tests pass on Python 3.13.18 and Python 3.14.10** across 35
unit/integration files. Both final receipts preserve all 937 Python source pins,
the exact command, unchanged input observations and the existing Starlette
deprecation warning. There are no skips in this union. Tests exercise synthetic
model wire responses through the actual measured LM Studio adapter, native Session
SQLite/ASGI admission, exact task/source/body binding, independent policy/catalog
ownership, Agent resource grouping, observer and original cancellation, unknown
task enrollment, raw output preservation and actual cleanup. HTTP completion of
one advice operation is independent of unrelated work held by its Resources parent.

Ruff check passes; format check reports **1,244 files already formatted**. Contract
export passes with the worktree on `PYTHONPATH`. An earlier direct invocation
without that path selected the environment's older installed contracts and failed
at import; no dependency or source change was needed. `git diff --check` passes.
[Equivalence](equivalence.json) preserves all **608 inherited schemas byte-for-byte**,
adds exactly four operation schemas and retains all 74 original inventory assertion
ASTs and all 25 original native-admission assertion ASTs.

## Preserved first failures

- **#692:** one failure and 17 passes: observing the original's pre-entry
  cancellation raised the observer's cancellation instead of returning the native
  observation. Cancellation-count comparison now distinguishes a newly cancelled
  observer and preserves its earlier cancellation count. The focused repair has
  19 passes; a held-model control verifies genuine facade cancellation without
  cancelling or repeating the original.
- **#693:** one failure and 30 passes: a task factory enqueued the original and
  raised without returning its handle. Later fixture-controlled task cleanup
  erased `setup_unconfirmed` and released capacity. The owner's unknown-registration
  sentinel now survives the orphan's exit. The immediate repaired union has 31
  passes. Finalization also removes the direct returns flagged by Ruff B012.
- **#694:** one failure and 39 deselected: the fresh task context dropped the
  trusted native Resources actual-operation owner. Explicit parent enrollment
  retains the original and its own actual-operation hub through all known task,
  adapter and metadata handles. Unknown cleanup keeps that parent failed and held.
  The immediate repaired union has 41 passes.
- **#695:** the first metadata-close control fails once, with 55 deselected:
  cancelling the public proof wait lets the native owner retire before the held
  response close finishes. A retained strict metadata exchange first passes all
  56 admission controls. Two additional operation controls then fail, with 41
  deselected: the advice original still publishes confirmed cancellation before
  that actual close. Its per-original actual-operation hub now keeps observation
  and capacity pending, or explicitly unconfirmed after close failure. The
  immediate repaired combined union has 99 passes; the final 1,559 union adds a
  successful actual HTTP/parent-independence control.

[Verification manifest](verification.json) identifies 15 exact runs, five original
source captures, final source pins and targets. Each `logs/*.log.json` reconstructs
the complete original UTF-8 log and matches its recorded SHA-256; receipts retain
their original child exit codes and input pins. The orchestration wrapper exits
zero even when its pytest child fails; the receipt's `exit_code` is authoritative.
First-source captures retain the exact tracked patch, complete new Python source
and baseline/pins. No first failure or assertion was rewritten to pass.

## CI and remaining scope

Published predecessor `6c718b67a0d0ec51a8f483d30d677af4236488c7` has eight standard
jobs and six installer jobs passing, with four manual jobs skipped. Those runs
qualify that exact predecessor, not this new operation owner. Local Python 3.11
and 3.12, the complete project suite, frontend/device, physical audio and live/paid
model checks were not run for this increment. Existing pinned environments were
used without installations, SDK changes, owner-runtime promotion or system-policy
changes. The held #624 qualification run was not retried.

Earlier predecessor `2879743ec409187bc5d2c1ad12e6ad854dc2dabe` has a real Python
3.13 lost-finish-acknowledgment assertion failure. Its complete raw job log and
unchanged assertion are recorded under **#696**. A later passing run does not
settle that intermittent failure; this increment does not claim a root cause or fix.

Authenticated Agent endpoints/client recovery, runtime/mode guards, complete
qualified history, standing authority, service lifecycle wiring, protected policy
Options/persistence and the full consumer/provider/resource/external-agent matrix
remain unfinished. Raw model text stays untrusted, with no task-success assessment
or model-quality acceptance gate.
