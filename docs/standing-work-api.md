# Standing work: owner authorization and dispatch boundaries

Requirement: [#228](https://github.com/rgoza1996/Kinaiya/issues/228), within
[M7](https://github.com/rgoza1996/Kinaiya/issues/10) and the
[Options delivery plan](options-delivery-plan.md). Read with
[Agent operating modes](agent-control-api.md), Roadmap §13/§19 and clarification.md.

This branch implements the authorization owner, native dispatch gate and their
authenticated Session APIs, plus immutable authorization/dispatch Dart proofs and bounded
transport. A saved or armed authorization is **not** a scheduled task,
producer-health claim, execution receipt or completed responsibility. Native
occurrence admission, an actual Agent producer, positive execution settlement,
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
carried. The secret has no DTO, journal, Task or prompt field. Future native
admission, close and settlement must verify that private capability; reading a
historical claim must never let another process adopt a launch or attest cleanup.

The pure `standing_initial_request` builder emits the explicit
`agent.standing-command` / `standing` family with no fabricated conversation
sources. It checks the real existing AppendRequest serialization cap of 65,536
bytes. The unchanged owner/Decisions parser refuses this new family. Native
admission must still join the full retained claim and protected authorization
before assigning a `standing-supervisor` origin and appending that proposal.

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
source review and additive schema export. Native claim storage and the working
editor → occurrence → Agent → settled inbox chain remain unqualified.

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
