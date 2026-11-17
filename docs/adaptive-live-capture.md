# Completion-driven Converse capture (#623)

Status: implementation in progress; Adaptive Live remains feature-gated off.
This does not change a runtime default or qualify the complete feature.

## Ownership and compatibility

Converse owns the host's persisted Capture Mode. Core retains shared provider,
credential, security and storage ownership; Decisions owns its independent
typed routing policies; Agent retains all execution/admission authority.
Capture Mode is independent of model/STT/TTS/provider selection.

The three selections are `adaptive_live` (Adaptive Live), `rolling_windows`
(Rolling Windows), and `legacy_silence` (Legacy Silence, Advanced/Compatibility).
Rolling Windows is the existing live snapshot path, not silence-ended capture.
End-silence controls apply only to Legacy Silence. No error or capability loss
selects another mode. Unsupported saved choices stay selected and unavailable.

A protected Converse settings owner persists either a pointer to host defaults
or an explicit choice. The authenticated owner API provides inspection, CAS save
and exact original-request lookup. Native backend/controller/revision identity,
bounded immutable receipts and read-only recovery follow the existing settings
discipline. Saving starts no capture, model, provider request or process.
Changes apply to the next capture; a receipt does not establish the current
physical microphone state. UI shows its actual current capture separately.

Existing implicit host behavior remains the migration default while the gate is
off: configured rolling capture stays Rolling Windows; the older clip path
stays Legacy Silence. Once the complete Adaptive Live path is qualified and its
rollout enabled, new configurations/sessions without an explicit saved choice
will resolve to Adaptive Live. Explicit choices survive restart and rollout.
An existing empty/corrupt store, missing initialized state, or changed open-file
identity refuses rather than resetting. Native backend identity scopes original
requests; a different backend cannot settle a previous backend's uncertainty.

## Incremental input and Decisions contract

Build on the existing Whistle begin/PCM/finish/cancel transport and authenticated
native transcript journal. Browser PCM uses stateful conversion, contiguous
sequence/sample offsets and a bounded FIFO. Snapshot replacement is not safe
for this stream: overflow, gaps or unknown consumption visibly stop capture.
Recognized hypotheses persist before classification/acknowledgment, including
ignored and superseded text. Retain full authorized input and output within the
declared bounds; no model-quality repair, reroll or silent model change applies.

Version the new capture/decision records separately from existing four-outcome
records. Add `WAIT_WITH_BACKCHANNEL`: keep listening and allow Converse to
produce an acknowledgment. Decisions does not supply its wording or mandate
speech. Preserve the independent Agent routing axis and native permissions.
An old four-outcome provider remains explicitly Rolling-Windows-compatible;
unsupported contracts must refuse rather than misparse or silently downgrade.

The [native live publication contract](live-window-publication.md) atomically
pairs the unchanged transcript with a separate v2 capture annotation and retains
the original eligible source across identical raw snapshots. It provides source
partitioning and positive-only original lookup; it does not enable the future
completion-driven worker or microphone path.

The [native live inference contract](live-decisions-attempts.md) binds charged
admission and exact result publication to that canonical source and original
capture permission. It retains raw/completed outcomes, rechecks all original
disclosure floors at publication, and preserves existing finalized-v1 behavior.
It supplies no scheduler, consumer or playback integration by itself.

The [completion-driven worker increment](live-decisions-worker.md) implements
bounded durable discovery, one actual inference slot, fresh-input coalescing,
session turns and exact-result recovery. It retains the original wake and cleanup
owners. Host/Call and consumer integration remain separate work; Adaptive Live
continues to report unavailable while its feature gate is off.

The optional [native Converse consumer](live-converse-consumption.md) joins the
native Agent consumer behind trusted embedding. Backchannel and full-reply
effects share the original Converse generation/speech owner and preserve the
armed capture. Normal host/Call integration and complete rollout remain open.

Only fresh eligible transcript or speaking/capture-state changes can admit new
Adaptive inference. Persisted duplicate hypotheses do not spin the worker.
Keep one in-flight inference per worker by default, coalesce pending work to the
latest eligible native revision, and immediately reconsider it after actual
inference completion. Do not cancel and replace an admitted remote call merely
because a newer hypothesis arrives. Recheck exact source/session/epoch/revision,
selection, disclosure and availability before dispatch and publication.

Backchannel playback must preserve the armed microphone and current utterance,
prevent self-transcription, and deduplicate by native source/action identity.
An acknowledgment is distinct from full reply, user interruption, Agent work,
completed capture and physical playback settlement. Off, reset, cancellation,
disconnect, slow STT and resource uncertainty retain original cleanup owners.

## Verification still required

Trace separate capture, STT, inference, publication, first PCM and actual playback
times; model completion never proves the user finished speaking. Qualify settings
persistence/default migration, explicit unsupported state, v1/v2 compatibility,
continuous speech/corrections, pending coalescing, stale results, duplicates,
backpressure, reconnect, echo/mute, owner-off, cancellation and native handoff.
Use faithful scripted inputs and exact call/source/output/timing evidence.
Actual model behavior and backchannel quality are diagnostic observations;
architectural correctness is the acceptance gate.
