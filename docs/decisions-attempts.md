# Finalized Decisions native attempts (contract 1, issue #371)

Finalized routing uses a native Session attempt ledger. Provider inspection is
still observational: the existing call start/finish and GET APIs, payload
expiration and availability have no power to admit inference. The native attempt
ledger does not grant Agent permission, change task success or configure a model.
Live-window classification retains its separate existing contract.

## Retry policy and upgrade

`decisions_max_attempts` is trusted Session startup configuration, an integer
from 1 to 16, default **2**, including the first attempt. This preserves the
existing default retry allowance. Session freezes the configured value at the
first native reservation for each exact finalized source event. An explicit
legacy `DecisionsWorker(max_attempts=N)` limit can only lower this first
reservation's allowance; Session freezes the minimum of that proposal and its
startup policy. Default workers use the native setting. A later worker limit
can stop that worker earlier but cannot change the frozen native policy. Every accepted
reservation remains charged, including lost acknowledgements, cancellations,
timeouts and unknown provider outcomes. New UUIDs, different selected models,
worker replacement and Session restart cannot enlarge that source's allowance.
There is no new global quota, fallback model or scheduler.

A completed local provider error may retry within that original allowance after
its original operation and response cleanup return. An error receipt is not a
claim that the remote provider did no processing or incurred no charge. Pending
admissions, cancelled owners and unconfirmed cleanup cannot transfer their
original provider opportunity to another worker. They produce explicit
`call_outcome_uncertain` refusal, preserving the original reservation and late
completion history. They do not silently become Ignore.

Upgrade requires **stopping the old Decisions workers before starting the new
Session/Decisions versions**. At first ledger initialization Session atomically
records the existing transcript high-water sequence in
`decisions_attempt_cutover`. Unadmitted sources at or below that boundary are
`legacy_uncertain`, even when inspection is empty. Existing routing/refusal and
dispatch receipts are reconciled first as before. Other legacy sources get an
explicit `legacy_call_history_uncertain` refusal without re-inference. Inspection
records and their original uncertain charge evidence remain unchanged; no
numeric native allowance is reconstructed from diagnostics. New input creates
new sources above the boundary. Restart does not move the cutover boundary.
Mixed-version Decisions writers across this cutover are unsupported. This change
does not perform an upgrade or alter any running installation.

## Native service API

Only the authenticated **Decisions** service may use these JSON POSTs. Owner,
Converse and Agent credentials receive 403. Contract models and additive schemas
are in `packages/contracts/decisions_attempts.py` and `packages/contracts/schemas`.
No legacy schema changes. Inputs reject unknown/duplicate JSON fields and bad
counters. Admissions are at most 128 KiB, include at most 128 disclosure-source
bindings; completed raw response/output bounds remain 1 MiB/64 KiB. Client reads
are bounded by `MAX_ATTEMPT_BYTES`, with a two-second exchange facade deadline.
The original response acquisition/read/sole close remain enrolled after timeout.

- `/v1/decisions/attempts/lookup`: `DecisionsAttemptLookup` ->
  `DecisionsAttemptState`. Exact native source ID, sequence, revision, session,
  thread and epoch are required. `known_absent`, `admitted`, `completed`,
  `refused`, `retryable_error`, `exhausted`, `uncertain` and `legacy_uncertain`
  are distinct. Transport/capability/parse failure is **unavailable**, never
  absence; the worker represents the unknown attempt count as null.
- `/v1/decisions/attempts/admit`: `DecisionsAttemptAdmit` -> `DecisionsAttempt`.
  Immutable identity contains the original task owner/attempt IDs, expected
  aggregate count, optional lowering-only `max_attempts` proposal,
  provider/model/pin/protocol, request-body and transport-binding
  SHA-256, and all prompt disclosure sources. A SQLite `BEGIN IMMEDIATE`
  transaction rechecks native provenance, current source revision, session/epoch
  and the complete sticky disclosure floors, then exclusively reserves the next
  charged attempt. Only known absence or a settled retryable error can admit.
  Identical replay returns the same historical receipt; changed bindings,
  exhausted allowance, unresolved ownership or stale count return 409.
- `/v1/decisions/attempts/finish`: `DecisionsAttemptFinish` -> `DecisionsAttempt`.
  Only the exact original owner, source, attempt and admission digest may publish
  `completed`, `refused`, `error` or `uncertain`. The original immutable completion
  can be reconciled after a lost ACK. Conflicting completion is 409. Completion
  never refunds a reservation, reopens a provider opportunity or resurrects a
  deleted source. Exact late original completions remain historical evidence.

The stored provider request digest is over its exact canonical JSON body. The
transport binding hashes original provider/client identity, endpoint, headers
and selected transport identities; credentials/headers are not stored in the
ledger. Its private owner retains the actual original provider, client, task,
body and completion publication when acknowledgement is uncertain or physical
cleanup is unconfirmed. A committed uncertainty receipt does not establish
physical retirement and cannot release that original owner during lookup. The shared
HTTP transport boundary validates the frozen actual request after awaited hooks
and rechecks the original synchronous model-current callback before consuming
the provider opportunity once. A replaced body, stream,
client, transport or task cannot enter the provider using that reservation.
Native admission is the disclosure linearization point: later revocation cannot
recall an admitted in-flight request. Model lease/transport fences remain checked
before entry. Preparation and source-read failures before reservation consume
no attempt; a committed reservation is conservatively retained even if a later
transport fence prevents entry. Charged reservations are not a provider invoice
or a count of observed remote executions.

## Recovery, publication and fairness

Completed output is recovered only from the native ledger, strictly parsed and
validated against the original raw output when available. Recovery rechecks the
original source, selected provider/model and every original disclosed context
and task source. Invalid/stale/restricted results are refused without another
inference. Agent target revision, source permission and dispatch idempotency
checks continue to apply after recovery.

Session serializes finalized result/refusal publication per source. The first
native outcome wins; append returns that exact stored event. This stops a late
original result from dispatching after a replacement published uncertainty. The
late native completion and its charge are still retained.
When a refusal append instead returns an original winning decision, that exact
decision goes through the same guarded dispatch/receipt reconciliation before
the consumer can acknowledge the source. Existing executor
idempotency and durable dispatch receipts prevent replayed task effects.

Two consecutive unavailable native lookups/admission/completion publications
produce a durable `call_history_unavailable` refusal. Source-specific preparation
HTTP/storage failures receive at most three ordered processing attempts. A
successful operation clears that source's failure counter. These bounds count
metadata failures, not provider attempts. The winning outcome and any required
dispatch receipt must commit before cursor advancement; if the journal cannot
commit or acknowledge anything, progress
cannot be claimed. With journal publication available, unrelated later sources
proceed in the same batch. Restart may restart a metadata failure counter but
never the native charged allowance or an unresolved provider opportunity.

## Verification map

`tests/integration/test_decisions_native_attempts.py` includes real ephemeral
loopback Session HTTP/SQLite with the actual JournalClient and controlled actual
Compatible provider transport: worker/Session replacement, model/config change,
capture outage, classification-before-routing crash, lost ACK before/after
commit, late original response and sole close, native CAS/owner completion,
service authentication, legacy cutover, source-specific storage failures,
request-hook substitution and pending provider cleanup. LocalJournal and HTTP
CAS/completion cases exercise the same native owner.

`test_decisions_finalized_refusal_recovery.py` retains HTTP GET inspection
role/scope coverage and adds native recovery validation/fairness. Existing
`test_decisions_finalized_refusal.py`, `test_decisions_inspection.py` and
`test_decisions_task_descriptions.py` cover malformed-output refusal, retry,
source revision/disclosure/model fences, native d1 and Laya behavior. These are
synthetic correctness checks. No paid inference, model-quality measurement,
physical-device qualification or live configuration change is claimed. Issue
closure still requires independent review and exact integrated-head CI.
