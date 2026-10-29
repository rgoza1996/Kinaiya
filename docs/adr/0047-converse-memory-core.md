# Optional Converse memory core

Status: implementation contract for #113. Clarification section 12 governs
ownership. This is an owner-managed local Converse memory backend, not automatic
recall, an Agent memory tool, a routing engine or a measured context improvement.
Converse prompt integration, its UI toggle and replacement selection are later
work. Existing dialogue and the authoritative transcript do not depend on it.

## Authority and interface

The backend is installed only with the launcher's explicit `serve session
--converse-memory` (or `serve all --converse-memory`) switch. Without that opt-in,
the module is not imported, its schema is not created and its routes are absent.
This installation switch is separate from each namespace's owner use setting.
If the selected backend reports unavailable storage or an unsupported memory
schema, ordinary Session startup and transcript operations remain usable; memory
routes return a fixed unavailable response. Authenticate before receiving a body.
The backend uses the Session Store connection, lock and transactions. Its
separately versioned additive schema leaves journal version 1 and roles intact.
Each namespace is the current Converse `(session_id, thread_id, epoch)`. Reset
starts a new disabled namespace without copying memory; session deletion cascades
all memory. No cross-session, historical-epoch or global lookup is exposed.

All six routes below require owner authentication and permitted browser origin.
Other service credentials and scoped companion grants cannot access them. All
initial content is `local_only`, with provenance `owner_curated`. An observation
is an owner-reported observation, never native verification or execution evidence.
No model, external provider, extraction worker or automatic transcript scan runs.

The versioned memory models form an explicit backend interface. Namespace state
is disabled by default, revision 0, without creating rows on a state read.
Capture and hard retrieval require enabled state. Configuration, owner inspection
and forgetting remain independent administrative controls when disabled. Disabled
inspection reports the last stored state; it does not reconcile or process memory.
There is no background work. Disabling never deletes retained data or transcript.
Later Converse integration must use an optional replaceable interface, fall back
to ordinary bounded context when off/unavailable, and fence in-flight results
across disable/replacement. This backend alone does not implement that integration.

## Immutable nodes and provenance

Node kinds are `assertion`, `observation` and `inference`. Every node has a body,
a current authenticated finalized `conversation.user` source event and its exact
positive revision. Source identity, effective source sequence, node ID, ordinal,
scope, provenance and state are server-derived. Assistant text, tool results,
foreign/stale events and window drafts cannot masquerade as owner sources.

Assertions and observations require a structured fact: an exact ASCII key of
1..128 characters matching `[A-Za-z0-9_.:/-]+`, and a strict string, safe integer,
boolean or null value. Strings use at most 512 UTF-8 bytes; floats, containers,
coercions, nonfinite numbers and malformed Unicode are rejected. Canonical JSON
retains type, so boolean true differs from integer 1. Inferences have no fact and
require at least one prerequisite. Bodies use 1..2048 UTF-8 bytes, not whitespace
alone; ordinary newline/tab are allowed, other control characters are rejected.

Each new node declares at most 8 distinct existing eligible prerequisite IDs in
the same namespace. An immutable edge points prerequisite -> dependent; every
parent is strictly older in server admission order. Maximum depth is 16. Adding
edges to old nodes is unsupported. Refutation links are separate append-only
records, not DAG edges. Observation records remain historical records when tainted;
they cannot establish current truth merely by being retained.

Source freshness uses the original journal sequence, or the latest attributable
revision entry's sequence. A targeted expression index makes source-revision
lookups bounded. Validate the original and a contiguous attributable chain of at
most 32 corrections (fetch at most 33 to detect overflow), including strict
non-boolean revision integers and same-namespace identity. Store.resolve alone is
not chain validation. Invalid/overlong chains cannot admit or supply memory.
A pin includes source ID and revision; corrections never silently
rewrite memory bodies. Before enabled capture/query/inspection, reconcile all
bounded retained pins. A changed/malformed/missing source invalidates its claims
and their full dependent closure. Observations in that closure are tainted rather
than retracted. Unavailable parents cannot admit an eligible child.

## Contradictions and deterministic retrieval

Observation comparison is exact key plus typed value. For each key, a new
observation must have a strictly newer effective source sequence than all retained
observations for that key. An observation invalidates differently valued active
assertions with source sequence no newer than itself, plus their full dependent
closure. Traversal continues through observation vertices; their records remain
inspectable but tainted and unavailable to hard retrieval. Disagreeing observations
remain historical reports, without hypotheses or truth-confidence numbers.
An assertion already contradicted by an observation with the same/newer source
sequence is rejected. Matching later observations never revive old nodes.
Correcting an observation never restores an older observation as the comparison
head; retained observation source order still fences later admissions. Forget
advances a single namespace-wide capture floor to the greatest effective source
sequence in the erased closure. Every new capture, including assertions, must
use a source newer than this floor. This conservatively requires a fresh source
even for an unrelated key, while retaining no forgotten key, value or key hash.

One transaction commits capture, complete closure, refutation records, state and
namespace revision together. No partial invalidation or partial hard context is
returned. Namespace limits are 256 retained nodes, 2048 edges and depth 16.
Traversal loads only this bounded namespace, validates its structure and refuses
corrupt/over-budget graphs with a persisted quarantined state. Disable and forget
remain available in quarantine; forgetting must erase a conservatively complete
closure (all namespace content if structure cannot be trusted). Never serve stale
current claims after discovering a reconciliation failure.
Each indexed incoming/outgoing edge read has its own 2049-row detection bound
before a bounded in-memory union. An outer SQL UNION/DISTINCT limit is insufficient:
it may materialize an unbounded corrupt fanout before limiting returned rows.

Queries select either 1..8 seed IDs or 1..8 exact fact keys, not both. Key selection
uses eligible nodes. Return the complete eligible ancestor closure, sorted by
ordinal then ID. More than 64 nodes or 16 KiB serialized response is refused with
no partial bodies. Refutations, unrelated history, soft edges and confidence do
not participate. A missing, unavailable or forgotten explicit seed is refused.
The response pins the namespace revision for later integration fences.

## Retention, ordering and bounded wire behavior

Configuration and mutations use namespace revision CAS. Capture/forget also use
printable ASCII idempotency keys (1..128 characters) and canonical request digests.
Exact replay is accepted only while its saved namespace revision is current;
superseded replay returns conflict. Never store raw request/body snapshots in
receipts. There is at most one capture receipt per retained node and at most one
content-erasing forget receipt per node, hence at most 512 receipts per namespace.
An already-forgotten target is a no-op with no new receipt or revision (after
checking conflicting reused keys); this cannot consume the deletion budget.
Never refuse deletion merely because capture/receipt capacity is exhausted.
Configuration/disable remains possible. Reconciliation commits before reporting
stale CAS, so the caller refreshes rather than unknowingly acting on old state.
Explicit enabling also reconciles retained pins before CAS; a correction while
off can therefore require a refresh and a second enable request.

Forget erases body, key and value from the selected node and every dependent,
including observations. Retain only IDs, source pins, type, ordinal, edges and
minimal state/receipt digests. Block future admissions from those source event IDs
in this epoch, so retries cannot recreate forgotten content. A fresh, explicitly
admitted owner source is required for later memory. No deletion promise extends
to the original transcript, files, backups or any prior disclosure. Session
deletion performs the existing cascade. No automatic capacity eviction occurs.
If a corrupt namespace exceeds the ordinary response's 256-ID allowance, forget
erases all namespace content atomically and then returns a fixed conflict stating
that content was erased and the graph remains quarantined. It never returns a
truncated ID list as a complete result. State reports the actual metadata count
as a nonnegative safe integer, including an over-quota diagnostic count; the
normal admission quota remains 256.
An inconsistent retained forget receipt must not roll back content erasure.
If its same-node uniqueness conflict prevents recording the new receipt, commit
redaction and quarantine together, then report the same fixed erased-content
conflict without inventing a success receipt. Other storage write failures still
roll back the transaction and cannot claim erasure.

Wire schema is 1.0; extra fields and numeric/string coercion are refused. IDs use
the existing bounded ASCII identifier convention. Revisions/ordinals use safe
integers, never booleans. Expected-revision inputs accept the full safe range so
final-revision no-ops can be reconciled. Reserve the final increment for disabling so
exhaustion cannot leave use enabled. Other content-changing operations, including
forget, refuse numeric exhaustion instead of wrapping or mutating at an unchanged
revision; the existing owner session deletion remains available to erase all
retained memory in that exceptional state. Requests are JSON, at most 16 KiB before
parsing; reject duplicate object keys, malformed UTF-8 and nonfinite numbers.
Responses are no-store and at most 256 KiB. Validation errors never echo bodies.
Unknown/duplicate query parameters are rejected; none of these routes need query
parameters. Authentication, parsing and graph errors use fixed public messages.

## Owner API and model names

All routes are POST under `/v1/converse-memory`:

- `/state`: `MemoryBinding` -> `MemoryState`.
- `/configure`: `MemoryConfigure` -> `MemoryState`.
- `/capture`: `MemoryCapture` -> `MemoryCaptureResult` (`state`, `node`).
- `/inspect`: `MemoryInspect` -> `MemoryCaptureResult` (last state when disabled).
- `/query`: `MemoryQuery` -> `MemoryQueryResult` (`state`, `nodes`).
- `/forget`: `MemoryForget` -> `MemoryForgetResult` (`state`, `forgotten_ids`).

`MemoryBinding`: session_id, thread_id, epoch. `MemoryState` adds enabled,
revision, node_count and status (`ready`/`quarantined`). `MemoryConfigure` adds
expected_revision and enabled. `MemoryFact` has key and required value.
`MemoryCapture` adds expected_revision, idempotency_key, kind, source_event_id,
source_revision, body, fact (required for assertions/observations, null for
inferences), and prerequisite_ids (default empty). `MemoryInspect` adds node_id.
`MemoryQuery` adds seed_ids/fact_keys (default empty, exactly one nonempty).
`MemoryForget` adds expected_revision, idempotency_key and node_id.

`MemoryNode` adds node_id, ordinal, kind, source_event_id, source_revision,
source_sequence, prerequisite_ids, body, fact, state, content_scope and provenance
to the binding. State is active, invalidated, tainted or forgotten. Only forgotten
nodes expose null body/fact. Claims remain untrusted conversational context.

## Verification boundary

Use synthetic temporary databases and actual owner ASGI routes: persist/reopen,
transitive invalidation including paths through observations, monotonic source
ordering, correction fences, no resurrection, exact typed comparisons, bounded
complete queries, CAS/replay conflicts, off-state behavior, forget/redaction,
quarantine, reset/delete, concurrency/rollback, original journal compatibility and
service/companion authentication failures. No paid inference or hardware latency
test is necessary for this core. Automatic prompt recall and user-perceived
continuity remain separate, unverified work.
