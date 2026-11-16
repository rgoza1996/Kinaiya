# Optional contiguous browser PCM capture (#623)

Status: adapter foundation; Adaptive Live remains off. This optional interface is
not wired to Call, NativeASR, a scheduler, backchannels or a new runtime default.

The existing native capture AudioContext/AudioWorklet graph supplies mono16kHz
frames. Incremental capture refuses a different actual context rate. Conversion
stays in that continuous graph; snapshots are not independently resampled. The
bridge uses one960,000-sample Int16 allocation (1.92MB), matching the native
sixty-second stream bound. Each immutable packet contains at most16,000 samples
as canonical PCM16LE/base64, with capture epoch, sequence, contiguous sample
offset/count and a current speaking hint. There is no chunk list or snapshot
replacement policy in this FIFO.

`streamPcmConfigure` selects the optional adapter without acquiring a microphone.
`streamPcmState` and `streamPcmPeek` observe it. `streamPcmWait` retains one readiness
promise, waking on PCM, fault or local stop; it does not poll. `streamPcmAck`
requires the exact frozen packet identity and clears only its consumed samples.
Its last acknowledgment is idempotent. `streamPcmRelease` is a local buffer action
after the original microphone/acquisition/context-close barrier. Neither action
is proof of native ASR consumption, journal publication or native retirement.

Nonfinite/malformed frames, a gap, overflow, exhausted sequence identity or lost
capture stop local acquisition and preserve the original packet and buffered
input. The real original context close remains owned. A new frame cannot rewrite
a pending packet; an old acknowledgment cannot consume a new one. Epochs do not
alias across disposal. Playback and packet acknowledgment leave the armed
microphone and utterance intact. Incremental and the existing clip/window paths
are exclusive; there is no silent mode/provider fallback.

The optional `IncrementalPcmCaptureAudio` Dart interface exposes those operations
without changing other SpeechAudio implementations. Strict closed codecs check
integer/sequence/sample bounds, canonical base64, exact PCM length and its SHA256.
The web adapter handles both synchronous bridge returns and JavaScript promises.
Unsupported or malformed state refuses; it cannot manufacture a native receipt.

These checks use public synthetic WebAudio and pinned Flutter tooling. They do
not qualify a physical microphone, acoustic echo cancellation, ASR performance,
native publication or the complete Adaptive Live path. Native lifecycle recovery
is tracked separately in #654. Decoder-segment rollover, versioned Decisions,
completion scheduling, backchannel/mute ownership, stage timings and default
rollout remain under #623. Model answer quality is not an acceptance gate.

[Verification and first controls](evidence/incremental-pcm-capture-2026-11-16/README.md).
