# ADR 0111: Retained Ming 3 Tiny Buffered GPU TTS Profile

Status: Proposed (offline adapter and pinned manifest contract for [#72](https://github.com/rgoza1996/Kinaiya/issues/72) under M2 ([#4](https://github.com/rgoza1996/Kinaiya/issues/4)); pending live GPU hardware and listening qualification before closing [#72](https://github.com/rgoza1996/Kinaiya/issues/72)).

## Context

Decision D23 retains four TTS profiles behind the local speech provider interface:

- `Kokoro 82M`
- `Supertonic 3`
- `VibeVoice Realtime 0.5B`
- `Ming 3 Tiny`

`Ming 3 Tiny` (`inclusionAI/Ming-3.0-tts-tiny`) is a buffered 44.1 kHz synthesis model (30 safetensors shards totaling 17,963,396,008 bytes, 8,981,004,468 stored parameters, 7.9B total / 1.3B active backbone) that requires an owner-provisioned local checkpoint directory and GPU (`cuda`) execution. Kinaiya's role is to provide a deterministic, fail-closed harness for the retained profile without implicit network downloads, remote code execution, or silent CPU fallback, while keeping [#72](https://github.com/rgoza1996/Kinaiya/issues/72) open until live GPU hardware and audio qualification gates are completed.

## Decision

1. **Pinned 30-Shard Checkpoint and Runtime Manifest (`packages/providers/ming3_manifest.json`)**:
   - Checkpoint repository: `https://huggingface.co/inclusionAI/Ming-3.0-tts-tiny` at commit `4715ef5a2f02901f75687d538b6920fd0dd11e6b` (30 safetensors shards `model-00001-of-00030.safetensors`..`model-00030-of-00030.safetensors` totaling 17,963,396,008 bytes, `model.safetensors.index.json`, `config.json`, `generation_config.json`, `tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`, `chat_template.jinja`, and `campplus.onnx`).
   - Runtime repository: `https://github.com/inclusionAI/Ming-3.0-tts` at commit `9332fb9443325d4726ce02cefd4ec1c211df4079` (`ming_tiny.MingTinyTTS`).
   - Provenance and notices: `LICENSE` (MIT, verified by SHA-256), `LEGAL.md` (verified by SHA-256), `campplus.onnx` (`Apache-2.0`), and `builtin_voice_rights_status: "unestablished_upstream"`.
   - All required files are opened with `O_NOFOLLOW` and verified against pinned byte sizes, SHA-256 digests, and `model.safetensors.index.json` shard membership before loading.
2. **Offline Local Loading Without `trust_remote_code=True`**:
   - `verify_ming3_assets` inspects `config.json` and rejects `trust_remote_code: true` or any unapproved `auto_map` entries.
   - `_default_ming3_loader` sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, invokes `ming_tiny.MingTinyTTS.from_pretrained(str(model_dir), device="cuda:0", dtype="bf16", moe_backend="loop", runtime_state_dtype="stable_fp32")`, and intercepts `AutoTokenizer.from_pretrained` so tokenizer loading routes through `PreTrainedTokenizerFast.from_pretrained(..., local_files_only=True, trust_remote_code=False)` on the verified local `tokenizer.json`.
3. **Explicit CUDA-Only Profile and Pre-Publication Residency Verification**:
   - `validate_tts_device("Ming 3 Tiny", device)` requires `device == "cuda"` and rejects `device == "cpu"`.
   - `Ming3TinyBackend.load()` validates CUDA residency (`cuda:0`) on a local candidate (`candidate.engine.model` / `candidate.model`) before publishing `self._runtime`, `self.loaded_device`, and `self.assets_state = "verified"`. If residency verification fails, the candidate is closed and never retained for subsequent requests.
4. **Preset Speaker Task (`ip`) and Conflict Validation**:
   - Default and allowlisted voice: `US_Emma` under `MingTinyTTS.generate(task="ip", text=..., ip="US_Emma")`.
   - Conflicting `emotion` or freeform `voice_description` parameters with the `ip` preset speaker task fail closed with `ValueError`.
5. **Bounded Buffered Output and Cancellation Fencing**:
   - Input text is bounded to 1,200 characters; output is mono 16-bit PCM at 44,100 Hz bounded by `MING3_MAX_SAMPLES` (60 seconds).
   - Unstopped outputs (`LingV3TTSGenerationOutput.stopped is False`, `truncated`, or `stopped_by_max_tokens`), non-finite samples, and all-zero silent outputs are rejected with `TTSBackendError`.
   - Caller cancellation is checked before inference, after inference, and before emission.
