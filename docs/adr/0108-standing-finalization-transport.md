# ADR0108: Scoped Resources finalization transport

Status: implemented and qualified in the offline harness at
`fab6e613e17c49cca1ccc9b08a7f9125db97d588`, stacked on the
[physical producer](0107-standing-physical-finalization.md). Applicable exact-head
CI and parent integration remain required. This is transport for #239 / #228;
it does not add an automatic Agent caller or certify Agent retirement.

## Interface and authority

The existing Agent-role Resources client adds:

- `finalize_standing_claim(StandingResourceFinalizeRequest)` returning a complete
  `StandingResourceFinalizationReceipt`.
- `lookup_standing_finalization` with the same complete request, returning that
  receipt or literal null when no matching committed native history exists.

Their fixed POST routes are `/v1/resources/standing/finalization` and
`/v1/resources/standing/finalization/lookup`. Both use the existing Agent role,
strict ingress, shared 64 actual-operation slots and original producer scope.
There is no second pool, HTTP client, arbitrary ResourceAction or permission.
The executor's private capability header is forbidden. Owner Off remains an
independent authorized operation.

Complete request graphs are freshly validated and detached before the first
await. The existing limits remain: request 147,456 bytes, full receipt 819,200
bytes, depth 16 and 16,384 nodes. Both server and client freshly join the full
result to the original request, including backend/controller/key/context/report
identity. No old 262,144-byte clamp, abbreviated receipt, schema change or
cross-await validation cache is introduced.

## Time and original ownership

The new public methods share one absolute 25-second budget, beginning before
complete request freezing and extending through readiness settlement, the
original exchange and the final complete receipt join. Expiry during validation
refuses a late positive return; synchronous validation is still completed.
Expiry before sending creates no request. This is deadline classification, not
a promise to preempt synchronous parsing exactly at a wall-clock instant.
Server ingress retains its existing five-second bound and operation deadline.

The actual original send, response read and sole-close remain owned by the
existing exchange. Caller cancellation, timeout or a lost facade cannot certify
cleanup, close the transport early or start a replacement close owner. Genuine
cleanup failure remains sticky. Positive return requires the full bounded
identity response and its original close, followed by fresh request validation.

Lookup manufactures no binding, restoration or report. A lost acknowledgement
uses the exact frozen request/key and immutable native history. It cannot repeat
the physical restoration or infer absence from an unsuccessful request.

## Verification and retained findings

The [evidence record](../../evaluation/evidence/standing-finalization-transport-2026-11-02.json)
separates the original independently frozen 40 cases from six later source-aware
deadline controls. The original 40 first passed; review then found that request
freezing preceded the client deadline and the final receipt join lacked a
post-validation time check. All six logical-clock controls failed before the
correction. They transparently execute real complete validation before advancing
time; their final-join case also requires actual healthy response-close evidence.

After correction, all 46 cases pass, including real ASGI/Session/SQLite/CPU-worker
paths, complete large receipts, exact bounds, role/no-store checks, lost replies,
capacity and original cancellation/close ownership. All 254 affected older
client/barrier/API cases pass before and after the correction. Whole Ruff,
format (777 files), contract export/schema identity and diff checks pass.
Every tracked pin and HEAD remained unchanged during each qualification run.
Counts from repeated or overlapping runs must not be summed as unique coverage.

At this increment, the four old binding/seal methods retained their original
paths and behavior. Their inherited missing post-receipt-validation clock check was separately
[recorded](https://github.com/rgoza1996/Kinaiya/issues/239#issuecomment-6572228960)
as a source-review finding; this initial increment did not repair it. The
later [client return-deadline correction](../../evaluation/evidence/standing-resources-client-return-deadline-2026-11-02.json)
checks the original deadline after the complete receipt join and before any
successful return, including lookup-null. Complete validation and original
HTTP/sole-close ownership remain unchanged. A late-return refusal does not
establish that a binding/seal failed to commit or authorize a replacement attempt.
The earlier capability-absence result and six reproduced deadline failures remain
in the record. Raw logs/JUnit/receipts are ignored local artifacts identified by
hash, not claimed as published CI artifacts.

## Remaining Agent boundary

Agent must retain a claim owner before its first preflight await, retain genuine
binding evidence even for never-admitted work, and keep restricted original
closure authority without reopening the launch latch. A permanent fence and a
separate finalizer must join original preflight, worker, model, HTTP, control and
late-child owners outside locks and outside the scope the finalizer joins.
Selected history/session reads, external controls and shutdown dependency order
still need that enrollment. No historical typed record can reconstruct lost
original ownership after restart.

That next increment is design only. Native Task settlement, active-slot release,
recurrence, inbox and editor remain subsequent gates. No model accuracy,
inference, device/GPU operation, paid call, live service/configuration or preview
change is part of this transport qualification. The full roadmap remains open.
