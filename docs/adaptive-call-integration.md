# Adaptive Live host and Call integration (#623)

Status: implementation in progress. The normal release gate remains false, so
ordinary Call still reports Adaptive Live unavailable. Trusted qualification
embeddings can exercise the gated Call owner described below. Complete playback,
lifecycle, timing, migration and rollout qualification remains open.

## Native host ownership

The trusted launcher assembles one `LiveConverseController` when the qualified
release gate is enabled. It retains the existing runtime's personality, model,
context, speech and cleanup owners. Its original API lifespan starts the live
consumer and required v2 Decisions monitor; the launcher preserves its existing
resource/model/journal cleanup and does not start a second window loop.

Rolling Windows remains independently selectable. Disabling its host capability
does not disable the native live consumer or advertise v1 window support.
The corresponding Decisions factory shares the original journal, Agent client
and model settings with the native live inference and Agent consumers. The
existing finalized-turn and optional rolling workers retain their own loops.
Readiness inspection starts no inference, capture or provider probe.

The trusted rollout value is an exact boolean. While false, implicit defaults
retain existing behavior. When the complete feature is qualified and the gate
is enabled, an implicit host pointer resolves to Adaptive Live; explicit saved
and configured choices remain unchanged. Preference revision and next-capture
semantics remain intact. An unsupported provider/input stays visibly unavailable.

## Frontend native stream contract

`KinaiyaClient.liveStreams` owns its independent authenticated, bounded transport.
It implements the existing `converse_live_stream_v2` ownership, begin, PCM,
finish, lookup, reconcile and cancel endpoints. The normal and labeled mock
constructors require the appropriate independent factory when using injected
transports. Inspection and construction acquire no microphone or inference.

An original operation detaches its complete request and canonical UTF-8 digest.
The client checks original phase, owner/capture, decoder, scope, counters,
registration and paired transcript/capture publication. Lookup and reconcile
send only exact metadata, never PCM. A retained publication beside an unknown
transport result does not become an audio acknowledgment. Request/response
bounds are 96,000 bytes/448 KiB; control operations retain their 4 KiB bound.
Duplicate keys, unsafe/fractional counters and malformed UTF-8 are refused.

The existing transport retains actual sends, subscriptions and cleanup beyond
facade deadlines and client replacement. A timeout remains unconfirmed; it
does not free actual capacity or automatically resend the original operation.

The [negative-original protocol](live-stream-capture.md#cancellation-before-the-initial-start-arrives)
also supports a delayed initial begin after cancellation. A separately enrolled
original recovery channel remains usable after normal-client closure and has
no positive capture/audio methods. Its native persistent fence, exact readback
and actual native cleanup are separate from browser and client HTTP settlement.
The gated Call owner retains and joins these obligations through stop and
disconnect; complete replacement qualification remains open.
The [negative-original qualification](evidence/live-stream-negative-original-2026-11-17/README.md)
retains native/actual-wire checks, original failures and source-bound frontend
and release-build evidence.

Decoder rollover requires the exact confirmed final original, positive paired
source, decoder completion and actual decoder/HTTP ownership retirement.
Process release is separate: the same logical microphone deliberately retains
its native STT lease across decoder segments. A begin/PCM proof or a model
completion cannot establish this final-source/cleanup boundary.

The optional `SegmentedPcmCaptureAudio` adds a local
`browser_pcm_segment_v1` rollover original. It binds `capture_epoch`,
`next_sequence` and the acknowledged `sample_offset`; its receipt repeats that
exact boundary and a strictly newer local capture epoch. The caller must first
qualify native finish and cleanup separately. Rollover preserves the speaking
hint, armed microphone and every buffered sample after the acknowledged prefix.
It resets only local decoder counters, inside the same fixed 960,000-sample
allocation. Exact readback is idempotent; old-segment acknowledgments cannot
consume new samples. Pending packets, faults, stale/corrupt originals and mic
cleanup refuse. This does not grant native permission or mark speech ended.

The [explicit native utterance boundary](live-stream-capture.md#explicit-logical-utterance-boundary)
preserves the same mic/STT lifetime for successive logical utterances. The
frontend checks its separately versioned capability and uses an original built
from the exact confirmed, actually retired final. Existing v2 begin retains its
current-utterance guard. Backchannels do not request this boundary, and model
completion does not establish an acoustic end. The gated Call owner joins this
operation to its original capture lifetime; complete playback qualification
remains open.
Its [qualification record](evidence/native-utterance-boundary-2026-11-17/README.md)
also retains the #681 availability defect, original failing controls and local
repair evidence.

The gated Call additionally requires the separate `native_live_retire_v1`
capability on the same native owner before microphone acquisition. It uses that
original negative channel for stop and metadata readback, preserving the old
`native_live_abort_v1` endpoints for existing clients. Exact native controller or
scope replacement can close permission without inventing an explicit fence;
actual registration/decoder/HTTP/STT and local microphone/speaker cleanup remain
separate. A pending original registration cannot donate cleanup capacity.
Passive native identity recovery preserves an old persisted backend binding
after temporary Converse history retires. Originals with no durable native row
retain their sole proof within the existing total registry bound; history
pressure cannot erase them. See [native retirement](live-stream-capture.md#native-controller-replacement-and-retirement-685).

The [qualification record](evidence/adaptive-call-integration-2026-11-17/README.md)
retains commands, unchanged input pins, first failures and repairs. Actual
Dart transport runs against fresh native HTTP, SQLite and synthetic Whistle IPC
owners, using the pinned existing Flutter dependency cache with `--no-pub`.
It verifies two decoder starts, one PCM submission, one finish and one
cancellation, plus metadata-only observation and one retained STT lease.

The [local rollover controls](evidence/pcm-segment-rollover-2026-11-17/README.md)
separately exercise two minutes of synthetic browser input without opening a
replacement microphone, actual bridge-to-Dart receipt/PCM parity, and the
release web compiler. These are local protocol controls, not a physical-device
or complete Call qualification.

## Gated Call ownership

`AdaptiveLiveCapture` owns one explicitly armed incremental microphone and its
original native capture. Call requires segmented PCM and fresh speaking-hint
capabilities, separately inspects the native stream and utterance protocols, and
enrolls its independent cancellation channel before dispatching the first begin.
Unsupported input remains unavailable. Nothing silently selects another mode.

Fresh browser notifications drive contiguous PCM sends. Frozen packet hints and
bytes remain unchanged until an exact native positive proof and actual HTTP
settlement permit acknowledgment. Fresh speaking state is inspected separately.
At half of the declared sixty-second FIFO, a segment finishes without claiming
the microphone or user speech ended. Exact final-source and decoder retirement
precede local rotation, preserving buffered tail input and the same mic/STT lease.
Boundary-only proof fields stay with their original next-utterance request and
are not copied into subsequent PCM, finish or ordinary decoder starts.

A backchannel preserves the current logical utterance. Provider completion
(`live_output_observed`) also preserves it: a noninterrupting reply can still be
buffered while the user speaks. A separate authenticated `live_no_audio_ready`
state is emitted only for a full reply with no published chunks, a reclaimable
original producer, and current native source authority. That fact can request
the next logical utterance even when playback is unavailable. A reply with audio
uses its first actual queued PCM boundary and existing native source/effect/
generation authorization checks. Neither boundary infers an acoustic end or
judges model quality.

An independent Session reader uses `journal_wake_v1` to promptly recheck bounded
sixteen-entry pages scoped to the original session, thread and epoch. It starts
after the native capture registration sequence. Wake revision is a passive hint,
never a transcript sequence or speech permission. The journal codec preserves
finite decimal payload metadata and Unicode, rejects duplicate keys, malformed
UTF-8 and unsafe identity numbers, and requires integer wire spelling for cursor,
entry and playback counters. Limits are 2 MiB, 32 nesting levels and 65,536 JSON
values. Existing settings codecs and legacy polling retain their prior behavior.
Closing a wake body before dispatch refuses without enrolling a waiter (#682).

An independent Call observer looks up the current decoder's exact native capture
permission through the enrolled metadata channel. It owns one read at a time
and a cancellable nominal one-second control heartbeat. Speech delivery or an
unresolved speaker stop cannot block that observer. A native permission fence
needs no transcript entry or fresh PCM. Closed, missing or unavailable permission
fences new local input and requests microphone/speaker stop plus original native
cancellation/readback. The
lookup grants no PCM acknowledgment, inference or replacement capture; PCM and
model work do not wait for its heartbeat. Stop cancels the timer/wakes the
observer synchronously. Its lifetime and actual HTTP reads join the retained
cleanup obligations, including when a facade fails. This is not a physical or
provider latency guarantee.

Stop fences new admission synchronously. Native cancellation/readback, original
HTTP sends, decoder cleanup, browser acquisition/stop, local FIFO release and
passive reader cleanup remain separate obligations. A public deadline or parent
client disconnect cannot release the actual capture slot. Late local packets
are not sent after that fence. A refused rotation can retire only after actual
native cleanup and stopped FIFO readback matching its original epoch, sequence
and acknowledged offset. A changed local epoch still requires the exact retained
rotation receipt. Uncertain cleanup remains retained.

The first current native live output enrolls its bounded runtime identity for
this capture. A `delivery.updated` negative playback fence is accepted only with
that same runtime, session, thread and epoch, exact native `generation_stop`,
`interrupted` and `delivery_unknown` metadata, and safe monotonically increasing
generation counters. Any source must match its sole cause. Audible delivery
receipt fields, model text and another runtime cannot grant stop authority.
The existing local generation gate stops playback without closing the armed
microphone; duplicate or older generations cannot stop newer audio.

Call stop also retains the original local speaker-stop Future. Native input
cleanup alone cannot release the FIFO or Call capacity while that Future is
unresolved. Adapter failure leaves cleanup unconfirmed. Synthesis EOF, local
source completion, native capture cleanup and local speaker cleanup are separate
facts; only actual local source completion can support a played receipt.

The retained Call registry outlives widget and ordinary client replacement.
Every Call mode and one-shot recording recheck it before microphone acquisition,
including after asynchronous setup. A new surface cannot treat a retained Call
as idle for a new conversation. Registry notifications refresh presentation;
they grant no admission or cleanup authority. The same original must actually
retire before a replacement surface enables new capture. Replacing the ordinary
client preserves the separately enrolled native cancellation channel; Stop
readback does not replay positive audio or enroll another cancellation original.

Actual native controls in `test_live_stream_dart.py` run the Call widget against
fresh HTTP, SQLite and synthetic Whistle IPC. They cover contiguous input, held
physical-stop futures through disconnect, late PCM exclusion, empty backchannels,
no-audio full-reply boundaries, refused rotation and lost rotation receipts.
They also verify that provider completion of a buffered noninterrupting reply
does not dispatch finish or start a new utterance.
These are synthetic architecture/lifecycle controls, not physical audio or
provider latency measurements. See the [Call ownership qualification record](evidence/adaptive-call-owner-2026-11-17/README.md).

The [playback qualification record](evidence/adaptive-call-playback-2026-11-17/README.md)
adds actual native speech/delivery/interrupt HTTP controls with synthetic PCM
from the legacy-local TTS adapter. It checks backchannel capture continuity,
natural playback completion, native generation stops, and Mute with large text
and reduced motion. A held speaker-stop Future must retain Call after native
microphone cleanup and the public deadline. Browser controls separately show
that speaker PCM does not enter or wake the input FIFO and that the bridge
requests echo cancellation and noise suppression. These controls do not qualify
a managed TTS worker, physical playback or room/device echo cancellation.

The [replacement qualification record](evidence/adaptive-call-replacement-2026-11-17/README.md)
retains the first enabled-control and actual second-acquisition failures, then
tests disposal with a replacement Legacy Silence surface and client replacement
inside the same mounted widget. Both retain the original speaker/FIFO through
native input cleanup and public deadlines. The replacement Legacy Silence Call
can acquire only after that same original retires. These controls do not qualify
native owner/process replacement, every reconnect failure or hardware cleanup.

The [dependency-loss qualification record](evidence/adaptive-call-dependency-loss-2026-11-17/README.md)
stops the real native live Decisions worker and lets its original required-service
monitor observe the actual readiness endpoint. It covers a backchannel followed
by quiet input and an armed capture that receives no PCM at all. Local cleanup
must complete without another packet or bridge event. A subsequent explicit Call
observes unavailable capability without rearming or selecting another mode.
Legacy Converse availability remains separate from this v2 dependency fence.
That record qualifies the earlier journal-coupled observer. The
[independent permission-observer record](evidence/adaptive-call-permission-observer-2026-11-17/README.md)
adds a forced ordering: a real positive lookup response is delayed until native
capture closure and generation-stop publication, then released unchanged while
the local speaker stop is held. Mic revocation must proceed independently;
speaker/FIFO/Call ownership remains retained until the same original settles.

## Remaining integration

Extend the synthetic playback/backchannel, mute and accessibility qualification
to the remaining lifecycle and supported-adapter paths. Complete no-audio,
reconnect, process replacement and dependency-loss qualification; physical
echo prevention remains unverified. Add timing
instrumentation and faithful same-conversation mode comparisons before enabling
the default. Capture/STT/model/playback observations remain distinct. Model
answer, classification, persona and speech quality are diagnostic evidence;
architecture and harness correctness determine acceptance.
