# 0056: Explicit GPU voice inference for the small Converse profile

Issue #132 corrects a human-test launch that used the existing CPU adapters
despite the owner's 8 GB **VRAM** target. That target applies to the accessible
Converse stack, including speech recognition, dialogue and speech output;
loading CPU models is not evidence of meeting it. Decisions is measured
alongside the stack. Large local Agent models have separate requirements.

The installed Whistle engine is CPU-only, as described by its
[publisher](https://www.cactuscompute.com/blog/whistle). The GPU test therefore
selects cached OpenAI Whisper tiny.en rather than labeling Whistle as accelerated.
CPU Whistle and faster-whisper remain explicit compatibility selections.

## Voice selection and truthful readiness

The managed voice settings add `voice_device`, `voice_stt_backend` and
`voice_stt_checkpoint`. Existing explicit CPU configurations retain their behavior.
The GPU combination is `cuda` + `openai-whisper` + `Kokoro 82M`, with the absolute
path to the pinned local tiny/tiny.en checkpoint. CUDA is PyTorch's device spelling
for both NVIDIA CUDA and AMD ROCm. Unsupported combinations fail before worker
creation; missing GPU support or wrong model placement never selects CPU.

The local Whisper adapter hashes the same immutable official checkpoint bytes
that it loads with `weights_only=True`. It does not invoke a model downloader.
Uploads are decoded in memory with nested file/network protocols disabled,
bounded to 10 MB and 60 decoded seconds, and rejected rather than truncated.
Parameters and registered buffers must remain on the selected GPU before and
after inference. Kokoro explicitly receives the GPU device and verifies placement.
Phonemization, audio decoding and transport may use CPU; model inference uses GPU.

The installed ROCm runtime failed Kokoro's fused MIOpen LSTM with
`miopenStatusUnknownError` after successful GPU loading. Kokoro therefore uses
native Torch GPU operations on ROCm, disabling the cuDNN/MIOpen dispatch only
around iterator construction, each native advance, and cleanup. Caller flags are
restored before audio publication or backpressure. The successful diagnostic
observed all six LSTM input/output boundaries on `cuda:0`; this selects another
GPU kernel path, without moving model inference to CPU. NVIDIA and CPU selections
retain their existing dispatch behavior.

Worker health distinguishes the requested `device` from nullable actual
`stt_device` and `tts_device`. Unloaded models report null. An API-ready worker
with `loaded: []` does not establish GPU model readiness. The resource host checks
the selected backend and actual placement of loaded models. Existing locks,
bounded audio queues, policy checks, producer ownership, quarantine and process
release remain authoritative.

Relevant configuration fragment (alongside the normal separate Agent, Decisions,
session, permissions and origin settings):

```json
{
  "providers": {
    "converse": {
      "kind": "compatible",
      "base_url": "http://127.0.0.1:1234/v1",
      "model": "kinaiya-converse-gpu",
      "allow_cloud": false,
      "max_tokens": 128,
      "reasoning_effort": "none"
    }
  },
  "voice": {"url": "http://127.0.0.1:8765", "profile": "Kokoro 82M"},
  "resources": {
    "enabled": true,
    "voice_python": "/opt/kinaiya-rocm/bin/python",
    "voice_device": "cuda",
    "voice_stt_backend": "openai-whisper",
    "voice_stt_checkpoint": "/opt/kinaiya-models/whisper/tiny.en.pt",
    "stt_model": "tiny.en",
    "tts_voice": "af_heart"
  }
}
```

The separately loaded local Qwen3-0.6B Q8 instance requests full GPU offload,
GPU KV cache, context 2048 and one prediction slot. The compatible provider's
optional `reasoning_effort: "none"` uses the current LM Studio Chat Completions
[contract](https://lmstudio.ai/changelog/lmstudio), without changing conversation
messages or stripping reasoning from a failed response. Invalid efforts are
rejected; absent effort preserves previous wire behavior. Strict completion,
length, reasoning and cleanup checks are unchanged. An older standalone SDK
silently dropping an unknown thinking flag is retained as a failed probe,
not accepted as successful nonthinking inference.

Local LM Studio model residency is outside Kinaiya's managed native text worker.
Do not claim verified unload/handoff ownership for it; the existing unmanaged
local-text handoff refusal remains. Managed GPU text lifecycle work stays in #101.

## Acceptance limits

Offline regression tests cover configuration refusal, wrong placement, checkpoint
identity, bounded decoding and retained producer/owner controls. Actual local
GPU calls and device/VRAM observations are recorded separately in #132. File size,
requested offload settings and an API health response are insufficient to prove
combined VRAM fit. Measure residency and peak across the running Converse models,
including runtime allocations and concurrent work, separately from host RAM and
unrelated GPU workloads. Measurements on the development machine's 32 GB GPU
do not prove behavior on constrained 8 GB hardware, broad latency/quality,
physical microphone/echo handling or mobile calling.
