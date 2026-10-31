# ADR 0083: Synthetic multimodal qualification before enabling capture

Status: Qualification tools and observations implemented; production transport
and quality gates remain open in [#204](https://github.com/rgoza1996/Kinaiya/issues/204).
Part of [#196](https://github.com/rgoza1996/Kinaiya/issues/196).

## Requirement

The owner requested tests of Omni models together with Decisions, audio/video,
camera inputs, Muse Transcribe and Meta image/segmentation tools. They reiterated
the existing $10 cumulative cloud budget, including earlier spend. Existing
Converse, Decisions and Agent ownership and user-armed capture remain intact.

## Tools and boundaries

`scripts/qualify_local_multimodal.py` creates public synthetic image, moving
frames, three beeps and a three-second video. Optional ffmpeg/flite speech says:
“Please make a spreadsheet with three columns. The red square moves to the right.”
Inputs have SHA-256 manifests. Camera-frame replay uses these generated frames;
it never opens a physical camera. The tool never loads/unloads a model, changes
a production provider or starts Agent work. Inference requires `--run-local`,
explicit loaded aliases and loopback-only endpoints. Separate Decisions and
Converse servers are supported. HTTP responses, source media, JSON and typed
probabilities are bounded/validated. No automatic retry or cloud fallback exists.

Prompt profiles are explicit. The [LFM3 model card](https://huggingface.co/LiquidAI/LFM3-Omni-2B)
specifies an ASR system instruction; the [Gemma 4 card](https://huggingface.co/google/gemma-4-12B-it)
places image content before instructions and audio after them. Generic baseline
results remain separate from runs using those documented conventions.

The paired preflight sends media to typed Decisions, then calls Converse only
for a valid `respond` choice at confidence ≥0.8. An additional explicit ASR path
labels the recognized text as an untrusted hypothesis before classification.
These are model-pair probes, not production session/permission integration.
Neither choice nor confidence grants tool/capture authority. The threshold is
a fixed test gate, not a calibrated accuracy claim.

`scripts/qualify_meta_multimodal.py` defaults to fixture preparation. Reading the
protected existing Meta credential and making requests require `--run-meta`.
It only uses the fixed local admission gateway and first checks its bounded
numeric summary for a retained cap ≤$10. There are no user-media or remote-input
URL options. Model choices are fixed public synthetic cases. Standard buffered
voice, single-image SAM and Muse Image remain separate from Contributor text.
Generated image bytes are saved locally with a digest, never auto-executed;
remote output URLs are not fetched. Media smoke routes require a separate
explicit gateway flag and a $0.10 sublimit. See [budget semantics](../meta-budget-tests.md).

## Observations

The [dated evidence](../evidence/multimodal-2026-10-31/README.md) contains exact
synthetic question scripts, states, model/runtime hashes and failed observations.
LM Studio rejected some content types before inference; isolated unmodified
upstream llama.cpp accepted them. Image/audio transport acceptance did not imply
reliable speech interpretation, video counting or routing. LFM3's documented
ASR passed the one speech fixture; combined answers and d1 routing remained
mixed. Gemma's speech and combined answers also contained errors. The paired
gates correctly withheld low-confidence follow-up inference.

Muse Transcribe passed the recorded speech, beep and silence observations.
Spark transcribed speech but missed beep counts. SAM's official parser decoded
the expected still-image box/mask without diagnostics; Muse Image returned one
visually inspected square. These are single-fixture smoke results, not general
benchmarks. Physical camera, production multimodal transport, realtime Muse ASR
and SAM video remain untested by this slice. No production input capability was
enabled merely because a catalog declared it.

## Deterministic verification

Offline tests cover local endpoint/alias binding, media and response bounds,
malformed JSON/probabilities, manifests, documented input ordering, exact-word
ASR checks, incomplete/refused routes, fixed Meta endpoint/headers, explicit
opt-in, cumulative budget checks and absence of automatic retries. A regression
rejects a conversational response that merely contains ASR keywords.

The Options browser check also found the selected auxiliary purpose reset after
a successful save/refresh. The parent now retains that selection; a Flutter
remount test verifies the behavior. This presentation state never affects native
model-setting revisions or accepted task pins.
