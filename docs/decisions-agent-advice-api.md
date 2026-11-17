# Internal Agent advice HTTP boundary (#566 / #567)

The launcher installs the native `AgentAdviceOperations` owner when both Agent
and Decisions are selected and Decisions has a valid policy at launch.
The [protected policy store](decisions-consumer-policies.md) selects the next
policy or defers to the legacy host declaration.
The owner shares the service's policy and journal objects. The app lifespan
starts it and observes its bounded cleanup on shutdown.

## Authentication and contracts

Every route below requires the **Agent service credential**. Owner, Converse,
Decisions and Resources credentials cannot enroll or control an advice request.
The ordinary owner policy inspector remains separate. POST authentication is
rechecked after awaited ingress, before enrollment, lookup or cancellation.

When the launcher installs the owner, `GET /v1/decisions/options/agent-advice-runtime`
exposes the same metadata to the owner credential with `Cache-Control: no-store`.
Operations remain available only to the Agent credential.

| Method and path, under `/v1/decisions/agent-advice` | Request | Result |
| --- | --- | --- |
| `GET /runtime` | No query envelope | `AgentAdviceRuntimeState` |
| `POST /requests` | `AgentAdviceCall` | HTTP 202 `AgentAdviceObservation` |
| `POST /lookup` | `AgentAdviceLookup` | `AgentAdviceLookupResponse` |
| `POST /cancel` | `AgentAdviceLookup` | `AgentAdviceLookupResponse` |

Requests require identity JSON, one unambiguous header envelope, no query
parameters and an exact declared body length when present. Ingress has a
five-second bound. Calls retain their 4 MiB resource bound; lookup/cancel metadata
has a 4,096-byte bound. Parsing rejects duplicate keys, noninteger/nonfinite
numbers, invalid UTF-8, unsupported fields and versions, and structural excess
(12 levels or 16,384 JSON nodes). The declared 2,048 role-bearing message limit
fits this structural bound. No semantic or style requirement is imposed.

Successful responses and explicit handler errors use `Cache-Control: no-store`.
Envelope/length/disconnected ingress returns 400, unsupported media or encoding
415, excess input 413, ingress timeout 408, invalid versioned input 422, original
controller/key/digest conflict 409, and unavailable original custody 503.
Authentication preserves the native 401/403 behavior. Diagnostics are constant
and never reflect input text or private exception details.

The response bound is six times the 4 MiB raw output bound plus 65,536 bytes for
metadata: JSON can expand a one-byte control character to six escaped bytes.
Complete raw output, including empty text and NUL, is preserved within those
declared bounds. All 612 inherited schemas remain unchanged; runtime-state and
exact-lookup-envelope schemas are additive.

## Enrollment, observation and lifecycle

Enrollment returns the observation of one original immediately. An HTTP 202 or
lookup result says nothing about model quality or Agent task success. Losing the
acknowledgment, disconnecting, or repeating the same original cannot rerun the
model. Every actual measured upload still requires the native Session
[task/source/disclosure admission](decisions-agent-admission.md).

Lookup/cancel responses echo the exact controller, logical key and request digest.
An observation belongs to that original or validation fails. A null observation
means no matching original was observed at that point; it does **not** fence a
late arriving request or confirm cancellation. A changed original controller
conflicts rather than becoming a new attempt. Cancellation sets the original's
request flag; pending or unconfirmed actual work remains owned.

Runtime inspection reports lifecycle, bounded active/retained counts, policy
digest and catalog metadata status. `new_request_available` describes space and
metadata eligibility for new enrollment, not model health, inference quality or
permission. Exact repeated requests and passive lookup can remain usable after
new enrollment capacity is exhausted. Inspection opens no model or credential.

Shutdown requests cancellation of known originals and waits for a bounded cleanup
observation. It reports `closing` or `cleanup_unconfirmed` while ownership remains
active, never `stopped` merely because cancellation was requested. A retired
controller cannot restart or use a replacement model to satisfy an old lookup.

## Remaining integration

The [Agent-side client](decisions-agent-advice-client.md) retains the original
submission, its local exchanges and its remote cleanup result. Ordinary Agent
workers can use it through the native `consult_model` tool. Accepted workers
remain subject to the existing `finish_already_accepted` operating-mode policy.
Standing-task admission and full
consumer/provider/external-agent qualification remain part of #566/#567.

The synthetic native prediction wire's missing cancellation protocol was recorded
as #697. Its fixture now distinguishes a qualified terminal acknowledgment,
a held acknowledgment and a channel close without a terminal; production refuses
unqualified cleanup. First failures and exact source/probe evidence are retained
in the [HTTP-boundary verification record](evidence/agent-advice-api-2026-11-17/README.md).
