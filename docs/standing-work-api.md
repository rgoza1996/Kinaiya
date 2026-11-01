# Standing work: owner authorization and dispatch boundaries

Requirement: [#228](https://github.com/rgoza1996/Kinaiya/issues/228), within
[M7](https://github.com/rgoza1996/Kinaiya/issues/10) and the
[Options delivery plan](options-delivery-plan.md). Read with
[Agent operating modes](agent-control-api.md), Roadmap §13/§19 and clarification.md.

This branch implements the authorization owner, native dispatch gate and their
authenticated Session APIs, immutable authorization/dispatch Dart proofs and bounded
transport, native claim reservations and atomic initial-task admission with
authenticated execution APIs, permanent admission closure and an internal adapter
to the actual Agent worker.
A saved or armed authorization is **not** a scheduled task,
producer-health claim, execution receipt or completed responsibility. Native
producer integration, positive execution settlement,
the inbox and embedded owner editor still require integration. Autonomous stays
unavailable in the current ordinary mode API. This document does not qualify M7
or the full issue.

## Roles and routes

Session owns the protected authorization database and current controller
generation. Authentication and the listed role are checked before body ingress:

| Method | Route | Role | Result |
| --- | --- | --- | --- |
| POST | `/v1/standing/changes` | owner | `StandingChangeReceipt` |
| POST | `/v1/standing/changes/lookup` | owner | `StandingChangeLookup` |
| GET | `/v1/standing/responsibilities/{responsibility_id}` | owner | `StandingResponsibilitySnapshot` |
| GET | `/v1/standing/responsibilities?after={id}&limit={n}` | owner | `StandingResponsibilityPage` |
| GET | `/v1/standing/dispatch` | owner | `StandingDispatchState` |
| GET | `/v1/standing/dispatch/stop-context` | owner | `StandingDispatchStop` template |
| POST | `/v1/standing/dispatch/activate` | owner | `StandingDispatchActivationReceipt` |
| POST | `/v1/standing/dispatch/activate/lookup` | owner | `StandingDispatchActivationLookup` |
| POST | `/v1/standing/dispatch/stop` | owner | `StandingDispatchStopProof` |
| POST | `/v1/standing/producer/renew` | agent | `StandingProducerReceipt` |
| POST | `/v1/standing/executions/prepare` | agent | `StandingExecutionClaimAdmission` |
| POST | `/v1/standing/executions/lookup` | agent | `StandingExecutionClaim` or null |
| GET | `/v1/standing/executions/unresolved` | agent | `StandingExecutionClaim` or null |
| POST | `/v1/standing/tasks/admit` | agent | `StandingTaskAdmission` |
| POST | `/v1/standing/tasks/lookup` | agent | `StandingInitialProof` or null |
| POST | `/v1/standing/tasks/export-admission` | agent | `StandingExportAdmission` |
| POST | `/v1/standing/tasks/close-admission` | agent | `StandingAdmissionClosure` |
| POST | `/v1/standing/tasks/closure-lookup` | agent | `StandingAdmissionClosure` or null |

Owner, Converse, Decisions and Resources credentials cannot renew a producer.
All six dispatch/producer routes reject query fields. Activation and its lookup
accept the same complete `StandingDispatchActivation`; Stop accepts the complete
`StandingDispatchStop`; renewal accepts `StandingProducerRenewal`. The context
GET only reads a template and does not perform Stop.

All fields in the strict request and proof schemas are required, including
explicit nulls. Unknown fields, duplicate JSON keys, floats, unsafe integers,
invalid Unicode and extra/duplicate query parameters are refused. IDs use the
contract's exact lowercase UUID spelling; no normalization creates a new identity.
`limit` is a decimal integer from 1 to 20, default 20. `after` is an exclusive
responsibility-ID cursor. Snapshot of a genuinely absent definition has revision
zero and a null record, with the actual backend and controller identity.

Responses, errors and CORS preflights under `/v1/standing` use
`Cache-Control: no-store`. Invalid input is 422, unsupported media/encoding is
415, a definite authority conflict is 409 and unavailable native proof/storage
is 503. Authentication retains the shared 401/403 behavior. An error or missing
response does not prove that a write failed.

## Immutable authorization

`StandingChange` binds the entire original request to an idempotency key,
expected backend, known current Session controller, expected responsibility
revision and responsibility UUID. The owner authorizes:

- A label of 1–200 Unicode characters and literal objective of 1–16,000.
- Content/disclosure scope, enable state and the exact native Agent session,
  thread and epoch. New saves check that this is an actual current Agent session.
- An exact `WorkspacePin`. Saving it does not establish that the current Agent
  filesystem still matches it; execution must check that separately.
- Daily civil time, IANA timezone, start date and exclusive UTC expiry, with an
  explicit finite run budget from 1 to 32.
- The configured Agent profile as the inference authorization, subject to its
  existing disclosure, permissions and spending/resource guards.
- In-app, status-only delivery, routine or important attention and optional
  quiet hours. No external destination, push, sound, vibration or call is granted.
- The fixed no-overlap policy. Execution must retain a responsibility's unresolved
  slot across later authorization revisions; editing cannot erase uncertain work.

No authorization changes filesystem policy, network destinations, shell access,
approval TTLs, provider-disclosure rules or protected evaluation criteria.
Disabled and expired definitions remain attributable historical records.

## Native civil time

Native `ZoneInfo` resolves schedule meaning. Dart preserves the timezone string;
it does not infer authoritative eligibility from the browser's timezone or clock.
Schedules span at most 32 nominal civil dates and expire within 30 UTC days of
the actual native save/arm clock. A repeated local time chooses its earlier UTC
instant; a nonexistent local time is skipped. Grace is 1–86,400 seconds. Only
the latest relevant due instant within grace is a candidate, with no pre-save
backlog. Selection alone provides no dispatch deduplication or launch authority.

Quiet hours defer attention, not durable recording. Their native boundaries
handle folds and gaps with a bounded 48-hour search. UTC instants use exact
second-resolution `YYYY-MM-DDTHH:MM:SSZ` grammar and actual Gregorian dates.

## Save, arm and reconcile

A new CAS save commits a complete immutable receipt and a pending definition.
Only after the first actual successful native COMMIT acknowledgement does the
same original call attempt a second transaction to arm it. Arming samples a
fresh native UTC clock, rounds up to the next whole second when necessary and
records `authorized_from_utc`. It rechecks current definition/controller,
expiry, cancellation and deadline.

Actual first COMMIT followed by lost acknowledgement can leave a durable
**pending** definition. Exact retries and lookup return historical proof; neither
arms it. A later owner decision may save a new CAS revision, but reconnect must
not silently mint that new key or reinterpret uncertainty as failure. A saved
receipt can coexist with pending activation after cancellation, displacement or
an unavailable second transaction.

`StandingRecord` binds the full saved receipt, `pending`/`armed` state and its
nullable authorized-from instant. Record activation is distinct from future
global dispatch activation and actual work admission.

Lookup echoes the complete original request and retained receipt, plus a minimal
current revision projection. It proves whether the request's controller was
actually issued and is now current or retired. Unknown UUIDs are conflicts, not
retirement proof. `mutation_fenced` describes an **unobserved** request's native
fence; a recorded lookup uses false and remains historical evidence only. No
lookup reapplies a definition, selects a provider or starts work. Earlier
receipts stay retrievable after disable, expiry, a later revision or controller
replacement. A different backend at the same URL cannot supply that proof.

Digests use compact, sorted UTF-8 JSON with no Unicode normalization:

- Authorization: SHA-256 of `kinaiya.standing.authorization.v1\0` followed by
  `{responsibility_id, revision, request}`.
- Record: SHA-256 of `kinaiya.standing.record.v1\0` followed by
  `{receipt, activation, authorized_from_utc}`.

The schemas describe shape; native validation also enforces digest, full-request,
date, ownership and state relationships. A hash alone is not an authority grant.

## Bounds and actual operation ownership

Definition requests accept at most 131,072 raw bytes and 114,688 canonical bytes. Receipt
and record caps are 131,072, snapshot 147,456, and lookup/page 262,144 bytes.
The request reserves 32,768 bytes beyond its two full copies for proof metadata.
JSON depth is at most 12 and total nodes at most 2,048. A page can therefore
contain fewer records than its requested limit; its cursor names the final
returned record. Lookup avoids a third full request copy in current state.

The native owner retains at most 10,000 definition changes, reserves room for
disabling enabled definitions and permits at most 32 currently enabled
responsibilities. Pending and expired enabled records still count. It validates
integral revision storage and contiguous selected history; SQLite's INTEGER
affinity alone does not establish either. Capacity or corruption fails closed
without pruning proof or resetting authority.

The HTTP adapter owns at most 64 actual operations, including ingress, native
threads and serialization. Body ingress has a five-second bound within a
25-second whole-operation deadline. Timeout/disconnect sets the native
cancellation signal but retains capacity until the actual thread settles.
Shutdown joins these operations before retiring the Session controller. It
does not label an uncooperative thread stopped because its caller stopped waiting.

Dispatch/producer POST requests have a separate 4,096-byte raw and canonical
bound. Renewal receipts are bounded at 8,192 bytes, gate state and activation
receipts at 16,384, lookup/Stop proofs at 32,768, and Stop templates at 4,096.
The old four routes keep their larger definition/proof limits. Both families
share the same 64-operation registry, five-second ingress and 25-second total
deadline. Neither family gains a second capacity pool.

The Dart `StandingOwnerApi` uses the shared 32-owner transport bound, a
10-second result deadline and one-second cleanup wait. An attempt separately
reports its public result, actual settlement and whether it was dispatched.
Closing a view/client or returning an error cannot free an unsettled transport
owner. Only the typed standing domain uses the larger byte caps; existing
model, runtime and mode domains retain 4,096/16,384-byte limits. Redirects and
compressed responses are not followed or accepted.

Client read validation also binds the response to its caller: a snapshot must
name the requested responsibility; each page item must follow the requested
exclusive cursor and the page cannot exceed the requested limit. An internally
valid proof for another request is refused. Change and lookup additionally bind
the complete original mutation request.

`StandingDispatchOwnerApi` exposes gate observation, passive Stop-template
discovery, activation, full-original lookup and target Stop. Its strict eight
packet types preserve all native digest, history, null-controller and readiness
relationships. It provides no owner method for Agent-only renewal and infers no
liveness from a browser clock. A separate transport domain uses 4,096/32,768-byte
request/response caps in the same global 32-actual-owner registry; all earlier
domains keep their bounds. There are no implicit retries or replacement keys.

The [focused client evidence](../evaluation/evidence/standing-dispatch-client-2026-11-01.json)
records independent frozen cases, existing transport regressions and actual
Dart HTTP/Session/SQLite checks. It does not qualify a scheduler, Agent standing
worker, inbox or editor.

## Execution proposal contract

`packages/contracts/standing_execution.py` adds six strict immutable packet types:
`StandingOccurrence`, `StandingExecutionModelPin`, `StandingExecutionTask`,
`StandingExecutionIntent`, `StandingExecutionClaim` and
`StandingExecutionClaimAdmission`. They describe a complete original execution
proposal and retained claim proof. They do not create a native reservation,
authorize a model/tool call or launch a task. Their raw and canonical caps are,
respectively, 4,096, 8,192, 65,536, 131,072, 147,456 and 163,840 bytes.

An occurrence binds the backend, responsibility, authorization revision and
digests, local date, resolved UTC instant and offset. Its identity excludes the
producer, task and dispatch attempt; the full intent digest also binds those
fields, the complete task/model/workspace pins and executor capability hash.
Claim identity and record hashes use separate domains. Every proof is checked
against the full original request. Native schedule eligibility and producer
liveness require joined native state; packet timestamps do not establish them.

Only a contextual hash of a transient 32-byte original-process capability is
carried. The secret has no DTO, journal, Task or prompt field. Native admission verifies that private capability. Future claim close and
settlement must verify it too; reading a
historical claim must never let another process adopt a launch or attest cleanup.

The pure `standing_initial_request` builder emits the explicit
`agent.standing-command` / `standing` family with no fabricated conversation
sources. It checks the real existing AppendRequest serialization cap of 65,536
bytes. The unchanged owner/Decisions parser refuses this new family. Native
admission joins the full retained claim and protected authorization before
assigning a `standing-supervisor` origin and appending that proposal.

`StandingInitialProof` and `StandingTaskAdmission` separately describe the later
claim-to-initial-entry binding. Their raw/canonical caps are 163,840 and 180,224
bytes. The proof binds every request field plus the existing native event ID,
sequence, UTC timestamp and `standing-supervisor` origin. Its digest uses
`kinaiya.standing.initial.v1\0`. Raw entries require all thirteen fields; typed
native entries must already contain their actual metadata. Validation never
generates replacement metadata. Reconstructing a detached JournalEntry or
parsing a `created` disposition does not establish native acceptance or launch.

The [contract evidence](../evaluation/evidence/standing-execution-contract-2026-11-01.json)
records the independent frozen checks, original missing-capability baseline,
source review and additive schema export. Native claim reservations and initial
task admission are qualified separately below. The working editor → occurrence →
Agent worker → settled inbox chain remains unfinished.

## Native claim reservations

`services/session/standing_execution.py` adds an explicitly registered
`StandingExecutionOwner` over the same protected Store, controller,
authorization owner and dispatch gate. Construction and close are passive.
`prepare` joins the complete current armed/enabled grant, exact Agent session,
task/workspace metadata, native due occurrence, Autonomous activation and live
producer before committing an immutable claim. It checks the known initial
request serialization limit before inserting, and repeats current authority and
absolute cancellation/deadline checks immediately before COMMIT.

Two protected tables retain canonical full proofs and unique claim/occurrence
identities. This phase permits one unresolved reservation globally, including
across all responsibility revisions. It has no task-admission, worker, release
or settlement operation. Neither editing a grant nor Stop, expiry, session
reset/deletion or process restart clears the reservation. The full original
owner record and captured prepared time qualify historical occurrence meaning;
saved metadata does not prove the live external filesystem/provider matches it.

Only a timely direct acknowledged new COMMIT returns `created`; exact replay
returns `retained`. A lost acknowledgement preserves durable uncertainty.
`lookup` and `unresolved` validate history without sampling UTC or producer
liveness, starting work or changing the reservation. Established history also
remains readable at controller-registration capacity using validated passive
owner joins; no current writer is invented. No claim API or actual producer is
exposed by this class alone.

Schema validation rejects unexpected foreign keys in both tables. The
[native claim evidence](../evaluation/evidence/standing-execution-owner-2026-11-01.json)
retains the initial 64-case pass, two subsequent failing corruption regressions,
the source-only fix and unchanged 66-case qualification. Those checks use actual
SQLite transactions, commit faults, session lifecycle, clocks and competing
callers; they do not qualify Agent execution or effect settlement.

## Atomic initial-task admission

`StandingTaskOwner` explicitly joins the same Store, controller, authorization,
dispatch and claim capabilities. A first admission requires the full prepared
original and its exact transient 32-byte executor capability. It rechecks the
current grant, session, mode/Stop fence, live producer and native civil-time
eligibility. A higher heartbeat for the same producer can preserve eligibility;
a different producer cannot adopt the prepared request.

One protected transaction appends the actual `agent.standing-command` initial
entry and its full proof link. Both writer and origin are `standing-supervisor`.
The new table has no foreign keys. Registration and reads validate canonical
proofs, both stored journal projections, native columns and one-to-one links.
Generic append refuses this kind for every role, including an internal writer
string. Session deletion refuses retained standing task history; reset preserves
it. The old owner/Decisions grammar and submit-slot table remain separate.

A timely direct COMMIT acknowledgement returns `created`. An actual commit with
a lost acknowledgement leaves both entry and link retained. Exact replay and
lookup are historical: they do not append, recheck present eligibility or give
another launch opportunity. Lookup remains available at controller capacity
without fabricating a current writer. No operation releases the unresolved claim.
Initial acceptance alone starts no Agent worker and proves no settled effects.

Preparation, claim and initial-task routes are Agent-only, reject all query fields
and share the existing 64-operation hub with definition and dispatch routes.
Their four POSTs
accept the same complete `StandingExecutionIntent`, bounded to 131,072 raw and
canonical bytes. Responses use the respective contract bounds: prepare 163,840,
claim reads 147,456, task admission 180,224 and task lookup 163,840 bytes. Only the
three reads permit JSON null; absence does not prove failure or release authority.

`/tasks/admit` and `/tasks/close-admission` accept
`X-Kinaiya-Standing-Capability`: exactly one value of
64 lowercase hexadecimal characters, decoded to 32 transient bytes. Admission
allows an omitted secret for retained replay; first admission and every close
still require the correct secret.
Malformed or duplicate headers are refused. The capability is neither persisted
nor returned, and browser CORS does not expose this private header. Authentication
precedes body, query and capability parsing. Actual native work remains owned
after a caller stops waiting; shutdown joins the hub before closing task/claim
capabilities and the existing owners/controller.

The [admission and HTTP evidence](../evaluation/evidence/standing-task-admission-http-2026-11-01.json)
preserves the original missing-capability baselines, first failures, independently
diagnosed fixture corrections and qualification results. These use actual SQLite
and ASGI operations with synthetic credentials and no inference. They do not
qualify a scheduler, provider, worker, inbox, owner editor or full issue #228.

## Permanent admission closure

`StandingTaskOwner.close_admission` permanently closes the first-admission
opportunity for one complete prepared original. It requires the exact transient
capability, including on replay. It shares the Store lock and transaction
boundary with first task admission. If admission wins, the closure retains its
exact native initial proof. If closure wins, the proof is null and later first
admission is refused. An exact retained admission remains historical metadata.

The immutable `StandingAdmissionClosure` binds the full intent digest, claim
identity and optional initial proof with the domain
`kinaiya.standing.admission-closure.v1\0`. Native storage joins the entire actual
claim and journal proof, rather than treating the digest alone as authority.
Two additive protected tables retain the backend/version and closure. Partial,
malformed, oversized or contradictory storage is unavailable; lookup cannot
repair it or create a fence.

Closing existing authority does not require a live producer, current Autonomous
mode, unexpired occurrence or still-enabled authorization. A new close does
require the current native writer. Historical closure lookup is clock-free and
survives controller replacement, Stop and disable. No matching claim or no
recorded closure returns null from lookup; a conflicting complete original is
refused. A lost or late COMMIT acknowledgement can leave a durable closure and
must be reconciled with the same original.

Both new routes share the existing 64 actual operation owners, 5-second ingress
and absolute 25-second deadline. Requests accept the same 131,072-byte intent;
closures are bounded to 180,224 bytes. Only closure lookup permits JSON null.
Neither operation releases a claim or establishes worker, file-effect, provider
or Resources settlement. Those remain separate acceptance gates.

The [closure evidence](../evaluation/evidence/standing-admission-closure-2026-11-01.json)
retains independent missing-capability baselines, the first combined run,
the separately diagnosed old-control error-family fixture correction and
unchanged native/API/client qualification. It distinguishes closure from
the still-required positive settlement and recurrence integration.

## Python Agent transport

`JournalClient` exposes `standing_renew`, `standing_prepare`,
`standing_claim_lookup`, `standing_unresolved`, `standing_admit` and
`standing_task_lookup`, `standing_export_admission`,
`standing_close_admission` and `standing_closure_lookup`. These methods send
strict frozen originals to the Agent-only routes above. Admission and closure
accept the optional exact 32-byte
capability; it is sent as the private header and is not put in a public packet.
The nullable reads accept a validated absence without treating it as a
failed dispatch or permission to retry work.

Each standing exchange has a 25-second absolute deadline covering serialization,
acquisition, bounded raw response reading, proof validation and actual response
close. Redirects and compressed bodies are refused. Late acquisition, body work
or close remains owned after the caller stops waiting; failed cleanup latches
uncertainty. There is no automatic retry or worker launch.

Standing, control and ordinary submit exchanges share the same actual owner
registry. The shared allocator now enforces its 64-owner limit before registering
an exchange or creating its Task. Review found that the old ordinary path could
otherwise enter as owner 65 after standing/control callers stopped waiting. Two
additional frozen tests reproduced that gap for ordinary submit and lookup; the
two-line allocator correction preserves their old error type and cleanup code.

The [transport evidence](../evaluation/evidence/standing-agent-client-2026-11-01.json)
records the original 73-case pass, the additional two failing regressions, the
reviewed correction and 228 passing new/existing checks. One production-client
test uses actual Session ASGI/auth/SQLite admission and retained history. It does
not establish a running scheduler, model provider, standing worker or inbox.

## Standing inference disclosure

Before a standing worker can make a count or generation request, the new
`StandingExportRequest` binds the full accepted initial proof and complete
`AgentExportRequest`. Native `StandingTaskOwner.export_admission` joins the
actual retained proof and current Agent session/thread/epoch. It folds the
captured task's disclosure floor with the outgoing request's scope and checks
the exact captured provider/model/local identity. A weaker request cannot
relax the original floor. Sources and retrieved context must be empty for this
first `native-files-v1` family. The internal worker adapter below enforces its
tool subset and owns the actual worker; producer and settlement integration
remain required.

The saved grant remains historical for already accepted work: later disable,
Stop, controller replacement or producer expiry does not falsely attest that
the work stopped. Session reset still fences a new export. Admission performs
no inference, state mutation, registration or producer/UTC clock sampling. Its
actual transaction, current-session checks and cancellation/deadline guards
cover the final SQL read and COMMIT acknowledgement. A lost reply grants no
permission to retry inference.

`StandingExportAdmission` binds the entire original with a domain-separated
`kinaiya.standing.export.v1\0` digest and the complete old export receipt. This
is point-in-time metadata, not a reusable upload, tool or launch permit. Both
request and response validate complete actual typed graphs before an older
validator can discard fields or normalize a forged provider. Raw nested old
packets require every field, including defaults; genuine typed defaults are
existing values. The old public packet grammar and digests remain unchanged.

The new Agent-only route shares the existing operation hub and rejects queries,
compressed input and unbound/null results. Its raw/canonical request limit is
262,144 bytes; responses are bounded to 73,728. Python
`JournalClient.standing_export_admission` uses the same owned exchange,
25-second deadline and 64-owner bound. It sends no executor capability: this
metadata check does not create a launch opportunity.

The ordinary export path refuses an accepted standing identity. It checks
native journal projections and bounded retained proof, validates proof hashes
and actual link columns, and joins the full retained claim. A missing initial
or a corrupted/counterfeit proof cannot downgrade a standing task to an ordinary
export. SQL also bounds link identity scalars before Python fetch, preserving
valid 128-character Unicode event IDs. A valid unrelated ordinary task remains
usable beside standing history.

The [disclosure evidence](../evaluation/evidence/standing-export-admission-2026-11-01.json)
records independent pure/native oracles, actual API/client integration,
preserved failures and source corrections. It does not qualify live inference,
worker effects/cleanup, recurrence, settlement, the inbox or editor.

## Internal Agent worker launch

`AgentRuntime.submit_standing(original, capability=...)` is an internal adapter
for an already prepared native claim. It validates and detaches the complete
intent and exact transient capability before any lookup or model capture. A
retained initial is returned before current provider/workspace selection and
never reconstructs or launches a worker. A new attempt checks the actual Agent
session, captured workspace object, harness and full model pin. Host profiles
require a fresh native factory; catalog and host adapters need a real
`TaskModelLease` owning the exact primary. Shared or wrapped aliases are refused
without closing their existing owner. Auxiliary providers are not captured.

Only a timely direct `created` admission, held by the same unsuppressed RAM
attempt, may launch the existing `_run` once. Lookup, lost/malformed/late replies,
caller cancellation, close and restart cannot create a launch opportunity. One
25-second deadline covers lookup, contention, model capture, admission and final
native validation. The final guard follows synchronous workspace/model checks.
The adapter shares the existing 128 actual runtime-operation owners, separately
bounds standing reservations to 64 and preserves the Journal transport's 64-owner
limit. A caller's return does not abandon resistant native operations.

The standing record retains its full initial proof and distinct origin; it does
not enter ordinary submit idempotency or manufacture an ordinary control
identity. Its `native-files-v1` snapshot intersects actual owner availability
with `read_file`, `write_file` and `finish`. Trusted dispatch and the actual
operation boundary enforce that subset even without a scoped-permissions owner.
Existing file policy, approvals, workspace/protected-root checks and completion
file verification remain authoritative. Count and generation use full standing
export admission, with no ordinary-route fallback or unrelated integration
metadata in the prompt.

Meta typed-v2 schema and decoding retain the current native snapshot's subset
and owner mask. Bound tuples must match that snapshot; an unbound three-name
tuple does not establish a standing selection. The old unbound tuple grammar
and provider defaults remain unchanged.

The real worker publishes ordinary authoritative work evidence and runs its
existing resource/model cleanup. Terminal publication and `wait()` may precede
actual cleanup. Provider cleanup uncertainty remains sticky even after a later
adapter close succeeds. The generic Resources completion callback now waits for
the exact task model lease's positive close result and prior resource releases.
Pending, cancelled or uncertain cleanup suppresses that callback for ordinary
and standing tasks; a terminal outcome alone cannot trigger restoration.
This adapter releases no native claim, responsibility or
global standing slot. Generic Resources acknowledgement is not an exact lease
settlement witness; that dependency is tracked in
[#239](https://github.com/rgoza1996/Kinaiya/issues/239#issuecomment-6558355459).
The [retirement ordering evidence](../evaluation/evidence/resource-restore-model-retirement-2026-11-01.json)
records the frozen failing baseline, actual cleanup ownership and affected
regressions; it does not qualify physical model restoration or settlement.

The [worker evidence](../evaluation/evidence/standing-agent-worker-2026-11-01.json)
distinguishes actual SQLite/ASGI/Agent/file work with synthetic measured inference
from provider-wire MockTransport checks and source review. This internal adapter
adds no public launch API, running recurrence/producer, standing Stop/steering
adapter, restart Task hydration, positive settlement, inbox or owner editor.
Those remain required for #228 acceptance.

## Task-bound Resources registration

The Resources handoff now resolves a Task against the native Session before
preemption, unloading or worker control. Missing, malformed or ambiguous native
identity cannot become an ordinary Task. Genuine ordinary Tasks retain their
existing handoff behavior; unbound owner actions remain separate.

Five additive strict packets in `packages/contracts/standing_resources.py`
preserve the complete original identity and domain-separated digests:

- `TaskResourceQuery`: exact existing Resources Task ID; 4,096-byte limit.
- `TaskResourceContext`: full standing initial proof or ordinary native submit
  identity, plus Session backend and original query; 180,224-byte limit.
- `StandingResourceReservation`: the standing context, stable Resources backend,
  actual controller instance, old action digest and preallocated lease identity;
  196,608-byte limit. The action digest retains the old payload's exact omissions
  and canonicalization; no private fields enter the old action packet.
- `StandingResourceReservationReceipt`: full request, its digest and
  `created`/`retained` disposition; 212,992-byte limit.
- `StandingResourceObservation`: full-original claim/closure joins, immutable
  bootstrap coverage and optional reservation metadata; 212,992-byte limit.

The new authenticated Session POST routes are:

| Route | Required role | Request |
|---|---|---|
| `/v1/standing/resources/context` | Resources | `TaskResourceQuery` |
| `/v1/standing/resources/reserve` | Resources | `StandingResourceReservation` |
| `/v1/standing/resources/observe` | Agent | `StandingExecutionIntent` |

These routes share the existing 64 actual operation owners, five-second ingress
limit and 25-second whole-operation deadline. They reject the private admission
capability header. `JournalClient.resource_task_context`,
`standing_resource_reserve` and `standing_resource_observe` bind the full reply
to the original request and retain actual transport cleanup ownership.

`StandingResourceOwner` is registered explicitly after the native Task owner.
Its two protected tables retain the native backend, a bootstrap journal sequence
captured once, and the complete reservation. Reads never bootstrap or repair
storage. Ordinary discovery checks native submit slots and both bounded journal
projections; a corrupt slot cannot disappear merely because its serialized
operation was changed. First registration shares the Store transaction and
permanent admission-closure fence: close-first refuses registration, while a
previously committed exact reservation remains historical after closure, Stop
or controller replacement. Accepted work does not need renewed scheduling
eligibility to retain its history.

The actual Resources coordinator owns a separate protected connection to its
existing action database. It registers its stable backend/current instance
before startup worker actions and retains a complete local pending intent
before asking Session to reserve. Only a direct, timely `created` receipt held
by that same unsuppressed RAM attempt permits a new handoff. The full receipt
must be durably stored before effects, and the resulting lease uses the original
preallocated identity. The 25-second capture deadline covers context lookup,
registration and final validation before the first effect; it is not a new
whole-hardware-action deadline. Existing locality, measured headroom, readiness,
owner-Off and native worker-identity checks still apply.

Lost, malformed, late or retained replies never launch effects. Any retained
local intent, including an acknowledged one, suppresses automatic startup
restoration rather than pretending restart proved cleanup. Old action replay
and pending semantics remain unchanged. Missing gateway or suppressed capture
raises a fixed `StandingUnavailable`/HTTP 503; definite identity conflicts use
`StandingConflict`/HTTP 409. Actual caller cancellation remains cancellation.
Failure of this capability does not remove unbound owner Off, inspection or
cleanup. Failed worker close retains verified process handles for retry rather
than dropping cleanup authority.

This increment provides registration and observations only. An absent
reservation, coverage metadata, a generic Resources `ok`, terminal Task state or
a missing Resource client is **not** a no-lease or cleanup certificate. No claim
is released; the single lifetime claim and reservation remain bounded and
retained. Exact positive Resources finalization, original-worker/effect settlement,
safe active-slot release, recurrence, the durable inbox and embedded editor are
still required. The [registration evidence](../evaluation/evidence/standing-resource-registration-2026-11-01.json)
separates real disposable-process/SQLite/HTTP checks from unqualified live
hardware, model and provider behavior.

## Prework binding and Session finalization testimony

The integration branch adds six strict packets in
`packages/contracts/standing_resource_finalization.py`: binding request/receipt,
finalize request/context, finalization and its receipt. They preserve complete
claim, original intent, admission closure and resource observation graphs.
Their separate codec accepts at most depth 16 and 16,384 nodes; older codecs and
their 184 schema files are unchanged. These bounds do not apply to transcript
metadata or establish physical cleanup.

| Session POST route | Role | Request → response byte caps |
|---|---|---|
| `/v1/standing/resources/binding` | Resources | 163840 → 180224 |
| `/v1/standing/resources/binding/lookup` | Resources | 163840 → 180224 or null |
| `/v1/standing/resources/finalization/context` | Resources | 147456 → 786432 |
| `/v1/standing/resources/finalization/report` | Resources | 802816 → 819200 |
| `/v1/standing/resources/finalization/lookup` | Agent or Resources | 147456 → 819200 or null |

Binding must precede first task admission or permanent admission closure.
The native owner validates the actual prepared claim and fixes its Resources
backend/controller identity. Reservation creation checks this binding in the
same transaction before insertion and before COMMIT. Explicit native
registration attaches the reciprocal owner as well as HTTP composition; a bare
legacy owner cannot bypass existing new-family state. Genuine family absence
preserves the old reservation protocol, and exact retained receipts remain
historical reads.

Context requires the genuine binding and permanent closure. The Session owner
recomputes the full context from protected native rows, then records the first
exact Resources report immutably. A different idempotency key, policy revision
or report body cannot rewrite it. Agent can read recorded testimony but cannot
create binding/report authority. No private executor capability is accepted on
these routes. The new response bounds remain separate from the older 262,144-byte
transport clamp; all standing routes share one 64-operation registry and the
existing 25-second total/five-second ingress bounds. Shutdown fences admission
before joining actual operations and retiring collaborators.

This is **native Session testimony**, not a physical Resources certificate.
The actual Resources controller has not yet implemented prework binding RPC,
capture sealing/drainage, durable lease acquisition/restoration witnesses or
production of these reports. Agent supervisor retirement, positive settlement,
active-slot release, recurrence, inbox and editor remain required. In
particular, `no-reservation` in a parsed packet cannot establish that no actual
operation acquired resources, and no claim/reservation is released here.

The [finalization evidence](../evaluation/evidence/standing-resource-finalization-2026-11-01.json)
records independent contracts/client/native oracles, original source failures,
the documented fault-fixture correction, two reproduced native composition
bypasses and their source repair. The final 96-case native/Resources run passed;
the separate 63/74/69/239-case runs overlap and must not be added into a full-suite
or physical-cleanup claim. One unrelated old TestClient smoke still has a
recorded strict-warning collection error in the local locked environment.

## Native dispatch gate

`services/session/standing_dispatch.py` implements explicit registration,
producer renewal, gate observation, owner activation, historical lookup and
target-epoch Stop against the same native Store and Agent controller. Opening
or closing this local capability does not register or retire a controller.
Its four protected tables retain the gate, immutable activation proofs and
bounded producer highwater state. These operations start no timer or worker.
The strict exported packets are distinct from the unchanged ordinary
owner/Decisions submission grammar.

Session startup explicitly registers this local gate even if ordinary controller
registration is exhausted, preserving limited read/Stop over established healthy
storage. Missing or corrupt authority stays unavailable. The exposed
`standing_dispatch_initialized` lifecycle flag proves neither a live producer
nor permission. Shutdown joins the shared actual-operation hub before closing
both local standing capabilities and retiring the actual Session controller.

An activation binds a full ordinary Autonomous mode request, the observed
dispatch epoch and random fence token. Its receipt binds the ordinary native
ModeReceipt, next epoch and complete activation envelope. Replaying either
receipt must never rearm a stopped epoch. Stop proves only its issued target,
not Off, later epochs, worker retirement or settled effects. A fresh matching
Stop must rotate the fence even at epoch zero, before an already-fenced fast
path can return; a stale retry must not rotate a newer activation.

Emergency Stop must remain representable over valid established storage when
normal controller registration reaches capacity. A null current controller is
reported literally, with writable false and no live producer. It is never
replaced by an invented UUID or fabricated Off ModeReceipt.
`stop_context()` reads a current-target request template without clock sampling,
mutation or writer assignment. If there is no current controller, it uses an
actually issued historical controller identity. The template grants no new
writer authority and is not itself evidence that Stop happened.

Producer receipts carry a fixed 15,000 ms interval on the native current
Session's monotonic clock. A receipt alone does not prove current liveness.
The owner retains at most 10,000 producer identities and one latest renewal
per identity; it does not append a ledger row every heartbeat. Exact sequence
replay keeps its original expiry; retired producers cannot renew and old Session
leases never revive on clock rollback. Clock failure removes readiness while
preserving emergency Stop over healthy storage.

Fresh activation atomically saves its ordinary mode receipt and dispatch epoch.
Activation and renewal recheck cancellation, their absolute deadline, the current
controller and live lease before COMMIT, including a final cancellation/deadline
check after SQL validation. Rollback failure poisons the local capability rather
than supplying a proof. A lost COMMIT acknowledgement remains uncertain even
when a later lookup retrieves the retained receipt.

Ordinary restart may preserve an unstopped Autonomous intent under its original
finite owner grants, but future work needs a genuine new current producer lease
and every admission check. Off/on-demand revision changes and epoch Stop cannot
be undone by renewal. Unknown accepted work retains its capacity across restart.
Only a positive original-supervisor cleanup report can establish settlement;
terminal Task state, timeout, lease expiry and history lookup cannot substitute.

The remaining acceptance chain is owner editor → native authorization/occurrence
→ explicitly attributed standing admission → one permitted real Agent task →
durable deduplicated inbox. Converse must be unnecessary. Do not advertise that
chain from authorization CRUD or these packet schemas alone.
