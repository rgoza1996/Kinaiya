# ADR 0028: Retained VibeVoice Realtime 0.5B CPU synthesis

Status: implementation draft for [#69](https://github.com/rgoza1996/Kinaiya/issues/69), under M2 and Roadmap §§7.5, 14 and 25.2. This adapter adds the retained VibeVoice candidate; it does not replace Kokoro 82M or Supertonic 3. Ming 3 Tiny and full M2 acceptance remain open. Integration and live screening results must be recorded separately from this adapter's deterministic checks.

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
- Runtime `microsoft/VibeVoice` revision `16fb2cb1217c9934a886e1948ffb06120caa2df5`, package 1.0.0; all 22 installed Python source files are verified. The runtime source archive is also hash-pinned in the optional lock.
- Qwen tokenizer `Qwen/Qwen2.5-0.5B` revision `060db6499f32faf8b98477b0a26969ef7d8b9987`: tokenizer JSON/configuration, vocabulary, merges and Apache-2.0 notice. Separate Qwen model weights are unnecessary.
- Public `demo/voices/streaming_model/en-Carter_man.pt`, SHA-256 `a7bfdf1cd4939c22469bcfc6f427ae9c4467b3df46c2c14303a39c294cfc6897`, and the Microsoft runtime MIT notice.

Use an owner-controlled absolute model directory containing `checkpoint/`, `tokenizer/`, `voices/` and `licenses/` as recorded in the manifest. Keep it outside Agent-writable workspaces and prevent concurrent mutation. Verification uses no-follow descriptors for roots, nested directories and files and refuses missing, non-regular, wrong-size or wrong-hash files. Later library opens are not an immutable snapshot of the descriptors hashed earlier; owner protection remains necessary.

The adapter explicitly constructs the processor with the verified local Qwen tokenizer. Upstream's `from_pretrained` processor helper otherwise resolves a separate tokenizer repository and forwards `revision` to both repositories; the VibeVoice revision is not a Qwen revision. Model/tokenizer loading uses `local_files_only=True`, `trust_remote_code=False`, disabled Hub downloads and a safetensors-only checkpoint. The local pinned upstream package supplies the model implementation; this is not an unreviewed Hub Python-code load.

Preset loading verifies its hash first and uses `torch.load(..., map_location='cpu', weights_only=True)` with only the two official required classes, `BaseModelOutputWithPast` and `DynamicCache`, allowlisted. Each request deep-copies the cached prompt before native generation mutates caches. Arbitrary voice files, Python modules and inference settings are not accepted from speech requests.

## Optional environment and licensing

`requirements-vibevoice.in` and its generated hash lock define a Linux x86_64 / CPython 3.12 reference environment. Torch 2.9.1+cpu comes directly from the official CPU wheel index; Transformers is 4.51.3, the upstream streaming-extra pin. The lock also includes the voice API and faster-whisper 1.2.1 with its resolved dependencies. STT weights must already be installed locally; inference never installs them. Source archives and all runtime dependencies are hashed. The broad dependencies declared by the upstream package include its demo UI/network libraries; their presence grants no runtime network authority.

One reproducible setup route uses `uv venv --python python3.12 /absolute/owner/voice-vibevoice`, then `uv pip sync --python /absolute/owner/voice-vibevoice/bin/python --require-hashes --build-constraints requirements-vibevoice.lock requirements-vibevoice.lock`. The build constraints also pin setuptools/wheel for the upstream source package. Installation is an explicit owner action, never a model request. Base CI requires no optional runtime or weights.

Runtime code carries MIT. Checkpoint metadata declares MIT, but its model card additionally describes research-only intended use and exclusions using licensing language, and no standalone checkpoint LICENSE is supplied. Preserve those qualifications rather than calling the checkpoint unconditionally permissive. Presets ship in the MIT repository without a separate voice-specific provenance/license notice; independent voice-rights evidence was not established. Qwen tokenizer files carry Apache-2.0. Review these notices before use or redistribution.

CPU loading exists in the official demo; its SDPA quality/performance remains an evaluation obligation because upstream says only Flash Attention 2 was fully tested. ROCm is not enabled by this CPU profile. PyTorch's HIP compatibility is not proof of VibeVoice quality, latency or resource compatibility on AMD GPUs.

## Verification obligations

Dependency-free mocked tests cover absence of eager inference imports; source/artifact identity; tampering/symlink refusal; offline CPU/safetensors loading; restricted preset unpickling; prompt isolation; ordered callback output on the caller's thread; callback/native errors; cancellation; text/context/sample limits; and max-step truncation. These checks do not prove a successful model load or waveform generation.

The separate authorized live screen must record exact environment, cold load, warm first decoded/non-silent waveform, actual PCM samples, full RTF, CPU/RSS, disconnect-to-producer-stop time, stale-output fencing, subsequent-request recovery and verified process release. Report native first waveform separately from transport latency and physical audible onset. A small synthetic screen does not establish p95, listening quality, streaming underflow, loaded Converse/Decisions contention or the full M2 gate.

## Primary sources

- [Pinned official runtime](https://github.com/microsoft/VibeVoice/tree/16fb2cb1217c9934a886e1948ffb06120caa2df5), including the generation loop, CPU demo, dependency declaration and MIT license.
- [Pinned checkpoint/card](https://huggingface.co/microsoft/VibeVoice-Realtime-0.5B/tree/6bce5f06044837fe6d2c5d7a71a84f0416bd57e4) and [upstream blob metadata](https://huggingface.co/api/models/microsoft/VibeVoice-Realtime-0.5B/revision/6bce5f06044837fe6d2c5d7a71a84f0416bd57e4?blobs=true).
- [Pinned tokenizer/license](https://huggingface.co/Qwen/Qwen2.5-0.5B/tree/060db6499f32faf8b98477b0a26969ef7d8b9987).
- [PyTorch HIP semantics](https://docs.pytorch.org/docs/main/notes/hip.html), relevant only to future separately measured GPU profiles.
