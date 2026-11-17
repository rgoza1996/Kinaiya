# Native live microphone and decoder ownership (#623)

Status: input/lifecycle increment under qualification. Adaptive Live remains
gated off in the normal launcher and Capture Mode controls. No browser Call,
Converse/Agent v2 consumer, audible backchannel, acoustic echo, physical-device
qualification, timing comparison or default rollout is claimed here.

Trusted Decisions embeddings can supply the actual `LiveDecisionsWorker` to
`services.decisions.api.create_app`. It must share the exact native model owner.
The app starts and stops its original Task separately from the existing loops.
Owner/Converse GET `/v2/decisions/live-readiness` reports the live Task, runtime
flags and read-only model metadata. It acquires no model lease and probes no
model/GPU/audio endpoint. Its version 2 readiness is separate from v1 readiness.

Trusted Converse embeddings can explicitly supply `RequiredLiveDecisions` to
`services.converse.api.create_app`. Its owned lifespan starts the bounded,
expiring monitor and fences live captures on unavailability or peer replacement.
It does not change legacy Converse availability or owner intent. Without that
explicit dependency the new input API is unavailable. It additionally requires
the configured native STT selection owner; v1's trusted host-only embedding path
cannot bypass native selection ownership in this new path.

Live input availability also requires the original selected owned-stream
protocol. The STT owner passively checks its unchanged private Whistle binding;
generic buffered transcribers, closed/replaced clients and buffered catalog STT
remain explicitly unavailable for live input. Inspection acquires no provider,
credential, lease or capture and probes no model. A selectable legacy/buffered
STT choice is not silently replaced to satisfy live input (#681).

## One logical microphone

`LiveMicCapture` is passive at construction. Its detached original registration
binds the Session backend/controller, Converse owner/capture and session/thread/
epoch. A positive exact native receipt precedes the STT lease and every source.
Original registration, fence and paired publication Tasks survive facade
cancellation and deadlines. Negative fencing can precede delayed registration;
a late registration acknowledgment cannot reopen permission.

Fencing synchronously revokes local admission and starts one original native
fence. Its reason and request identity do not change on repeated calls. Missing
acknowledgment remains unknown. Read-only exact lookup can establish the positive
original fence or publication, which is usable without replaying the failed
transport Task or sending another mutation. The fence is logical permission
metadata; it is not proof of microphone, decoder, HTTP, GPU or playback stop.

The stream rechecks its original native permission before each STT effect.
An authenticated native fence, replaced original controller or obsolete original
session/epoch closes that permission. This can be used without issuing a new
fence to an obsolete controller. A competing native fence cannot acknowledge or
replace the local original fence request. An empty lookup under the same current
backend/scope remains unknown and cannot prevent a valid first registration.

Recognized text uses the existing atomic source/capture writer. Source payload,
original request digest and annotation pair are checked together. Lost-ACK
recovery cannot accept a same-looking legacy source without its native pair.
Late valid recognized text can be retained after a positive native fence, while
its source remains ineligible for new live inference. No audio is persisted.

## Decoder rollover and actual cleanup

`LiveIncrementalStreams` owns one native STT selection lease for the entire
logical capture. Native Whistle decoder segments keep their existing v1 wire
protocol and bounds. A new segment requires a contiguous segment index and the
previous segment's positive final source, broker acknowledgment, actual HTTP
drain and lifetime completion. A decoder finish does not end the microphone or
the user's utterance. An explicit `capture_ended` hint remains independent.

Owner/capture, native registration and STT pin stay fixed across segments.
Ordinary v2 begin also retains the exact current utterance. Each segment starts
at the previous confirmed sample boundary; source
audio times use that absolute offset, with each unchanged SpeechWindow interval
bounded to 60 seconds. Window indices increase across segments, with same-slot
recognizer corrections retaining their existing revision behavior. The new
segment's full native hypothesis is retained as its own source; earlier segment
text remains qualified journal history visible to the existing Decisions context
selector. The v2 request digest includes capture and segment identity, while the
native Whistle request digest still describes exactly its unchanged v1 body.

The one conservative native crash admission spans the whole mic lease. Finishing
a segment cannot release or replace that lease. Owner-off, dependency loss,
disconnect, scope changes and shutdown fence the original capture and decoder.
The old HTTP close, pending publication and native cleanup remain owned. A
two-second close facade can return false while the original owners continue;
unknown work never donates its physical slot. Metadata reconciliation never
repeats audio or inference. Historical segment retention stores a cursor, not
links to an unbounded chain of prior stream objects.

## Explicit logical utterance boundary

The additive `native_live_utterance_v1` boundary starts a new logical utterance
within the same microphone/STT lifetime. Owner GET `next-utterance` declares
its separate schema-1.0 capability and current owner instance without starting
capture, inference or a provider probe. A client must check this and v2 input
availability under the same original owner before starting a supported Call.

Owner POST `next-utterance` uses the closed `LiveStreamNextUtterance` request:
the existing v2 begin fields, `boundary_protocol: native_live_utterance_v1`,
`previous_utterance_id` and `previous_finish_sha256`. The next segment must be
contiguous and reference the exact original positive final source. Its native
broker acknowledgment, pending publication and actual decoder/HTTP lifetime
must all settle first. An explicitly ended capture cannot restart here.

The original owner/capture, session/thread/epoch and disclosure scope cannot
change. The new utterance ID is `<capture_id>_<segment_index>`; a monotonic
segment plus the initial-ID guard prevents reuse without retaining an
unbounded name set or imposing a new utterance-count limit. Window indices
start at zero for this explicit domain; absolute audio offsets continue.
The mic registration and STT lease remain unchanged, and earlier source
permission/readback remains valid. This boundary grants no permission and
claims neither acoustic silence nor model/speaker completion.

Ordinary begin/PCM/finish formats and current-utterance scope guards remain
unchanged. The new operation returns the existing v2 begin-phase proof, whose
digest binds every boundary field. Uncertain outcomes use existing exact
begin-phase lookup/reconcile; a duplicate mutation cannot replay the decoder.
Pending work, stale identity or missing original final/cleanup refuses.

## Cancellation before the initial start arrives

The additive `native_live_abort_v1`, schema 1.0, binds a negative original to
the full initial v2 begin (segment zero) and its cancellation reason. Owner GET
`cancel-original` declares this separate protocol and owner instance. It grants
no input availability, provider selection or positive runtime admission.

Owner POST `cancel-original` enrolls its exact original before its first wait.
It fences the same native owner/capture and session/thread/epoch even if the
initial begin has not arrived. A negative-only owner is permanently fenced and
can be constructed while Converse is off, without registration or STT admission.
A known capture must match its immutable initial begin, including after an
explicit utterance boundary. A changed cancellation original refuses.

The native Session fence is persistent. A delayed registration cannot reopen
it after the bounded temporary Converse record retires. Owner POST
`cancel-original/lookup` reads metadata only; when that temporary record is gone,
it reads the original persisted native fence without reenrolling capture or
repeating a mutation. Backend/scope conflicts and unavailable metadata remain
unknown. Native receipt capacity is retained rather than erased to make space.

The proof repeats the entire negative original and its canonical digest.
`confirmed` requires a positive exact native fence; `cleanup_confirmed` further
requires the original native capture, decoder, metadata tasks and STT lease to
settle. An explicit pre-STT flag distinguishes a fenced registration failure
from an operation that entered STT. A timeout, missing registration or transport
failure proves neither closure nor cleanup. A cancelled HTTP facade cannot
cancel its retained native fence. Actual browser acquisition/stop, client HTTP
settlement, downstream inference and speech/playback remain separate obligations.
The existing identity-only cancel contract still requires an owned capture.

The frontend enrolls `LiveStreamApi.recoveryFor(initial)` before dispatching the
initial begin. This independent original connection survives normal-client
closure and exposes only exact cancellation and metadata lookup/reconcile.
It cannot dispatch PCM, start capture, finish a decoder or advance an utterance.
The Call owner must retain and close this channel through actual settlement;
closing either facade alone is not a native cancellation receipt.

## Native controller replacement and retirement (#685)

The separate additive `native_live_retire_v1`, schema 1.0, uses owner
`retire-original` and `retire-original/lookup` endpoints beside the unchanged
`cancel-original` protocol. Its capability must be inspected explicitly before
Call admission. It repeats the complete original initial begin and fixed reason;
it grants no positive capture, PCM, inference or replacement permission.

Retirement can confirm permanent native permission closure from an exact fence,
a changed native backend/controller, or an obsolete session scope. A missing
registration under the same current backend and scope remains unknown. The old
abort protocol still requires its explicit native fence. Neither protocol
equates logical closure with microphone, socket, STT, decoder or speaker cleanup.
Actual original cleanup and the explicit pre-STT flag remain separately checked.
A held registration Task can have closed permission while cleanup is unconfirmed.

The read-only Session `live-captures/recover` endpoint accepts the closed
`LiveCaptureRecoveryQuery` identity and session/thread/epoch. Converse and
Decisions can inspect it through their normal authenticated journal interfaces.
Its bounded `LiveCaptureRecoveryState` repeats that exact query and returns the
original persisted registration/fence and current native state, or null when
absent. Scope mismatch refuses. It starts no capture, claims no controller,
writes no ledger and fabricates no fence. This lets a retirement lookup recover
an old backend binding after a recoverable temporary Converse record retires.

An original whose failed registration has no durable native row remains pinned
in the bounded Converse registry. History pressure cannot erase its only cleanup
proof. The existing total 96 record ceiling still applies; exhaustion refuses
new work. Recoverable terminal records retain their 64-entry history target.
This qualifies controller replacement within a surviving Converse owner, not
the death/replacement of that service process or physical-device recovery.

## Owner HTTP contract

The explicit `converse_live_stream_v2`, schema 2.0, lives under
`/v2/converse/audio/live-stream`. Owner GET `ownership` returns the current owner
instance and input-path availability. Owner POST `begin`, `pcm`, `finish`,
`lookup`, `reconcile`, `cancel` and the additive `next-utterance` boundary use
separate closed, required-field models.
Construction and inspection start no capture. An unavailable mode has no silent
Rolling Windows, Legacy Silence, provider or model fallback.

The response retains the explicitly versioned native v1 transport proof inside
the v2 capture proof. Confirmation requires the original positive registration;
a recognized source additionally requires its exact atomic pair and complete
owner/capture/session/thread/epoch binding. Decoder completion, process release,
ownership retirement and native permission remain separate observations.

An original Session reset closes its previous epoch while retaining the native
registration. Exact lookup can still show that closed original. Session deletion
can remove the registration row while a decoder result remains retained. Its
positive projection then refuses with 409 instead of inventing a registration or
returning an invalid proof (#686). The separately enrolled retirement channel
can observe the obsolete scope and qualify actual original cleanup. A refused
lookup does not establish cleanup or permit positive replay. In-memory originals
with no durable row remain retained under the existing total bound.

Requests authenticate before bounded body acquisition and recheck credentials
after acquisition, before effects. Identity JSON rejects query/duplicate envelope
fields, duplicate JSON keys, nonfinite numbers, coerced counters and unknown
fields. Control bodies are at most 4 KiB; PCM bodies at most 96,000 bytes; body
collection has a five-second deadline. Responses are uncached and bounded to
448 KiB. Native PCM remains 1–16,000 contiguous samples per request, at most
960,000 samples per decoder, with one pending original at a time.

There are at most 32 active and 96 total logical captures, with a 64-entry
recoverable terminal-history target and the existing bounded stream history per
capture. Only permanently closed, actually retired captures with durable native
recovery metadata can be removed for historical capacity. Native journal, permission and
model-resource bounds remain independent. Exhaustion refuses admission rather
than erasing uncertain work.

The [qualification record](evidence/live-stream-capture-2026-11-16/README.md)
links requirement-to-implementation controls, exact
commands, unchanged input/schema pins and retained first failures. Synthetic
SQLite/ASGI/IPC/provider controls prove the checked architecture behavior only.
Model answer, classification, persona and speech quality remain diagnostic;
they are not acceptance gates and receive no overrides or rerolls.
