# Native media caller and publication ownership

Implementation contract for #379, with prerequisite composition under #204 and
#449. This revision adds an experimental native caller/worker and Session
publication owner. The first controlled Linux composition passed 113 cases at
`4182bb4`; final integrated-tree and architecture qualification is recorded in
the PR. The launcher does not install it, and general media start/resume remains
unavailable.

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

The first implemented lane accepts one sealed canonical PCM16 mono16k WAV of at
most one second through the original native spool reader. Converse chat and
direct SystemOne Decisions retain their own request/result projections. The
lane requires actual source/channel/anchor/authentication and protected native
model leases; it exposes no request flag or new public route. It does not
qualify physical capture, realtime/duplex speech, model quality, cloud liability reconciliation
or the complete news-video demonstration. Those #204 acceptance gates remain
separate. Unsupported composition remains explicitly unavailable.

`ConverseRuntime.complete_native_media` uses a separate protected host-media
capture method; ordinary text capture still requires a streaming provider.
`DecisionsWorker.complete_native_media` uses its independent selected model
lease and typed media classifier. Both call the original Session extension on
its owning event loop. This is a native in-process composition seam, not a
cross-service RPC or an owner/client request option. Catalog media selection,
native capability/readiness qualification, managed physical model ownership and
deployment composition remain #204/#449 prerequisites before production use.

The V2 pin envelope contains the full canonical bytes of the unchanged legacy
Converse/Decisions model pin. It does not widen the media integer-only graph or
the legacy cleanup union. Reconstructed refs/DTOs cannot replace retained
native task, model lease, source or bridge identity. Ordinary journal append
rejects the reserved `native_media_v2` marker; only the original guarded
publisher writes it in the Session transaction.

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
   Session repeats that guard after its synchronous mutation audit immediately
   before SQLite commit, since the original deadline can expire during the audit.
   A positively acknowledged rollback proves no local publication; a poisoned
   commit/rollback boundary remains unknown and retains its original claims.
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
Scheduling tests also hold the actual publication lock or pause the real SQL
commit/post-audit boundary; they do not replace the native owners or publisher.

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

## Bounded history and recovery

The optional owner reserves 64 MiB of lifetime metadata, accepts at most 64
original attempts and two unsettled attempts, and stores at most 32 bounded
immutable records/512 KiB of record bytes per attempt. It retains the original
worker before releasing its start gate, its original source reader, model
lease and one publication promise. Before-return exchange observations cannot
claim that the still-running original task or its publication has joined.
Post-task observations preserve that earlier unknown snapshot and record the
actual original join and reader close separately.

`native_media_v2_attempts` and `native_media_v2_records` are a private versioned
extension, separate from the unchanged legacy media DDL. Transactions audit
canonical requests, original owners, exact refs/digests, component proof sets
and publication identities. Unknown local close/commit/model-operation state
retains the source and operation capacity. Source stop/delete/shutdown fences
new consumption before waiting for those original owners; epoch reset does
not make their old publication current.

Restart marks unfinished originals unknown and refuses rearming. Missing or
partial native metadata also blocks reset/deletion when the original runtime
is absent. There is no automated native crash-reconciliation or refund API in
this increment. Preserve the private DB, journal, spool and original process
evidence for operator review; deleting an unknown row, opening a replacement
reader, or resubmitting under a new key is not recovery. General production
enablement remains gated on those broader lifecycle prerequisites.

## Exact PCM16 identity conversion on both architectures

#575 was reproduced on ARM by the unchanged actual-child multitrack test; its
independent postmux scalar oracle passed on x64. For float32 input with unchanged
rate and mono/stereo layout, the child now explicitly multiplies by 32768,
rounds to nearest with ties to even, and saturates to [-32768, 32767]. Nonfinite
input is refused. Planar/interleaved channel order, original sample counts and
PTS remain unchanged. The existing exact-byte test is retained without a
tolerance. This rule applies only to identity float32-to-PCM16 conversion;
selected rate/layout transformations and other formats still use pinned PyAV.

Upstream has separate architecture conversion paths in
[AArch64 conversion](https://github.com/FFmpeg/FFmpeg/blob/master/libswresample/aarch64/audio_convert_neon.S)
and [scalar conversion](https://github.com/FFmpeg/FFmpeg/blob/master/libswresample/audioconvert.c).
Different rounding is the source-based explanation under test, not an
instruction trace of the pinned wheel. The explicit rule matches the project's
existing independent oracle and is checked through the actual decoder process
on both hosts. Changed decoder code needs a freshly pinned implementation
closure before deployment; no live runtime/profile was changed by this PR.
