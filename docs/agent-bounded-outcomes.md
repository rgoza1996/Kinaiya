# Bounded durable Agent outcomes

Issue [#487](https://github.com/rgoza1996/Kinaiya/issues/487) records a failure in
which a native Task appeared failed in memory while its duplicated evidence
made the final journal request exceed Session's unchanged 64 KiB entry limit.

## Format and authority

Small outcomes retain the existing inline representation. Before any terminal
POST, Agent measures the entire encoded `AppendRequest`. An oversized outcome
replaces both evidence arrays with empty arrays and adds one closed
`evidence_ref`: `agent.evidence-prefix.v1` for ordinary submission, or
`agent.evidence-prefix.v2` for standing admission. The reference binds task,
session, thread, epoch, objective revision, native submit sequence, final tool
sequence, count, canonical evidence bytes and two SHA-256 commitments. The first
commits the ordered original Evidence objects; the second additionally commits
their native event IDs and sequence numbers. Mixed objective revisions remain
in their original order. This is storage provenance, never new success proof.

Only exact authenticated acknowledgements to Agent's original tool request
populate the running index. A sparse outcome is permitted only when that index
matches the complete in-memory evidence list and its disclosure floor. Missing,
changed or unconfirmed receipts cannot be silently dropped. Metadata that still
exceeds 64 KiB is refused without cropping. Original journal tool rows remain
unchanged; outputs are not copied into the sparse outcome.

Recovery validates the commitment before installing the terminal Task. It also
retains actual durable receipts when work is recovered paused. Converse performs
the same authenticated reconstruction before evaluating its existing completion
announcement rules. It neither accepts empty sparse proof nor uses only a
prefix that happened to be available. Failed hydration propagates as unavailable,
so Decisions does not acknowledge the outcome as handled. Legacy inline outcomes
remain readable.

Legacy inline snapshots, including ordinary paused snapshots, keep their exact
evidence even when old tool rows cannot be indexed by this contract. A sparse
outcome still requires every original receipt. Small standing outcomes retain
their original inline shape.

Standing v2 adds exactly one closed `admission` object: `kind`, `origin`,
`event_id`, and `sha256`. Its family is fixed to `agent.standing-command` from
`standing-supervisor`. The hash commits the complete original native
`JournalEntry`, encoded as UTF-8 sorted compact JSON with ASCII escapes and
nonfinite numbers refused. This includes its native sequence/time/identity and
all claim, authorization, model, workspace and tool-subset metadata. Native
authenticated transport establishes provenance; a pure packet/hash grants no
permission. Readers fetch that exact initial family and validate the entire
commitment, without relabeling it as ordinary submission. Unknown versions,
extra reference/admission fields and family downgrades are unavailable.

The standing worker builds its index from the full `StandingInitialProof`
before its final freshness checks. Hash/proof-validation CPU consumes the
original launch deadline. The existing one-use direct-created launch latch,
model/resource capture, file-only tool subset and actual cleanup ownership
remain authoritative. A retained admission cannot launch a worker.

Restart installs confirmed terminal standing snapshots for historical status
only. They have no ordinary native submit identity, idempotency reservation,
standing launch owner, model lease or worker. Incomplete standing admission is
left to the native admission lookup, rather than installed as a resumable
ordinary paused task. Recovery does not create an outcome from a missing write
or turn an ordinary retirement record into standing completion. Converse uses
the same full hydration; the existing Agent-channel rule still prevents it
from speaking a standing completion in that channel.

Reconstruction permits at most 2,000 receipts, 128 MiB of canonical Evidence,
256 pages of at most 200 tool rows and a ten-second announcement read deadline.
The journal's existing authenticated, bounded read transport is retained.
Malformed pages, omitted receipts, changed event provenance, scope widening,
competing inline evidence and incorrect commitments fail closed.
Late bytes from a cancellation-resistant adapter cannot pass the read deadline
or owner cancellation fence. Only the early, read-only hydration timeout is
converted to typed evidence-unavailable. The outcome worker retains its cursor
and waits one cancellation-aware second before trying those reads again. A later
announcement/publication timeout is not retried by this path.

## Terminal write settlement

Success, failure and cancellation use the same frozen, one-shot append and exact
native acknowledgement/read-back confirmation. The visible state remains
`verifying` during confirmation. If serialization, POST or read-back cannot be
confirmed, local state becomes `paused` with an unconfirmed-journaling error,
empty summary and a retained completion fence. The original evidence remains in
memory, together with the exact attempted terminal snapshot in native custody.
That snapshot grants no retry or success authority. No subsequent inference,
effect, competing outcome or revision is
allowed. Restart reads durable truth without replaying any work.

This change does not fabricate an outcome for a historical process whose RAM
failure was never durably recorded. Such a task remains recovered paused;
retained owner observations and any administrative reconciliation must describe
that uncertainty explicitly. The repair does not alter tool execution, retention,
provider recovery, completion requirements or Session's entry limit.

## Ordinary outcome verification

Qualified on November 4, 2026 using the existing cached Python environment:
**479 passed, zero failures or skips, in 192.60 seconds**. This includes 56 cases
in `test_agent_outcome_evidence.py` and `test_agent_bounded_outcomes.py`, plus
423 existing completion, recovery, native submission, UI, disclosure, retirement,
standing execution and publication-recovery cases across 22 modules. Ruff format,
Ruff check and `git diff --check` passed. All 778 repository Python input hashes
were identical before and after the final combined run.

Public synthetic unit cases cover bindings, shape and size limits,
acknowledgements, scope, pagination, missing/reordered/tampered receipts and
deadlines. SQLite integration cases exceed the original limit using actual
native file-search receipts, cover all three terminal states with confirmed,
lost and absent acknowledgements, and check exact recovery and Converse
announcement/refusal without model calls or re-execution. Existing reset cases
now require paused/unconfirmed state, the retained failed snapshot and no stale
outcome; their no-effect, call-count, close and non-reset assertions remain.

First receipts were retained: Ruff initially reported one import-layout error;
the first new-test run passed 55 and failed one expectation of an internal error
that the public recovery API intentionally sanitizes. After its test-only
correction, all 56 passed. The first broader run passed 418 and failed the five
old reset-state expectations; only those branches were updated to the new
confirmed-outcome contract before the final 479-case run. No live service,
historical task, provider request, private media or device was changed by this
qualification.

## Standing outcome verification (#633)

The [standing evidence](evidence/standing-bounded-outcomes-2026-11-15/README.md)
records the frozen failing baseline, native admission/receipt qualification,
full source pins, test commands and retained limitations. Model inference is
synthetic and measured; native SQLite, authenticated Session/JournalClient,
one-use standing admission, file tools, captured model ownership and cleanup
are real. No model answer or style is an acceptance gate. The original #487
task's undurable historical failure remains uncertain and is not rewritten.
