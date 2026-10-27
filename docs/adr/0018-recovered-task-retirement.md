# ADR 0018: native retirement of recovered work

Tracks [#47](https://github.com/rgoza1996/Kinaiya/issues/47), Roadmap AT32 and
the M6 recovery boundary. An interrupted task can survive in the native journal
after its session epoch resets. Recovery correctly creates no worker, but the
old cancellation path attempted an old-epoch write and left an in-memory
`cancel_requested` task that could not finish. Ordinary stale writes must keep
failing; changing the task's epoch would move old context into the new session.

## Administrative receipt

An authenticated owner may retire verified recovered paused work. The trusted
Agent supervisor requires no worker or residual process handle and completes
native pin cleanup before publishing retirement. Ordinary live cancellation
keeps its existing worker cancellation and reaping path. A model, tool result
or Decisions classification cannot authorize administrative retirement.

The new `TaskRetirement` 1.0 payload records only original task/session/thread/
epoch/revision identity, hashed owner request identity and payload digest,
`disposition=cancelled` and `effect_status=unreconciled`. The Agent appends it as
`agent.retirement` in the current epoch of that same session/thread. Journal
authentication supplies `agent-supervisor` provenance. No original objective,
evidence, source text or raw cancellation reason is copied into the new epoch.
It is an administrative receipt, not an `agent.outcome` or conversational
completion notice. The Task status retains its original scope and states that
previous uncertain effects remain unreconciled. Cancellation does not claim
that effects were undone.

The session service's stale-epoch and causal-reference checks are unchanged.
The administrative payload identifies the old task without pretending that a
cross-epoch causal edge is a current-epoch transcript reference. Only native
Agent writers may publish this event kind. Consumers must validate its exact
versioned payload, native origin, task reference, original task scope/revision,
event scope and ordering against the native submission. A receipt cannot
override a terminal execution outcome or retire a task submitted later.

## Failure and replay

Retirement is serialized per recovered task. In-memory terminal/idempotency
state follows a durable receipt, so a failed or cancelled append leaves the
task paused and available for explicit retry. Stable hashed request identity
does not depend on the native event counter. Before retrying an unacknowledged
operation, the supervisor reconciles authenticated retirement receipts. A
commit followed by a lost acknowledgment must settle without another event,
model call or tool action. Recovery restores the terminal state and request
binding; conflicting reuse of a known request key remains an error.

The compute-plugin service retains its release mapping until all durable unpins
acknowledge. A failed pin write therefore remains retryable; it cannot silently
lose the only native cleanup handle. Repeating an already committed unpin is
idempotent. Recovered work cannot accept steering into an absent worker; the
owner retires it and submits a new task instead.

MCP management keeps its strict exclusion of all nonterminal tasks. Retiring
older work cannot release that gate while current work remains active. If
native cleanup, session lookup, metadata validation or persistence cannot be
verified, retirement fails closed and management remains unavailable.

## Verification scope

Deterministic tests use the actual journal with synthetic providers and check
reset/recovery/retirement/reopen, original identity, absent new-epoch context,
authority, malformed receipts, request conflicts, write failure/lost
acknowledgment, concurrent stops, stale writes/causes and current-task exclusion.
The repair does not replay or reconcile uncertain external effects, migrate
tasks into a new session, or satisfy the full M6 production gate.
