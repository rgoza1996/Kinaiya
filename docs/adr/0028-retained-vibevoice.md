# ADR 0028: Retained VibeVoice Realtime 0.5B CPU synthesis

Status: experimental implementation for [#69](https://github.com/rgoza1996/Kinaiya/issues/69), under M2 and Roadmap §§7.5, 14 and 25.2. This adapter adds the retained VibeVoice candidate; it does not replace Kokoro 82M or Supertonic 3. Ming 3 Tiny, the dependency-security follow-up #70 and full M2 acceptance remain open. The original evaluation environment has known advisories; it is not a supported patched production runtime.

## Runtime and trust boundary

The owner selects one allowlisted local backend. `VibeVoiceBackend(model_dir, voice)` supports `load()` and blocking `produce(text, emit, cancelled)`. It creates no producer thread. The worker owns the single native-generation thread, model lock, bounded PCM queue, cancellation event and sanitization of failures. Native `generate(audio_streamer=..., stop_check_fn=cancelled.is_set)` calls `emit(AudioWaveform(..., 24000))` as acoustic latents are decoded, before full generation returns. Callback backpressure blocks native generation; callback errors unwind it and are never translated into successful EOF.

This is model-native waveform streaming from a complete input string. The audited API tokenizes that string upfront and interleaves fixed text windows with acoustic generation; it does not accept an iterator of newly arriving LLM tokens. No service contract change or personality change follows from the model's marketing name.

The fixed reference profile uses CPU float32, SDPA, four intra-op threads, one inter-op thread, five DDPM steps, CFG 1.5, deterministic generation configuration and the public `en-Carter_man` preset. Deterministic sampling configuration is not a bitwise-reproducibility claim: diffusion still uses random noise. The runtime must match the checked CPU Torch, Transformers and upstream package versions, and its installed `vibevoice` Python files must match the pinned source hashes before import. Agent startup/health and backend construction import no Torch, Transformers, NumPy or VibeVoice.

Input is limited to 1,200 characters and the 8,192-token model context. Generation has an explicit text-token plus 900-acoustic-step budget (7.5 steps/sec), further restricted by remaining context. The callback independently refuses actual output beyond 120 seconds at 24 kHz. A max-step truncation is an error, not successful synthesis. The worker validates mono finite PCM, actual samples, rate consistency, transport frame size and cumulative duration before exposure.

Cancellation is cooperative. Native source checks the event between acoustic windows, and the callback also checks before forwarding each decoded chunk. In-flight Torch kernels are not preempted; no claim that receiving cancel proves native execution has stopped is permitted. The worker must wait for actual producer termination before starting another inference or use its verified process-unload recovery path. A late callback cannot authorize output from an obsolete Converse generation.

Upstream retains generated waveforms and concatenates them internally even with `return_speech=False`. A bounded worker queue therefore does not imply constant native memory. The sample/context caps bound this profile's generation; memory residency and cancellation latency need actual measurements. The implementation does not copy the upstream demo's unbounded queues, unmanaged thread or indefinite thread join.

## Exact offline assets

The [checked manifest](../../packages/providers/vibevoice_manifest.json) records all required regular files, sizes, SHA-256 digests and upstream provenance:

- Microsoft checkpoint `microsoft/VibeVoice-Realtime-0.5B` revision `6bce5f06044837fe6d2c5d7a71a84f0416bd57e4`: safetensors, configuration, processor configuration and model card. The safetensors is 2,035,332,888 bytes and matches its upstream LFS SHA-256.
- Runtime `microsoft/VibeVoice` revision `16fb2cb1217c9934a886e1948ffb06120caa2df5`; all 22 installed Python source files are verified. The current derivative distribution, `kinaiya-vibevoice-runtime==1.0.0+kinaiya.1`, preserves those source bytes while removing the upstream demo dependencies.
- Qwen tokenizer `Qwen/Qwen2.5-0.5B` revision `060db6499f32faf8b98477b0a26969ef7d8b9987`: tokenizer JSON/configuration, vocabulary, merges and Apache-2.0 notice. Separate Qwen model weights are unnecessary.
- Public `demo/voices/streaming_model/en-Carter_man.pt`, SHA-256 `a7bfdf1cd4939c22469bcfc6f427ae9c4467b3df46c2c14303a39c294cfc6897`, and the Microsoft runtime MIT notice.

Use an owner-controlled absolute model directory containing `checkpoint/`, `tokenizer/`, `voices/` and `licenses/` as recorded in the manifest. Keep it outside Agent-writable workspaces and prevent concurrent mutation. Verification uses no-follow descriptors for roots, nested directories and files and refuses missing, non-regular, wrong-size or wrong-hash files. Later library opens are not an immutable snapshot of the descriptors hashed earlier; owner protection remains necessary.

The adapter explicitly constructs the processor with the verified local Qwen tokenizer. Upstream's `from_pretrained` processor helper otherwise resolves a separate tokenizer repository and forwards `revision` to both repositories; the VibeVoice revision is not a Qwen revision. Model/tokenizer loading uses `local_files_only=True`, `trust_remote_code=False`, disabled Hub downloads and a safetensors-only checkpoint. The local pinned upstream package supplies the model implementation; this is not an unreviewed Hub Python-code load.

Preset loading verifies its hash first and uses `torch.load(..., map_location='cpu', weights_only=True)`. Torch 2.14 refuses the legacy preset's `SETITEMS` operation on a Transformers dictionary subclass. Two explicit serialized-name aliases therefore decode `BaseModelOutputWithPast` into an exact `OrderedDict` and `DynamicCache` into an inert local state container. The adapter checks all four fixed output records, mirrored attributes, token/layer counts, exact tensor shapes, bfloat16 CPU tensors and finite values before constructing the runtime cache/output objects. It never dispatches a method or class name from the decoded data. The original Torch 2.9 loader and this decoder produced identical canonical hashes for all 100 public-preset tensors and cache metadata. This narrowly scoped compatibility decoder is not a general untrusted-pickle importer or an operating-system sandbox.

Each request deep-copies the cached prompt before native generation mutates caches. Arbitrary voice files, Python modules and inference settings are not accepted from speech requests.

## Optional environment and licensing

`requirements-vibevoice.in` and its generated hash lock define an experimental Linux x86_64 / CPython 3.12 environment. Torch 2.14.1+cpu comes directly from the official CPU wheel index; Transformers remains 4.51.3, the upstream streaming-extra pin with unresolved advisories. Pillow is 13.0.0 and setuptools is 84.2.1. The lock also includes the voice API and faster-whisper 1.2.1 with its resolved dependencies. STT weights must already be installed locally; inference never installs them.

The standard-library-only [runtime wheel builder](../../scripts/build_vibevoice_runtime.py) verifies the 22 upstream sources and MIT notice through bounded no-follow reads. It executes no upstream build script. Its deterministic wheel contains the unchanged sources, license, minimal dependency metadata and a valid hashed `RECORD`; adjacent provenance records every source and the wheel digest. This removes Gradio, aiortc and other demo dependencies without hiding or fixing Transformers advisories. The current wheel SHA-256 is `beef94dace5211e8c9c46565b41cc3ea79746aea8ced9e34191ad83b812db097`.

With an owner-controlled checkout of the exact Microsoft revision and the verified license already present, a setup route from the Kinaiya repository is:

```sh
uv venv --python python3.12 /absolute/owner/voice-vibevoice
uv pip sync --python /absolute/owner/voice-vibevoice/bin/python --require-hashes requirements-vibevoice.lock
python scripts/build_vibevoice_runtime.py --source-dir /absolute/owner/microsoft-vibevoice --license-file /absolute/owner/assets/licenses/runtime-MIT.txt --output-dir /absolute/owner/runtime-wheel
uv pip install --python /absolute/owner/voice-vibevoice/bin/python --no-deps /absolute/owner/runtime-wheel/kinaiya_vibevoice_runtime-1.0.0+kinaiya.1-py3-none-any.whl
uv pip check --python /absolute/owner/voice-vibevoice/bin/python
```

The source directory contains `vibevoice/`; existing output files are refused. Inspect the builder's provenance and wheel digest before installation. Re-running `uv pip sync` removes the separately installed runtime wheel, which must then be installed again. Installation is an explicit owner action, never a model request. Base CI requires no optional runtime or weights. Invoke the probes from the repository as `python -m scripts.probe_retained_voice` and `python -m scripts.probe_retained_resources`, with their explicit environment/profile/model/output arguments.

Runtime code carries MIT. Checkpoint metadata declares MIT, but its model card additionally describes research-only intended use and exclusions using licensing language, and no standalone checkpoint LICENSE is supplied. Preserve those qualifications rather than calling the checkpoint unconditionally permissive. Presets ship in the MIT repository without a separate voice-specific provenance/license notice; independent voice-rights evidence was not established. Qwen tokenizer files carry Apache-2.0. Review these notices before use or redistribution.

CPU loading exists in the official demo; its SDPA quality/performance remains an evaluation obligation because upstream says only Flash Attention 2 was fully tested. ROCm is not enabled by this CPU profile. PyTorch's HIP compatibility is not proof of VibeVoice quality, latency or resource compatibility on AMD GPUs.

## Verification obligations

Dependency-free mocked tests cover absence of eager inference imports; source/artifact identity; tampering/symlink refusal; offline CPU/safetensors loading; restricted preset unpickling; prompt isolation; ordered callback output on the caller's thread; callback/native errors; cancellation; text/context/sample limits; and max-step truncation. These checks do not prove a successful model load or waveform generation.

The separate authorized live screen must record exact environment, cold load, warm first decoded/non-silent waveform, actual PCM samples, full RTF, CPU/RSS, disconnect-to-producer-stop time, stale-output fencing, subsequent-request recovery and verified process release. Report native first waveform separately from transport latency and physical audible onset. A small synthetic screen does not establish p95, listening quality, streaming underflow, loaded Converse/Decisions contention or the full M2 gate.

## Actual CPU screen and lifecycle proof

[Original recorded evidence](../../evaluation/evidence/vibevoice-cpu-2026-10-27.json) contains one new-process request and nine warm synthetic replies on the Ryzen 7 5800X with Torch 2.9.1+cpu and the original upstream distribution. The following table belongs to that preserved environment, not the newer lock. Filesystem/page caches were not flushed, host load was uncontrolled, and portions overlapped focused test work. This is a feasibility screen, not a controlled provider comparison or p95 qualification.

| Measurement | Observed result |
| --- | --- |
| Warm first decoded PCM | 0.258 s median; 0.237–0.674 s range |
| Warm first non-silent frame, 1% PCM amplitude threshold | 3.335 s median; 0.258–5.527 s range |
| Warm full synthesis RTF including HTTP | 3.457 median; 3.089–4.105 range |
| New-process first decoded/non-silent PCM | 7.798 s / 13.031 s |
| Observed process-lifetime peak RSS | 7,840,325,632 bytes |
| HTTP response close to observed producer-thread exit | 0.564 s, one cancellation observation |

All nine warm requests delivered decoded PCM while the native producer was still running. Early decoded frames often contain silence. This CPU profile is slower than realtime and does not establish uninterrupted playback. No physical microphone, speaker, acoustic onset, actual client underflow or human listening acceptance was measured.

Closing a long synthetic HTTP stream after its first chunk set the producer's cancellation event. Health subsequently confirmed zero live producers, completed cancellation and no quarantine; a fresh request succeeded. This observes native producer exit, not preemption of an in-flight Torch kernel or physical audio stop.

The generic [voice probe](../../scripts/probe_retained_voice.py) reproduces the screen with `--exercise-cancellation`. The [resource probe](../../scripts/probe_retained_resources.py) separately demonstrated a loaded worker around 5.90 GB RSS, verified owned process-group reaping, a new restored PID, fresh 24 kHz synthesis, old-speech HTTP 409 and durable owner-off. Its text provider and authenticated control transport are synthetic/in-process; no real Agent task or cloud inference was performed. Both owned workers and private temporary data were removed. Use an explicit measured restoration reserve; this screen used 8 GiB rather than relying on the general 2 GiB default.

Whisper tiny.en in the same optional environment transcribed one retained synthetic VibeVoice WAV as “I am here. What would you like to do?” through the actual transcription HTTP endpoint. This establishes a synthetic roundtrip, not microphone or human quality acceptance.

The original integrated Python suite passed 1,153 tests without a repository-wide `PYTHONPATH` override. An independent Codex review while that delegation was authorized ran 274 focused tests with no actionable findings. A genuine visible Muse Spark 1.3 Contributor/xhigh CLI static review of source `69d5322` also reported no findings and ran no tools/tests. Neither review covers later changes automatically or represents human approval or a passed dependency audit. The user's latest instruction permits both Codex subagents and visible Muse CLI Contributor subagents at max/xhigh; the shared Meta budget guard still applies.

## Minimal-runtime screen on 2026-10-28

[Separate evidence](../../evaluation/evidence/vibevoice-minimal-cpu-2026-10-28.json) records the newer 74-package environment at implementation `d67dafe`. All native source/model/tokenizer/preset identities remain pinned; only the explicitly documented package/dependency profile and restricted preset translation differ. Nine warm synthetic requests were measured before the regression tests started, with uncontrolled host load and unflushed filesystem caches. Diffusion output lengths vary, so these observations are not a controlled comparison with the original screen.

| Measurement | Observed result |
| --- | --- |
| Warm first decoded PCM | 0.227 s median; 0.217–0.227 s range |
| Warm first non-silent frame, 1% PCM amplitude threshold | 2.385 s median; 0.227–2.933 s range |
| Warm full synthesis RTF including HTTP | 2.627 median; 2.469–2.776 range |
| New-process first decoded/non-silent PCM | 6.885 s / 8.618 s |
| Observed process-lifetime peak RSS | 7,829,159,936 bytes |
| HTTP response close to observed producer-thread exit | 0.464 s, one cancellation observation |

All nine warm requests emitted PCM before producer exit. Cancellation ended the native producer without quarantine and a subsequent request succeeded. A second owned-worker proof verified actual process-group release, a new restored PID, fresh synthesis, stale-speech 409 and durable owner-off, using an explicit 8 GiB reserve. All owned processes and private temporary data were removed. Whisper tiny.en in the same new environment transcribed one public synthetic WAV correctly in 0.900 seconds through the HTTP endpoint. No physical audio, human listening, p95, GPU or loaded-stack acceptance was measured; the CPU profile remains slower than realtime.

The updated full Python 3.12 suite passed 1,193 tests; Ruff lint and formatting passed. A separate Python 3.11 run passed 144 focused backend/builder/Converse-cancellation tests. An earlier full run on that local standalone Python 3.11 failed 48 tests because its `fcntl` module lacks `F_ADD_SEALS`; worker bootstrap correctly refused to run without sealing. No fallback or test weakening was added. [Updated GitHub CI](https://github.com/rgoza1996/Kinaiya/actions/runs/40696389336) supplies those prerequisites: Python 3.11 and 3.12 each passed all 1,193 tests, sandbox checks, contract export and the base dependency audit; Flutter tests, analysis and the web build also passed. Base CI does not install the optional runtime or clear its advisories. The original CI run also exposed a real cancellation-monitor race and a first-frame test synchronization error; both were corrected before this screen.

An independent Codex review of changes through `d67dafe` reported no actionable findings, passed 164 focused tests, independently compared the 100 tensor-state records and verified all wheel source/license/RECORD digests. It did not rerun heavy inference or perform exploit testing. The genuine Muse CLI proposed the initial minimal wheel implementation; the lead applied, hardened and tested it. Neither that proposal nor the earlier static Muse review is a review of all later changes. Transformers advisory disposition and a supported patched port remain open in #70; this profile remains outside the running reference defaults.

## Primary sources

- [Pinned official runtime](https://github.com/microsoft/VibeVoice/tree/16fb2cb1217c9934a886e1948ffb06120caa2df5), including the generation loop, CPU demo, dependency declaration and MIT license.
- [Pinned checkpoint/card](https://huggingface.co/microsoft/VibeVoice-Realtime-0.5B/tree/6bce5f06044837fe6d2c5d7a71a84f0416bd57e4) and [upstream blob metadata](https://huggingface.co/api/models/microsoft/VibeVoice-Realtime-0.5B/revision/6bce5f06044837fe6d2c5d7a71a84f0416bd57e4?blobs=true).
- [Pinned tokenizer/license](https://huggingface.co/Qwen/Qwen2.5-0.5B/tree/060db6499f32faf8b98477b0a26969ef7d8b9987).
- [PyTorch HIP semantics](https://docs.pytorch.org/docs/main/notes/hip.html), relevant only to future separately measured GPU profiles.
- [PyTorch restricted serialization and serialized-name aliases](https://docs.pytorch.org/docs/2.14/notes/serialization.html), and the [official CPU release wheels](https://pytorch.org/get-started/previous-versions/).

## Dependency security gate remains open

The first installed-environment audit reported 70 advisory records across Pillow 12.3.0 (38), setuptools 80.9.0 (3), wheel 0.45.1 (1) and Transformers 4.51.3 (28). These are reported records, including overlapping identifiers, not a count of independently verified reachable defects. The original environment, lock and raw audit remain preserved; successful synthesis does not close [the dependency-security follow-up #70](https://github.com/rgoza1996/Kinaiya/issues/70).

A separate isolated compatibility investigation tried the then-current official PyPI releases Transformers 5.21.0, Pillow 13.0.0, setuptools 84.2.1 and wheel 0.48.0. All model/tokenizer/preset and 22 native-source hashes remained enforced; only the expected dependency version was explicitly overridden for the experiment. The unchanged Microsoft runtime failed during import: its `AutoModel.register` collides with a native Transformers VibeVoice acoustic-tokenizer registration. A probe-only registration override then exposed its import of removed `transformers.models.qwen2.tokenization_qwen2_fast`. The installed native Transformers implementations cover `vibevoice`, ASR and acoustic tokenizers, without this checkpoint's `vibevoice_streaming` configuration or `audio_streamer`/`stop_check_fn` waveform API. A native drop-in replacement was not established.

Transformers 4.57.6 with the three updated packaging/image dependencies successfully loaded the unchanged native source, emitted a public synthetic reply through the actual waveform callback and returned from cooperative cancellation after one chunk. This is compatibility evidence only: 4.57.6 still had 11 advisory records in official release metadata. The dependency override also conflicts with the upstream streaming extra's exact 4.51.3 pin and Gradio's Pillow `<13` constraint; that experimental environment is not promoted.

The subsequent minimal wheel and Torch 2.14.1+cpu lock above form a dependency-consistent 74-package environment. Its installed-environment audit still reports 28 Transformers records, representing 19 distinct PYSEC identifiers. It skips the derivative runtime and direct CPU Torch distribution; a separate official base-version lookup is not an audit of that binary. The audit is not green. The initial Torch 2.14 load refused the legacy preset; the restricted translation described above addresses that compatibility failure while retaining hash checks and `weights_only=True`.

The installed-environment audit also does not establish coverage of the direct CPU Torch binary or the source-only Microsoft distribution. A separate official PyPI lookup for base Torch 2.9.1 reported `PYSEC-2026-139`, `GHSA-rrmf-rvhw-rf47`, `GHSA-qfhq-4f3w-5fph` and `PYSEC-2026-2286`, with fixes listed through 2.13.0 and one record without a listed fix. This is base-version advisory metadata, not proof of exact `2.9.1+cpu` binary reachability or safety. The allowlisted local assets, safetensors-only checkpoint, restricted preset unpickling, bounded authenticated input and CPU-only profile limit the exposed interfaces; they do not justify dismissing these findings. Reachability review, a supported compatible patched runtime and renewed actual inference/resource checks remain required before closing the security gate.
