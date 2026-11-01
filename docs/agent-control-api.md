# Agent operating-mode control

Requirement: [Roadmap §13.1](../Roadmap.md#131-operating-modes),
[M7](https://github.com/rgoza1996/Kinaiya/issues/10) prerequisite
[#217](https://github.com/rgoza1996/Kinaiya/issues/217).
The strict version 1.0 wire grammar is implemented; native enforcement and the
owner panel are being integrated. This document does not establish qualification
or completion. See [ADR0105](adr/0105-agent-operating-modes.md).

## Ownership and routes

Session owns the mode in its protected SQLite journal database. Mode is separate
from service health, Converse intent, permission presets and tool grants.
`off` refuses new task and steering admissions. `on_demand` permits explicit,
otherwise authorized requests, including source-validated Decisions routing.
`autonomous` additionally requires a real, authorized standing-work producer.
The initial implementation advertises only `off` and `on_demand`, with
`standing_work_available: false`; the enum does not implement monitoring.

| Route | Authenticated role | Request → response |
|---|---|---|
| `GET /v1/agent-control/state` | owner, agent | → `AgentControlSnapshot` |
| `POST /v1/agent-control/changes` | owner | `AgentModeChange` → `AgentModeReceipt` |
| `POST /v1/agent-control/changes/lookup` | owner | full original `AgentModeChange` → `AgentModeLookupResult` |
| `POST /v1/transcript/agent-steers` | agent | `AgentSteerAdmissionRequest` → `AgentSteerReceipt` |
| `POST /v1/transcript/agent-steers/lookup` | agent | full original admission request → `AgentSteerLookupResult` |

Only the owner changes mode. Agent's read access discovers the stable control
backend for steering; `control_writable` describes the serving controller's
registration, and grants no credential additional authority. Decisions output,
conversation text, model output and journal contents cannot change mode.
All control responses, including refusals, use `Cache-Control: no-store`.

## Mode writes and proof

Every change supplies all seven fields; there are no inferred identity fields:

```json
{
  "schema_version": "1.0",
  "idempotency_key": "mode:public-synthetic-1",
  "expected_backend": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "expected_instance": "01234567-89ab-cdef-0123-456789abcdef",
  "expected_revision": 0,
  "mode": "off",
  "active_work_policy": "finish_already_accepted"
}
```

The backend is a dedicated 32-character lowercase hex identity. The active
control-writer generation is a UUID, registered explicitly by the Session
control API. Opening another Store or LocalJournal does not retire it. Maintain
issued-generation history; a retired generation cannot become active again.
Bootstrap defaults to on-demand. An explicit legacy false flag may seed Off
only for a positively identified new store; persisted owner mode wins thereafter.
New journals retain durable fresh/uninitialized provenance and a stable backend
before controller registration. A backend-only read does not select a mode.
After failed first registration, a pending journal requires explicit Session
startup on reopen; it cannot be mistaken for a legacy on-demand journal.
The trusted original creating Store may initialize the default at its first
native admission only before its own first API-registration attempt. Auxiliary
and reopened pending Stores have no such initialization privilege. If that
original creator and a separate controller race their initializations, the first
native commit establishes mode. An uncommitted Off intent in the other
controller is not a persisted Off setting or a fence on independent authority.

Within the same SQLite acceptance transaction, exact retained request/receipt
proof precedes current writer/revision checks. Replay proves the historical save
and never reapplies its mode. New changes require the active local controller,
matching backend/generation/revision and an actually available mode.
Task admission checks the shared mode inside its existing acceptance transaction,
after retained task proof and before new acceptance DML.

Lookup matches the full original request, not a key or equal current value.
An unobserved request is permanently fenced only by its **known issued and
retired** generation, or a persisted revision greater than its expected revision.
An unknown arbitrary generation is a conflict. Off, temporary busy state,
read-only observation, equal values or a future expected revision do not prove
absence. Database replacement is a foreign backend, even at the same URL.
This protocol assumes protected, unforked, non-rolled-back native storage.
Quiesce older Session/Agent binaries before enabling this authority: code that
does not participate in native mode admission cannot enforce the new setting.

## Active work and steering

`finish_already_accepted` permits accepted work to finish under its existing
authorization, source, completion and resource-ownership guards. Off does not
cancel work, free a provider, undo effects, or prove an unknown write absent.
Stop, inspection, approvals, historical lookup and owner controls remain usable
without Converse or inference. The UI reads actual task state separately.

A steering intent binds the complete original six-field `SteerRequest`, its
authenticated actor, effective disclosure scope, ordered source revision/scope
observations and the immutable initial native submit identity. Task ID alone is
insufficient. Preserve literal instruction/key characters and ordered duplicate
sources; sanitization must not change request identity.
Serialize one accepted intent per canonical task/base objective revision. Commit
the intent and native proof before changing RAM scope, queuing a goal or waking
the worker. A direct-created receipt may install only after fresh live checks.
`intent_accepted` and its target revision do not mean the objective was applied.
Historical proof, including retained replay, never installs new RAM work.

Lost steering acknowledgement remains scoped uncertainty. An unobserved lookup,
Off/on-demand transition or subsequent refusal cannot clear it. Initial recovery
is inspection, owner Stop/confirmed retirement, then a new task. Continuing the
same task/base revision requires a separate native intent retirement/consumption
protocol and is not implemented by this contract.

## Bounds and client behavior

Mode requests are at most 4,096 bytes; mode snapshots/proofs at most 16,384.
Steering ingress is at most 131,072 bytes, its canonical admission request at
most 130,048, and full proof at most 262,144. The canonical reserve guarantees
room for a lookup that echoes the request twice. Native task identity has an
8,192-byte metadata bound. JSON permits at most 12 levels, 2,048 nodes and
integers through `9007199254740991`; next-revision requests stop one below it.
Reject duplicates, floats, non-finite numbers, malformed UTF-8/surrogates,
trailing data, coercions and unknown fields before mutation. Generated schemas
describe field shape; the strict byte decoder also enforces aggregate bounds.

Refuse invalid wire with 422, unsupported content type/encoding with 415,
authentication with existing 401/403, identity/revision/key/capability conflicts
with 409, new admission while Off with 403, and unproven storage/proof with 503.
Public reasons are fixed and do not echo input or SQL. Any later refusal remains
distinct from proof about an earlier dispatched request.

The panel scopes RAM-only pending changes to private owner credential, canonical
Session endpoint and native backend. Retain immutable originals through view
close/reconnect; never persist credentials or retry/poll mutations automatically.
The shared model/runtime/control transport pool owns at most 32 actual exchanges.
A public timeout, facade close or logical settlement does not release an actual
owner before genuine cleanup. Model/runtime semantics retain their own contracts.
Historical receipt and current observed mode are separate UI states; unsupported
Autonomous and unknown mode remain explicit rather than inferred from health.

The native journal retains at most 10,000 controller generations, mode-change
proofs and steering intents in each respective table. Reaching a limit refuses
new records; this slice provides no pruning or history-compaction protocol.
Mode/intent exhaustion preserves retained lookups in an already registered
controller; generation exhaustion prevents a new controller from starting.
These are protected-store lifetime limits, not claims about standing-work
throughput.

The Agent HTTP steering operation has a 25-second public boundary. Its preparation,
native admission and fresh-authority phases use 5-, 15- and 5-second guards.
Runtime initial-submit and steering operations share the existing 128-operation
owner limit; nested native exchanges consume that pool too. JournalClient
separately bounds its shared actual exchanges to 64. Timed-out or cancelled
operations retain actual ownership until settlement. An internal direct runtime
call has passive preparation guards and can remain awaiting an uncooperative
source adapter; the production HTTP route supplies the outer deadline. These
limits do not assert a separate Session request-ingress time limit.
