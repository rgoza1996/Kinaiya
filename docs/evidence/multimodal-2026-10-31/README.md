# Multimodal observations — October 31, 2026

Public synthetic inputs only. **No physical camera, user recording or production
multimodal app path was exercised.** The CPU times are observations under local
development load, not a realtime latency qualification. Failed observations are
retained. See [ADR0083](../../adr/0083-synthetic-multimodal-qualification.md) and
[open qualification work](https://github.com/rgoza1996/Kinaiya/issues/204).

## Results

| Test | Observed result | Evidence |
| --- | --- | --- |
| LM Studio HTTP | Image question answered correctly. Audio/video parts were rejected by HTTP validation before inference. | [Transport baseline](lmstudio-transport.json) |
| LFM3 Omni 2B Q4_K_M | Image and three-beep audio probes passed. Documented ASR transcribed every word. The combined speech/image answer omitted the spreadsheet task; video counted two beeps; the generic frame replay chose the wrong direction. | [Generic](lfm3-generic.json), [documented profile and ASR bridge](lfm3-documented.json) |
| Gemma 4 12B QAT Q4_0 | Correct image color and frame direction. Audio counts were wrong/incomplete. Documented ASR misheard words; one combined answer invented three rows, even though it contained the requested spreadsheet/color facts. | [Documented profile](gemma4-documented.json) |
| d1 Omni 2B Q8_0 | Typed probabilities, zero generated output tokens. Beep/image route chose respond at 0.905. Speech-presence and routing probes were unreliable; low-confidence gates withheld Converse. The additional ASR bridge also stayed below threshold. | Local reports above, [four-destination probes](d1-routing-probes.json) |
| Muse Spark 1.3 Contributor | Correct color and speech transcription; understood the spreadsheet request with an image. Returned zero instead of three beeps in the initial audio probes. | [Both runs](muse-spark.json) |
| Muse Voice Transcribe API | Every spoken word matched after case/punctuation normalization. Beeps and silence produced empty transcripts. A separate run through the committed qualifier also matched. | [Three cases](muse-transcribe.json), [runner reproduction](meta-runner-repro.json) |
| SAM 3.1 image API and official parser | One image processed. Box was exactly `[15,65,66,116)` in half-open pixels. Decoded mask was 51×51 with all 2601 pixels foreground; zero parser diagnostics. | [API metadata](meta-media.json), [parser output](sam-parser.json), [opaque wire text](sam-output.txt) |
| Muse Image API | One inline image returned. Local visual inspection showed one red square centered on white. Requested model is recorded separately because the images response omitted resolved model identity. | [Metadata and image digest](meta-media.json) |

`expected_observed` is a small fixture-specific check, not a general semantic
evaluator. In particular, combined-answer checks look for required facts and do
not prove that additional claims are true; Gemma's invented rows are explicitly
recorded above. Exact ASR checks compare all words and normalize “three”/“3”.
The generic baseline predates that stricter ASR evaluator and has no speech case.
The raw model text is retained so these checks remain independently inspectable.

Neither a high probability on one fixture nor a selected class establishes
calibration. Four-destination probes include Agent work as an answer rather than
forcing all work requests into Converse. No Agent task was dispatched by them.

## Reproduce local probes

Use a disposable directory, Python with Pillow/httpx, ffmpeg with flite for the
optional speech fixture, and already-loaded explicit aliases. No model download
or server startup is hidden in these commands:

```sh
PYTHONPATH=. python scripts/qualify_local_multimodal.py --directory /tmp/kinaiya-omni-fixtures --include-speech
PYTHONPATH=. python scripts/qualify_local_multimodal.py --directory /tmp/kinaiya-omni-fixtures --include-speech --run-local --native-llama --base-url http://127.0.0.1:8815 --decisions-base-url http://127.0.0.1:8816 --prompt-profile lfm3
```

The test aliases are `kinaiya-qual-lfm3-omni` and `kinaiya-qual-d1-omni`.
Gemma used `--converse-model kinaiya-qual-gemma4-12b`, port 8817 and
`--prompt-profile gemma4`. Failed fixture checks return exit status 1; this is
expected for these recorded runs. Reports retain partial progress and distinguish
synthetic replay and model-pair preflight from production/hardware tests.

[Runtime evidence](runtime.json) pins the unmodified llama.cpp source, cached
GGUF/projector SHA-256 values, CPU execution, context, thread/output limits and
fixture tool versions. LM Studio CLI reported commit `43d4b26`. The isolated
server was needed to distinguish installed HTTP transport from model behavior;
the production services and user-loaded models were not replaced.

## Reproduce the buffered Meta check

The existing budget gateway must be running on port 9790 with its retained
ledger capped at at most $10. The default command only prepares public fixtures:

```sh
PYTHONPATH=. python scripts/qualify_meta_multimodal.py --directory /tmp/kinaiya-meta-fixtures
PYTHONPATH=. python scripts/qualify_meta_multimodal.py --directory /tmp/kinaiya-meta-fixtures --run-meta --cases voice-speech
```

Other explicit cases include `spark-image`, `spark-speech`, `spark-image-speech`,
`voice-beeps`, `sam-image` and `muse-image`. The last two require the gateway's
disabled-by-default media smoke flag. No automatic retry occurs. The official
[Meta-SAM parser](https://github.com/meta-models/meta-sam) at commit
`9348a8a791d3e5b6417232227e32e10464cdf49f` decoded the saved wire text using its
public Python API. It was run from an isolated source checkout, not installed
as a Kinaiya production dependency; no upstream source was modified or vendored.

## Cost and remaining checks

[Budget snapshot](budget-summary.json) includes earlier settled estimates and
uncertain reservations. The combined Meta ceiling was lowered to $10 without
resetting history. Media used a conservative $0.025 reservation/settlement total;
the published per-image prices imply $0.0125 for the two successful images.
Those are estimates, not invoices; reported billing was unavailable. No
OpenRouter requests were made in this qualification. Any earlier spend on a
different provider must also be reconciled before further paid tests there.
The [new-test calculation](test-cost-estimate.json), excluding other shared
callers, totals $0.026329 under the conservative admission rates, including
all five Spark calls, four buffered speech calls and the two images. Earlier
ledger history remains separate and was not reset.

The media smoke routes were returned to their default disabled state afterward;
the $10 ceiling remains. Realtime Muse endpointing, SAM video, actual camera
permission/stop/reconnect, owner-session multimodal transport and a held-out
quality/routing matrix remain open. The current result supports further work;
it does not justify enabling every modality or disabling STT for every Omni model.
