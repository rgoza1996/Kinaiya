# Deterministic HTTP cancellation at original enrollment (#687)

The cancellation control now calls the real synchronous negative-original
enrollment, then cancels its still-running HTTP Task in that same call stack,
before the first facade wait. Both abort and retirement protocols retain the
`CancelledError` requirement. Actual native fence entry is observed afterward;
the same held Task must remain owned, settle after release, and confirm exact
cleanup without replay or STT. Production code and deadlines do not change.

Baseline: runtime `a3be77b5f0cd0ca970da71b85d9cb2ba9351a4c3` on
`codex/623-adaptive-call-integration`. The [manifest](manifest.json) records exact
commands, source and lossless log digests. [Issue #687](https://github.com/rgoza1996/Kinaiya/issues/687)
was recorded before correction.

| Check | Result | Receipt |
| --- | --- | --- |
| Complete modified native abort/retirement module | 38 passed on Python 3.13.18 | [Command and pins](logs/issue-687-cancel-enrollment-313-receipt.json) |
| Same scope | 38 passed on Python 3.14.10 | [Command and pins](logs/issue-687-cancel-enrollment-314-receipt.json) |
| Ruff | Passed | [Command and pins](logs/issue-687-ruff-final-receipt.json) |
| Format | 1,227 files already formatted | [Command and pins](logs/issue-687-format-final-receipt.json) |
| Real bounded-facade ordering probe, before correction | One passed | [Command and pins](logs/issue-687-facade-deadline-probe-receipt.json) |

The predecessor [CI run](https://github.com/rgoza1996/Kinaiya/actions/runs/44091623901)
on `17a2350b` failed its Python 3.11 cancellation control: `CancelledError` was not
raised. The complete [original job log](logs/pr-683-predecessor-job-134067933659.log.json)
reports one failed, 16,091 passed, 26 skipped, 49 deselected and two passing
subtests. Its download receipt describes successful retrieval, not passing CI.
The exact committed first control source remains preserved.

The old control waited for native fence entry before calling cancel, without
establishing that the bounded HTTP facade remained unfinished. The separate
native probe observes a legitimate 200/in-progress response while the original
fence remains held. Cancelling that completed HTTP Task returns false; releasing
the same native original later confirms cleanup with no replay. This proves the
ordering permits the race. The original CI trace does not establish its precise
scheduler timing, and no such timing is invented here.

The corrected case forces the intended cancellation at real enrollment. It
extends no deadline, adds no arbitrary sleep or retry, and does not accept a
noncancelled result. Existing deadline controls retain natural-completion
coverage. The whole modified module runs on both local interpreters to check
fixture restoration and surrounding native ownership behavior.

Only `test_live_stream_abort.py` changes after the
[557-case runtime qualification](../session-scope-retirement-2026-11-17/README.md).
[Byte comparison](runtime-equivalence.json) confirms production Python and all
340 frontend inputs remain unchanged. That runtime record is not relabeled as
a new all-source run; the 38-case scope overlaps it. Python 3.11 is not locally
qualified. Updated exact-head CI must qualify that interpreter before integration.

Five local run records, the full failed CI job, three source snapshots and the
original probe remain preserved. Each `.log.json` stores exact UTF-8 output and
its raw byte SHA-256; encoding `text` recovers the original bytes. Archive and
receipt digests are separate. No dependencies were installed, support versions
retired, models called, CI retried or installed owners promoted. Adaptive Live
remains disabled and #623 remains open.
