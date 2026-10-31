# Agent completion confirmation (#167)

The native supervisor verifies evidence before preparing success. A successful
model `finish` proposal is not proof that either work or its completion record
has been durably confirmed. This protocol applies to file/tool tasks and native
UI publication tasks; publication remains evidence only of the published view.

## Final completion boundary

After the existing source, epoch, objective-revision and evidence checks, a
previously accepted steer supersedes the finish and returns to the ordinary
bounded task loop. An already admitted cancellation wins before finalization.
Otherwise the supervisor synchronously reserves one immutable append request,
including its task/session/thread/epoch, objective revision, evidence snapshot,
native idempotency key and successful Task snapshot. The live task remains
`verifying`, with no successful summary, while that write is in flight.

At this boundary no further work remains to cancel or steer. New steer and
interrupt requests return HTTP 409 with an explicit completion-confirmation
conflict immediately, including while confirmation remains uncertain. They do
not wait for the journal or claim that cancellation took effect. Admissions
which began before this boundary recheck the fence after source/session reads
and before changing the record, including when success has already been
confirmed. An identical replay of previously accepted steering returns its
existing task status without another mutation; conflicting reuse remains an
idempotency conflict. Status remains independently readable.

## Positive confirmation and uncertainty

The supervisor sends the reserved append exactly once. A matching acknowledged
entry confirms success only when its authenticated origin is `agent-supervisor`,
its sequence follows submission, and every append field matches the frozen
request. This includes the entire payload, task reference, scope and key.

If append fails or returns a mismatched receipt, the supervisor reads back only
this task's outcomes within the same session/thread, in pages of at most 200.
Read-back is limited to eight pages and five seconds total. Only the exact
native match confirms success. Empty or malformed pages, non-advancing
sequences, exhausted bounds, timeouts and read failures leave it unconfirmed.
An empty read is not proof that an earlier request cannot still commit.

On uncertainty the local task is `paused`, its summary is empty, its sanitized
error explicitly says completion journaling is unconfirmed, and local waiters
are released. The supervisor does not append a competing failed/cancelled
outcome, retry the append, repeat tools or ask the model to regenerate work.
Shutdown or worker cancellation during this interval preserves the same
uncertainty. Raw transport exception details never become this status text.

The local finalization fence remains until restart recovery settles journal
state. Recovery restores a committed terminal snapshot when available;
otherwise work remains paused. Recovery does not invoke the model or replay
effects. This bounded fix does not provide a distributed transaction or
exactly-once execution for arbitrary external tools, nor an online owner
reconciliation editor. Broader recovery, retained-resource handling and all
production release gates remain separate qualifications.

## Traceability and verification

This implements the truthful completion/recovery obligations in
[Roadmap §§5.4, 20.3 and AT06/AT10](../Roadmap.md),
[clarification §11](../clarification.md#11-cross-surface-task-coordination-steered-by-converse-and-voice-traceability)
and [AGENTS.md](../AGENTS.md).

- `services/agent/completion.py`: exact native receipt validation and bounded
  read-back, with deterministic malformed-receipt/cancellation/bounds tests.
- `services/agent/runtime.py`: detached successful snapshot, finalization
  arbitration and conservative local pause.
- `tests/integration/test_agent_completion_recovery.py`: actual SQLite journal,
  before/after-commit failures, held append visibility, delayed commit,
  shutdown, restart without inference and accepted-before-fence steering.
- `tests/integration/test_agent_completion_races.py`: admissions crossing the
  fence and shutdown during read-back.
- Existing Agent, disclosure/source-authority, retirement, artifact and
  cross-service integration suites remain required regression checks.

All fault injection uses public synthetic inputs and scripted inference.
The [verification record](../evaluation/evidence/agent-completion-2026-10-31.json)
retains focused results, the failing prior baseline and rejected Muse candidate
provenance. Full execution results belong in PR/CI evidence; this contract is
not a test result.
