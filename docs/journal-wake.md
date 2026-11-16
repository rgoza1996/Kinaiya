# Journal change wake hints (#623)

Status: native metadata increment under qualification. Adaptive Live remains off.
Existing workers and journal WebSocket consumers retain their compatibility paths.
No inference, transcript publication, capture or playback is started by this API.

## Contract and ownership

GET `/v1/transcript/wake` returns a closed `journal_wake_v1` cursor containing
schema 1.0, the original Store owner's random instance ID and a bounded monotonic
revision. POST `/v1/transcript/wake/wait` accepts that exact cursor and an explicit
wait_ms from 0 through 1,000. Its closed observation echoes the original request,
returns the current cursor and says whether its revision advanced. Responses are
uncached. Only owner, Converse and Decisions credentials may inspect/wait; the
HTTP wait rechecks current credentials/origin before returning. Agent and Resources
credentials cannot use these endpoints. LocalJournal enforces the same roles.

The body is bounded to 4,096 bytes and five seconds for acquisition. Unknown or
coerced fields/versions, duplicate JSON keys and nonfinite numbers are refused.
The result carries no transcript, token, model output, permission, success,
publication receipt, native-worker release or capture-state claim. JournalClient
uses the existing retained bounded raw exchange and sole response-close owner;
caller cancellation cannot erase its original HTTP operation.

The cursor is a process-local recheck hint. It does not prove a commit, qualify
any particular source or settle an old effect. Consumers must reread their durable
sources and recheck session/epoch/revision, current selection, disclosure,
availability and authority before inference or publication. A typical consumer
captures the cursor, reconciles its durable sources, then waits on that cursor;
a transaction between those steps makes the wait return immediately. The maximum
idle wait is a heartbeat, not an artificial delay before admitting fresh input.

## Native notification boundary

A small SQLite connection subclass preserves the original operation's return or
exception. It observes total_changes only when SQLite has exited its transaction,
including context-manager exit, explicit commit/rollback and script/SQL commit.
An open uncommitted transaction cannot wake readers early. A script may commit a
prefix and then fail; finally observation must expose that changed state without
waiting for a later SELECT. Reads and empty transactions do not generate wakes.
A rollback may emit a harmless recheck hint; this is why a wake is never a receipt.
No schema migration, inference admission or alternate database writer is added.

One lock atomically compares a cursor and enrolls its event. Committing threads
schedule that same event on its original loop. There are at most 64 retained waits.
A failed callback cannot free a slot: the actual wait's finally path alone removes
it. Cancellation, timeout and normal completion retire only their own wait. Store
close wakes callers into explicit unavailability. A different Store instance or
future cursor is refused; restart cannot impersonate the previous owner. Revision
exhaustion disables wake inspection instead of wrapping or changing write results.

## Verification and limits

Controls use real SQLite, cross-thread commits, ASGI authentication and the actual
JournalClient transport. They cover subscription ordering, commit/rollback,
partially committed script errors, idle/self-wake exclusion, reset/deletion,
64 retained waiters, failed callback ownership, actual cancellation, close and
replacement, credential rotation, byte/type/role bounds and frozen schema exports.
See the [source-pinned evidence](evidence/journal-wake-2026-11-16/README.md).

Notifications are for mutations through this original connection. External writers
or another Store instance are not a supported shared-writer protocol. The sole
Session owner and its independent service contracts remain authoritative. A new
completion-driven Decisions consumer, source partition, backchannel ownership and
full Adaptive Live rollout remain separate #623 work. Model quality is not an
acceptance gate for this metadata and lifecycle increment.
