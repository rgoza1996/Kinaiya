# Incremental Whistle transcription

Status: native binding implemented, reviewed and exercised with the actual local
engine; managed transport/Converse/client integration pending. Tracks [#93](https://github.com/rgoza1996/Kinaiya/issues/93)
under #49/M2. No default promotion or full voice acceptance is claimed.

The existing live browser transcribes independent overlapping WAV snapshots.
Its latest-pending replacement policy works for those snapshots, but cannot feed
a recognizer that appends fresh samples. The pinned needle 3.3.3 engine exports
`needle_stream_transcribe_process` and `needle_stream_transcribe_stop`. We add
their explicit optional ctypes binding without importing the upstream resolver,
downloader or telemetry. Existing batch selection and pinned binary identities
remain unchanged.

## Native ownership and bounds

`WhistleEngine(..., enable_streaming=True)` enables the native seam only. It is
not yet a managed streaming provider selection. The trusted isolated child
serializes all calls to the process-global, non-thread-safe engine. There is no
native stream handle or documented speech abort.

- `begin_stream()` claims an idle logical stream without a native call.
- `stream_pcm(bytes)` appends fresh PCM16 little-endian, mono, 16 kHz input:
  1–16,000 samples per call and at most 960,000 samples per logical stream.
  Conversion uses signed values divided by 32,768. These are explicit supported
  bounds, not upstream throughput or maximum-recording claims.
- `finish_stream()` invokes the synchronous native stop exactly once after
  audio, validates its final output and returns to idle. Empty streams finish
  without calling a native stop. A fresh stream or batch operation may follow
  only confirmed normal completion.

Each update separates newly committed text, cumulative committed text and a
replacing provisional tail. The exact result fields are `delta_text`,
`committed_text`, `pending_text`, `received_samples`, `final`, `native_pass_ms`
and `language`. Native consensus and `final` do not establish recognition
accuracy, completed owner capture, conversational opening or action permission.

The fixed 128 KiB response buffer is never enlarged after dispatch. Strict JSON,
UTF-8, numeric, word-count/time, language, received-sample and aggregate text
bounds reject malformed native data. A native call is never replayed after an
uncertain result. Any exception from dispatch through state/result publication
permanently taints the engine and retains uncertain ownership. Further native
operations refuse; the process owner must terminate and confirm process/group
release. Stop/reset must not be used as an abort mechanism. The original
publication-interruption defect and its four-case reproduction are retained.

The existing sealed-input → zero file-size limit → explicit native load order
remains. Mandatory network isolation and address-space/CPU-affinity controls
belong to the trusted child launcher; constructing this low-level class does
not install those caller-owned controls.

## Integration still required

The next adapter must share one batch/incremental/retained-cleanup slot and bind
every append to the exact owned worker incarnation, capture scope, sequence,
contiguous sample offset, PCM digest and current owner/resource policy. It must
bound queues, deadlines, total input and acknowledgment recovery. Unknown
consumption cannot cause blind PCM replay or silent sample loss. Cancel,
disconnect, epoch reset, owner-off and handoff must fence publication before
exact process release; an old abort cannot stop a replacement worker.

Converse must persist each produced full committed-plus-pending hypothesis
before acknowledgment/classification. Changed hypotheses and meaningful
speaking-state transitions advance native window indices; same-slot revisions
remain actual corrections. PCM sequence, recognizer updates, transcript windows
and journal sequence are distinct counters. Retain ignored/superseded history
and existing source/permission checks. Decoder finalization never silently sets
`capture_ended` or commits an unfinished request.

The browser needs a separate contiguous PCM path with stateful conversion from
the actual capture rate and a bounded ordered FIFO. Its rolling snapshot drop
policy cannot be reused. Overflow or an unresolved gap must visibly stop that
capture. Existing microphone gestures, disposal, provider choices, independent
TTS and direct Agent operation remain applicable.

## Evidence and limits

At native commit `35e4ad58d303363837c143c1ef53668ea3574b0f`, 226 focused Whistle
and speech checks passed locally. Independent synthetic verification passed
86 matrix cases; the unchanged four-case publication-interruption probe passed
separately after repair. These use fake native calls and do not establish actual streaming,
latency, quality or process cancellation during a real C call. Full CI and the
managed transport/client remain separate gates.

[Muse CLI provenance](../../evaluation/evidence/incremental-whistle-muse-implementation-2026-10-28.json)
records the actual Contributor implementation, requested xhigh, reported usage,
original launch failure, Codex repairs and independent automated review. It is
not human approval. No model weights or native inference binary are added.

The [actual native-STT experiment](../../evaluation/evidence/incremental-whistle-native-2026-10-28.json)
passed its fixed four-stream acceptance in one run at the same native commit.
Five disjoint appends produced both committed and replacing pending speech;
normal flush returned the final sentence. A separate silent stream stayed
empty. Cancellation after dispatch, sampled process CPU advance and a pending
receive confirmed exact old-process/group release before a fresh child's silent
stream and final release. This observation does not instrument the exact C-call
entry. All twelve native dispatches, eleven returned updates, source pins and
private evidence are retained; there was no inference replay or fixture tuning.
The final transcript adds a comma after "proof": exact comparison fails while
word-normalized comparison passes. The recognized request was data only.

The five speech appends took 10.82–35.92 seconds each, totaling 130.23 seconds
for 4.7 seconds of input. The two-CPU profile does **not** sustain realtime
speech. Highest sampled VmHWM was 83.16 MiB; one process-metric KeyError sample
is retained, so the samples are observations rather than complete physical
accounting. Native loading took 76–81 ms. No physical hearing or integrated
Converse latency was measured, and the worker's twelve observed threads
are not a two-thread cap.

The next separately owned integrated Converse run must explicitly select local
STT, managed Qwen3-0.6B dialogue, local TTS and independent local Decisions.
Cloud dialogue remains an explicit opt-in. Muse Contributor is a development
coding subagent, separate from the selected Converse runtime model. Persistent
transport/client integration, sustained loaded voice, Decisions quality/examples,
full #49/M2 and the remaining roadmap remain open.
