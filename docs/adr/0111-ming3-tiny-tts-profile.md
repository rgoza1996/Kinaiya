# ADR 0111: Retained Ming 3 Tiny Buffered GPU TTS Profile

Status: implemented for [#72](https://github.com/rgoza1996/Kinaiya/issues/72) under M2 ([#4](https://github.com/rgoza1996/Kinaiya/issues/4)).

## Context

Decision D23 retains four TTS profiles behind the local speech provider interface:

- `Kokoro 82M`
- `Supertonic 3`
- `VibeVoice Realtime 0.5B`
- `Ming 3 Tiny`

`Ming 3 Tiny` (`inclusionAI/Ming-3.0-tts-tiny`) is a buffered 44.1 kHz synthesis model that requires an owner-provisioned local checkpoint directory and GPU (`cuda`) execution. Kinaiya's role is to provide a deterministic, fail-closed harness for the retained profile without implicit network downloads, remote code execution, or silent CPU fallback.

## Decision

1. **Pinned Checkpoint and Runtime Manifest (`packages/providers/ming3_manifest.json`)**:
   - Checkpoint repository: `https://huggingface.co/inclusionAI/Ming-3.0-tts-tiny` at commit `4715ef5a2f02901f75687d538b6920fd0dd11e6b`.
   - Runtime repository: `https://github.com/inclusionAI/Ming-omni-tts` at commit `9332fb9443325d4726ce02cefd4ec1c211df4079`.
   - License: `Apache-2.0` (`LICENSE` verified by SHA-256).
   - Required assets (`config.json`, `tokenizer.json`, `tokenizer_config.json`, `model.safetensors`, `preset_speakers/US_Emma.pt`) are opened with `O_NOFOLLOW` and verified against pinned byte sizes and SHA-256 digests before loading.
2. **No Remote Code Execution (`trust_remote_code=False`)**:
   - `verify_ming3_assets` inspects `config.json` and rejects any `auto_map` or `trust_remote_code: true` entries.
   - Runtime loading sets `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `local_files_only=True`, and `trust_remote_code=False`.
3. **Explicit CUDA-Only Profile and Residency Verification**:
   - `validate_tts_device("Ming 3 Tiny", device)` requires `device == "cuda"` and rejects `device == "cpu"`.
   - `Ming3TinyBackend.load()` verifies that model parameters and buffers reside on `cuda:0` and sets `loaded_device = "cuda:0"` and `assets_state = "verified"`.
4. **Preset Speaker Task (`ip`) and Conflict Validation**:
   - Default and allowlisted voice: `US_Emma` under task `ip`.
   - Conflicting `emotion` or freeform `voice_description` parameters with the `ip` preset speaker task fail closed with `ValueError`.
5. **Bounded Buffered Output and Cancellation Fencing**:
   - Input text is bounded to 1,200 characters; output is mono 16-bit PCM at 44,100 Hz bounded by `MING3_MAX_SAMPLES` (60 seconds).
   - Truncated outputs (`truncated` or `stopped_by_max_tokens`), non-finite samples, and all-zero silent outputs are rejected with `TTSBackendError`.
   - Caller cancellation is checked before inference, after inference, and before emission.
