# ADR 0078: Conditional resource actions preserve newer Converse owner intent

Status: experimental implementation for [#184](https://github.com/rgoza1996/Kinaiya/issues/184),
under [#9](https://github.com/rgoza1996/Kinaiya/issues/9), Roadmap §14.3 and AT14.
Extends [ADR 0023](0023-converse-owner-intent.md) and
[ADR 0013](0013-resource-handoff.md). This slice does not qualify live voice,
model replacement, temporary GPU leases or the full M6 milestone.

## Problem and authority

A client reading owner revision R before a temporary resource change cannot
safely restore an earlier choice with an unconditional on action. Another
owner request may have written off at revision R+1 between the read and the
restore. Idempotency prevents duplicate action execution; it does not prevent
overwriting a newer, different owner request.

The native Converse owner endpoint and resource actions share the same durable
intent file and write lock. Conditional resource changes must compare and write
under that lock, rather than checking a status response before an unrelated
write. Owner intent remains distinct from resource readiness and actual audio.

## Additive owner action

`POST /v1/resources/actions` retains its owner authentication, strict bounded
operation-specific body and idempotency key. A converse action may also provide
`expected_owner_intent_revision`, a strict integer from 0 through 2^63−1:

```json
{
  "operation": "converse",
  "enabled": true,
  "expected_owner_intent_revision": 17,
  "idempotency_key": "synthetic-restore-17"
}
```

Omitting the precondition or explicitly supplying null retains the existing
unconditional owner command. Supplying it on another operation is invalid,
including explicit null. Boolean, coerced, negative and oversized revisions
are invalid. This is an additive resources API field; it does not change the
native Converse endpoint or grant Agent permission to change owner intent.

`ConverseIntent.set(enabled, expected_revision=R)` compares R with the freshly
read record and replaces it atomically under the existing per-write flock used
by all writers. Every accepted write increments the revision exactly once,
even when the requested boolean already matches. Thus a newer same-value
owner write also invalidates an older conditional action. Overflow refuses.
The in-memory store follows the same revision semantics. Existing private-file,
no-follow, nonblocking, bounded-read, fsync and atomic-replace protections remain.
No lock is held across worker loading, control I/O or other awaits.

## Results and side effects

A stale comparison raises `OwnerIntentConflict` carrying the immutable record
observed under the write lock. The resources action returns an ordinary durable
result with `ok:false`, `owner_intent_conflict:true`, a constant error and
`observed_owner_intent:{enabled,revision}`. Its returned policy projects that
observed choice without saving a new policy. The action ledger may record this
result; the stale action must not change the intent, policy file, worker/control
state or managed inference admission/preemption counters.

In particular, a conditional off must not announce eager preemption while
waiting for the coordinator's action lock, before its comparison is accepted.
Existing unconditional emergency-off preemption remains available. Unsafe or
unreadable records produce the existing refusal path, not invented observed
values or an enabled default.

When an owner write is accepted, completed action results retain
`owner_intent_change:{enabled,revision}` from that exact write, captured before
any await. Preserve it even if subsequent resource work fails. A later owner
write may already have superseded it by response time; the receipt is evidence
of the accepted transition, not a claim of current choice or ready workers.
Restoration keeps its existing fresh owner checks around asynchronous startup.

## Replay, restart and uncertainty

The existing SQLite ledger stores the exact request digest and final result.
An identical key/body replays that result without executing effects; conflicting
bodies are refused. Omission and explicit null are distinct request bodies even
though both select unconditional behavior. A replayed accepted transition or
conflict observation remains historical; inspect current status separately.

The intent file and action ledger are separate stores. A crash or cancellation
after an intent update but before the final ledger result can leave an uncertain
action. An existing null-result row stays uncertain and does not repeat the
write. This design does not claim atomic transactions across both stores,
automatic rollback, or that a lost response means no change occurred.

## Verification and limits

Use synthetic, deterministic unit and coordinator/API tests for successful and
stale comparisons, shared writers/processes, same-value invalidation, strict
revision bounds, unchanged file/lock protections, newer writes during awaited
resource work, accepted receipts on later failure, exact replay, conflicting
bodies, restart/lost acknowledgment and null-result uncertainty. Include managed
preemption seams and preserve unconditional emergency-off behavior. Retain the
actual failing baseline before evaluating the candidate.

The original `47f71b0` source produced 31 failures and 33 passes on the frozen
64 new cases. With the implementation, the same cases and all 79 existing cases
in these two files passed (143 total):

```bash
PYTHONPATH=. pytest -q tests/unit/test_converse_intent.py \
  tests/integration/test_resource_handoff.py
```

Independent review and exact-head repository CI remain required before merge.
These deterministic regressions do not qualify live resource actions, actual
model placement/performance or physical speech. No live controller/profile
change or model inference was used for this slice.
