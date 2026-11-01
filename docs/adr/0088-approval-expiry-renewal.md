# 0088: Visible approval expiry and exact owner renewal

October 31, 2026. Prerequisite for precise permissions under
[#208](https://github.com/rgoza1996/Kinaiya/issues/208).

## Native lifecycle

The supervisor previously hid expired approvals while workers waited for a
resume signal. Broker records had no closed-history bound. Expiry now pauses
the same action and retains a visible `status:expired` owner request. The waiter
checks at most one second later, without model, journal or effect calls on
each tick. A monotonic deadline also prevents backward wall-clock changes
extending a permit. It records `approval.resolved` with `approved:false,
reason:expired` and an `agent.state` pause. Uncertain metadata acknowledgment
does not restore authority or imply an effect ran.

The owner can deny, stop the task, or explicitly renew. Renewal creates a fresh
**unapproved** permit after checking exact action digest, objective, task,
source, epoch, disclosure, tool identity and native policy. It never reruns
inference or effects. Steering supersedes the action; recovery cannot recreate
live authority from journal text. Denying stale expired work needs no new grant.

Owner-only `POST /v1/approvals/{old_id}/reissue` accepts strict versioned
`ApprovalReissue {schema_version, action_digest, objective_revision}`. The old
ID is the transition identity. Typed `ApprovalReissueReceipt` contains
`schema_version`, `status:reissued`, `previous_approval_id`, `approval_id`,
`action_digest`, `objective_revision`, `expires_at` and
`replacement_status:pending|expired|used|retired|unavailable`. An identical
replay returns the historical replacement and its current state. It never
renews the replacement implicitly. A conflicting digest/revision is refused.
HTTP410 means bounded receipt history is unavailable, not that an earlier
request failed or another action is authorized.

## Ownership and limits

The broker permits at most 1024 live or retained approvals and 256 closed records.
Paused waiters retain their permits; pruning cannot discard a live waiter or
dispatch permit. Renewal transfers that slot synchronously. Older receipts
occupy bounded closed history. Capacity refusal requires explicit retirement.
Waiter cleanup releases ownership on denial, steering, cancellation, publication
failure or settled dispatch. Cancelled terminal tasks retain no actionable row.

Owner intent is not a spend. Effect brokers still validate and consume once
after final source/revision/policy checks. Expiry during awaited admission
returns to the same paused action; used permits cannot be restored. Broad
grants and YOLO still require exact owner approval for uncommitted speech.
Hard Deny, containment, protected paths, isolation and disclosure remain ceilings.

## Flutter and evidence

Expired rows disable Approve and expose Renew request and Deny. New requests
require separate approval. Unknown renewal attempts retain the exact old-ID/body
in bounded RAM (at most 64), across views and same-backend/owner reconnects.
They are not stored in preferences or logs. Late replies cannot affect a
replacement connection. Missing receipts remain unconfirmed, with exact retry
and task inspection available.

The existing invalidated-approval case is updated for visible expiry, immediate
refusal, paused state and cancelled-waiter retirement. No new automated cases
or local test execution were introduced. Static review, schema export, formatting,
compilation and Dart analysis are distinct from exact-head CI recorded on the
PR. This prerequisite does not deliver read/per-tool rules or broader scopes;
the [permission rollout](../options-permission-plan.md) remains open.
