# Native Converse-to-Agent handoff receipts

Issue #141 extends the existing journal envelope schema `1.0` additively. Decisions
still classifies intent; the native Agent still owns admission, source disclosure,
permissions, task execution, and completion evidence. Model labels grant no
authority. Neither provider location nor a failed status probe rewrites a valid
work intent into casual dialogue.

`routing.dispatch` (finalized text) and `routing.window_dispatch` (rolling speech)
are authenticated `origin: decisions` events. Consumers must bind the envelope's
session, thread, epoch and causes to the authenticated source, then bind payload
source ID, native sequence and revision. These fields are never taken from model
output. Finalized corrections refer to the original `conversation.user` ID and
sequence, use its current revision, and retain the correcting event as the cause.
Both native user source kinds have origin `authenticated-user`.

Example finalized-text acceptance payload:

```json
{
  "decision_id": "decision-141",
  "source_event_id": "user-141",
  "source_sequence": 23,
  "source_revision": 1,
  "route": "queue",
  "target_task_id": null,
  "state": "accepted",
  "reason": "executor_accepted",
  "native_task_id": "task-141",
  "native_task_state": "accepted"
}
```

Window payloads retain `agent_route` (the uppercase WindowDecision enum),
`semantic_key`, and existing `queued` / `applied` states instead of text's `route`
and `accepted`. The source revision is the exact window `revision`, not its
`window_index`. Window receipt causes reference the native `routing.window_decided`
event. Text receipt causes reference the finalized source or its correction.

| State | Native meaning |
| --- | --- |
| `accepted` (text), `applied` (window) | Executor returned a valid Task with the same session/thread/epoch, bounded native task ID, and known TaskState. Steer/interrupt ACK IDs must equal their target. A queue ACK must match the stable submission idempotency key; it creates a new task, so its target can be null or a prior task. |
| `queued` (window) | Durable intent waits for a confirmed native task boundary. No submission has been acknowledged. |
| `rejected` | Native source/target admission refused the operation. An HTTP 403, 404, or 409 uses only `reason: executor_rejected` plus integer `http_status`. No response-body privacy explanation is inferred. |
| `unknown` | Timeout, transport failure, other HTTP status, malformed response or mismatched ACK leaves application unconfirmed. Uses `reason: executor_ack_unknown`. |
| `not_applied` | No mutation was attempted, for example explicitly disabled Agent or unavailable status before a targeted window control. |

An interrupt ACK records only the executor-reported state; it does not prove that
work stopped. Acceptance never proves successful task completion. Unknown outcomes
are durable terminal dispatch receipts, and restarts do not blindly replay them.
The native Agent idempotency key remains unchanged. Historical typed rejections
remain terminal even without the additive identity fields; readers must not
interpret those older receipts as new positive ACKs.
Text admission keeps the existing Agent HTTP client's 15-second bound; window
mutations retain their one-second bound. If the native journal is unavailable
before an outcome receipt can persist, stable native Agent idempotency remains
the duplicate-effect protection. Dispatch and receipt persistence are not one
distributed transaction.

If a window classifier's bounded Agent status probe fails, it records
`routing.window_status` with phase `agent_unavailable`, reason
`agent_status_unavailable`, source ID, sequence and revision. Valid classifier
intent is retained. A queued dispatcher must observe current Agent status and
recheck source freshness, latest text, epoch, interrupt fences, target revision
and native admission before submitting. Superseded/reset sources cannot become
work after recovery. Explicit Agent disablement still suppresses future replay.
Status receipts with optional identity fields are validated against their native
source; historical receipts without those fields remain compatible.

Decisions health and owner status add `window_agent_available` (the classifier's
last observed successful/failed status result, or null before observation or when
disabled), `window_dispatch_error` (bounded exception class/constant), and
`window_dispatch_running`. These are observations, not proof that Agent is ready
now. Dispatcher errors clear after a successful native status read, not merely
because unrelated journal entries arrived.

Verification: `tests/integration/test_handoff_receipts.py` exercises authenticated
ASGI AgentClient HTTP refusals and unknown outcomes, actual native Agent
disclosure refusal with zero inference, ACK identity/state binding, durable replay,
correction identity, unavailable-status queue recovery, supersession/reset fences,
and strict optional status identity. Existing routing, backlog, native Agent,
disclosure and asynchronous Converse tests remain required. No live models or
external services are exercised by these fixtures.
