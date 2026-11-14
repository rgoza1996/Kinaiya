# Native media caller and publication ownership

Implementation contract for #379, with prerequisite composition under #204 and
#449. This document records the planned integration. No native worker/caller is
implemented or enabled by this documentation-only revision.

## Existing boundary and required behavior

`NativeLlamaMediaProvider.complete_media` already rejects newly cancelled,
expired or stale returns after original HTTP cleanup and awaited exchange
settlement. Its `deliver_candidate` callback runs earlier, before that final
settlement. A native bridge must therefore stage that candidate privately;
receiving it is not permission to publish dialogue, routing or Agent work.

The existing #509 test uses a real Store but manually appends journal entries
and an Agent-dispatch list after a fixture task returns. It does not exercise
the production caller. The integration must replace that qualification gap
with observations of the actual native worker, publication owner and Agent
ingress. Existing provider regressions remain intact.

The initial implementation scope is the bounded local buffered-media path for
Converse chat and direct SystemOne Decisions. It does not qualify physical
capture, realtime/duplex speech, model quality, cloud liability reconciliation
or the complete news-video demonstration. Those #204 acceptance gates remain
separate. Unsupported composition remains explicitly unavailable.

## Component authority

- Session owns source/consumer identity, native grants, immutable provenance,
  charged resource/history state and the final authoritative publication guard.
- Converse owns its model request and character reply. Decisions owns typed
  classification and routing proposals. Neither model output nor a transport
  callback grants Agent permission.
- Agent retains its existing independent admission and execution authority.
  Imported media remains evidence-only; clip content cannot arm a command.
- The native exchange owner retains the actual original task, encoded-body and
  reader ownership, sole-close transport observation and publication work. It
  cannot manufacture a positive receipt from DTO fields or caller booleans.

Public refs identify records; they are not bearer credentials. Native operations
must resolve the exact original retained owner and authenticated incarnation,
not accept an independently reconstructed record with matching fields.

## Execution and publication ordering

1. Resolve current source/session/thread/epoch/fence/consumer/model/profile,
   disclosure, bounded input history and resource admission in trusted code.
   Register every promised original reader/publication dependency before use.
2. Retain one original worker and its cancellation baseline and absolute
   monotonic deadline. Observer cancellation fences further consumption before
   returning cancellation; cleanup continues under retained original ownership.
3. Construct the exact pinned provider request from admitted native data. Bind
   the actual local task, body and transport to one exchange registration before
   dispatch. No new request or replacement owner repairs an uncertain attempt.
4. Keep `deliver_candidate` private staging only. Await original transport/body
   cleanup and the provider's final return fence. An unknown physical component
   remains explicitly unknown and charged; a usable return does not settle it.
5. After each further native await, recheck original cancellation, deadline and
   source/model/consumer currentness. The actual publication commit must enforce
   its authoritative guard at the commit boundary. A separate earlier Boolean
   check or later caller check cannot protect an intervening append.
6. Observe actual publication/Agent admission and then reconcile the same
   original local owners. `LocalExchangeCleanupV2` may become settled only after
   its actual required task, transport, body, every reader and every publication
   have positively settled. Preserve prior unknown observations as history.

Cancellation racing after an already committed side effect cannot retroactively
erase that side effect. Retain its original receipt and reconcile its actual
outcome; never reinterpret a lost response as proof that nothing was published.
The negative acceptance cases arrange cancellation/expiration before the final
commit boundary and require that boundary to refuse publication.

Local exchange cleanup proves only its declared local scope. It cannot prove
model-worker/device/remote termination. Source stop/reset/delete first fences
new consumption, then joins all original local dependencies before releasing
their retained claims. Five-second observation limits report unknown; they do
not replace work or fabricate cleanup. Restart preserves unresolved original
incarnations and refuses rearming from missing records.

## Verification required before enabling composition

Use the actual production caller/worker, Session publication owner and Store,
and the real Agent admission boundary. Only the HTTPX transport/body/close is
controlled for failure injection; no synthetic exchange bridge supplies native
settlement and no hand-written publication harness substitutes for the caller.

For both modes, cover normal success, cancellation suppressed before response,
during body and during the original close, original deadline expiration in
those phases, stale/revoked source/consumer, and cancellation/expiry during a
held final native publication operation. Observe exactly one original close;
failed or unconfirmed close stays unknown with its retained dependencies.
Pre-existing cancellation counts are not new cancellation. Healthy positive
controls must actually publish the expected dialogue/routing and exercise the
appropriate permitted Agent path, proving the negative observers are connected.
An evidence-only input must never grant or dispatch a new Agent command.

Exercise stop/reset/delete/shutdown and exact replay while original cleanup or
publication is uncertain. Verify no replacement worker, renewed deadline,
invented receipt, duplicate publication/admission or premature capacity release.
Preserve existing immutable schemas and legacy behavior. Keep exact source,
commands, original failures and negative controls separate from later passing
results; run the relevant checks on both self-hosted architectures through the
guarded CI from #570. Actual device/model qualifications are not inferred.
