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

Owner/capture/utterance, native registration and STT pin stay fixed across
segments. Each segment starts at the previous confirmed sample boundary; source
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

## Owner HTTP contract

The explicit `converse_live_stream_v2`, schema 2.0, lives under
`/v2/converse/audio/live-stream`. Owner GET `ownership` returns the current owner
instance and input-path availability. Owner POST `begin`, `pcm`, `finish`,
`lookup`, `reconcile` and `cancel` use separate closed, required-field models.
Construction and inspection start no capture. An unavailable mode has no silent
Rolling Windows, Legacy Silence, provider or model fallback.

The response retains the explicitly versioned native v1 transport proof inside
the v2 capture proof. Confirmation requires the original positive registration;
a recognized source additionally requires its exact atomic pair and complete
owner/capture/session/thread/epoch binding. Decoder completion, process release,
ownership retirement and native permission remain separate observations.

Requests authenticate before bounded body acquisition and recheck credentials
after acquisition, before effects. Identity JSON rejects query/duplicate envelope
fields, duplicate JSON keys, nonfinite numbers, coerced counters and unknown
fields. Control bodies are at most 4 KiB; PCM bodies at most 96,000 bytes; body
collection has a five-second deadline. Responses are uncached and bounded to
448 KiB. Native PCM remains 1–16,000 contiguous samples per request, at most
960,000 samples per decoder, with one pending original at a time.

There are at most 32 active and 64 historical logical captures, with the existing
bounded stream history per capture. Only positively fenced and actually retired
captures can be removed for historical capacity. Native journal, permission and
model-resource bounds remain independent. Exhaustion refuses admission rather
than erasing uncertain work.

The [qualification record](evidence/live-stream-capture-2026-11-16/README.md)
links requirement-to-implementation controls, exact
commands, unchanged input/schema pins and retained first failures. Synthetic
SQLite/ASGI/IPC/provider controls prove the checked architecture behavior only.
Model answer, classification, persona and speech quality remain diagnostic;
they are not acceptance gates and receive no overrides or rerolls.
