# ADR 0027: Retained Supertonic 3 CPU synthesis

Status: experimental implementation for [#67](https://github.com/rgoza1996/Kinaiya/issues/67), under [M2/#4](https://github.com/rgoza1996/Kinaiya/issues/4), Roadmap §§7.5, 14 and 25.2. Kokoro 82M and Supertonic 3 synthesize locally; Ming 3 Tiny and VibeVoice Realtime 0.5B remain retained, unconfigured candidates. Full M2 acceptance remains open.

## Backend and trust boundary

The existing independent voice process selects one allowlisted implementation. Agent startup, health checks and worker construction import no optional inference runtime. Converse sends the configured profile through the existing loopback-only, authenticated `SpeechClient`; redirects and ambient proxies remain disabled. No inference request selects a model directory, runtime module, voice file or arbitrary process arguments.

Supertonic uses SDK 1.3.1 and ONNX Runtime 1.23.1, verified at load, with four CPU-only sessions, four intra-op threads and one inter-op thread. Eight synthesis steps, speed 1.05, English and preset F1 are the initial profile. F1–F5 and M1–M5 are supported configured presets. The current Kokoro profile uses af_heart. These choices do not change Converse personality.

The upstream SDK returns the whole waveform. Supertonic metadata therefore says `synthesis_mode: buffered`; Kokoro says `sentence_buffered`. Dividing completed waveforms into at most one-second transport frames does not establish model-native streaming. Frames preserve the actual sample rate (44,100 Hz for Supertonic; 24,000 Hz for Kokoro), mono PCM16 little-endian samples, order and sample count. Invalid shapes, non-finite samples and output exceeding 120 seconds are refused. Supertonic input above 1,200 characters is refused before inference; the existing Kokoro text bound remains 16,000.

Resource policy is rechecked after lock waits, after model load/inference and before output. A disconnected request fences subsequent work and output when cancellation is observed; synchronous native inference already running may finish first. Converse still validates its own event/generation before synthesis and before forwarding each frame. The backend does not author delivery acknowledgments or authorize Agent work.

## Offline configuration

Install deliberately into a separate environment, using the hash-pinned Linux/Python 3.12 reference lock:

```bash
python3.12 -m venv /absolute/owner/voice-supertonic
/absolute/owner/voice-supertonic/bin/python -m pip install --require-hashes -r requirements-supertonic.lock
/absolute/owner/voice-supertonic/bin/python -m pip install --no-deps -e .
```

The lock includes the voice API and Supertonic runtime, not Whisper or Kokoro. Install the optional `voice` extra in a separately reviewed environment if STT or Kokoro is also required; the Supertonic lock alone does not enable them. Ordinary deterministic CI installs no voice weights or inference runtime.

Read the model license before acquiring assets. Code is MIT; model weights/presets use [BigScience Open RAIL-M](https://huggingface.co/supertone-oss-archive/supertonic-3/raw/aafc6e32416a594460b32413efc49d7fe4ce6d46/LICENSE), which includes use and redistribution conditions. The [official repository](https://github.com/supertone-oss-archive/supertonic) was archived on September 9, 2026; this integration depends on preserved local files rather than a hosted service or ongoing upstream support.

An explicit setup download can use the installed Hub CLI:

```bash
/absolute/owner/voice-supertonic/bin/hf download supertone-oss-archive/supertonic-3 \
  --revision aafc6e32416a594460b32413efc49d7fe4ce6d46 \
  --local-dir /absolute/owner/models/supertonic-3
```

Use materialized regular files, including `LICENSE`, `onnx/` and `voice_styles/`. The [checked-in manifest](../../packages/providers/supertonic3_manifest.json) pins all 17 inference/license files. Load verifies sizes and SHA-256 of the four graphs, two configuration files, license and selected voice using no-follow descriptors; SDK construction sets `auto_download=False`. Missing runtime/assets, wrong hashes or unexpected execution providers return a sanitized unavailable response. No request downloads weights.

Keep the owner-controlled model directory outside the Agent workspace and protect it against concurrent mutation. Managed configuration refuses lexical/resolved overlap with that workspace; the loader refuses symlinked model roots, directories and files. Hashing establishes the files read during validation, not an immutable snapshot of later SDK opens. Manual worker launch assumes the same owner-protected directory discipline.

```json
{
  "voice": {"url": "http://127.0.0.1:8765", "profile": "Supertonic 3"},
  "resources": {
    "enabled": true,
    "voice_python": "/absolute/owner/voice-supertonic/bin/python",
    "voice_port": 8765,
    "tts_model_dir": "/absolute/owner/models/supertonic-3",
    "tts_voice": "F1",
    "stt_model": "tiny.en"
  }
}
```

These fields extend an existing full configuration; providers and service URLs remain required. `stt_model` selects an already installed Whisper model; it does not install STT. A standalone worker accepts `--tts-profile 'Supertonic 3' --tts-model-dir /absolute/owner/models/supertonic-3 --tts-voice F1`. Managed launch passes the same values and accepts readiness only when the actual owned PID, CPU device, profile, voice, synthesis mode and revision agree. `loaded: []` and `assets_state: not_checked` mean API ready; `tts`/`verified` follow a successful load. Neither proves successful synthesis until an inference request succeeds.

## Verification and measurement

Deterministic adapter/worker/config tests cover pinned files and versions, optional imports, malformed PCM, bounds, authorization, wrong profile, resource transitions, cancellation during load/inference/lock wait, stream ordering and health identity. Existing Converse epoch, delivery, resource handoff and owner-off regressions remain applicable. The voice-to-task fixture now uses the selected profile and actual PCM rate.

The reproducible [probe](../../scripts/probe_retained_voice.py) launches a disposable owned worker on a separate unused port. It never reads the reference service's credentials or configuration. Example, after explicit setup:

```bash
PYTHONPATH=. /absolute/owner/voice-supertonic/bin/python scripts/probe_retained_voice.py \
  --python /absolute/owner/voice-supertonic/bin/python --profile 'Supertonic 3' \
  --model-dir /absolute/owner/models/supertonic-3 --voice F1 --port 18965 \
  --output /absolute/owner/screen.json
```

Each warm screen repeats three public synthetic utterances three times. The report separates backend load, first returned waveform, first decoded PCM, complete HTTP synthesis, actual audio duration, RTF, process CPU and RSS. A new-process first request is recorded separately; filesystem/page caches are not flushed. Nine warm observations support a small screen with disclosed minimum/median/maximum, not p95 acceptance. Results and exact limitations are in [the public evidence record](../../evaluation/evidence/supertonic3-cpu-2026-10-27.json).

On the Ryzen 7 5800X reference host, these mixed-utterance screens observed:

| Profile | Warm first decoded PCM, median (range) | Warm median RTF, including HTTP | Observed peak process RSS |
| --- | --- | --- | --- |
| Supertonic 3/F1 | 1.157 s (0.714–1.241 s) | 0.219 | 559,706,112 bytes |
| Kokoro 82M/af_heart | 1.258 s (0.614–1.381 s) | 0.245 | 2,884,370,432 bytes |

Neither meets the warm-onset target. The Python/runtime environments differ and host load was uncontrolled, so this is not a quality ranking or a controlled provider comparison. One automatic Whisper tiny.en roundtrip recognized the synthetic sentence but rendered “Kinaiya” as “Canaya”; pronunciation and human listening acceptance remain open.

A separate real managed-process check loaded Supertonic, reaped its verified process group during handoff, restored a new PID, synthesized fresh 44.1 kHz PCM and rejected old Converse speech with HTTP 409. Owner-off remained off through restore and reaped the restored worker. Text inference and control transport in this check were scripted/in-process; no real Agent task or cloud inference was performed. Its initial final-cleanup assertion failed while rebinding the closed TCP port. The original failed report is retained alongside fresh absent-process/refused-connection verification; model synthesis was not repeated to obtain that cleanup result.

The packaged wheel contains the pinned manifest and passes ZIP integrity/readback checks. The optional lock installed with required hashes and its dependency audit reported no known vulnerabilities. A visible Muse Spark 1.3 Contributor/xhigh static review of source `54a2ab4` reported no actionable findings and ran no tools/tests; that is not human approval.

Physical microphone/speaker quality, acoustic audible onset, echo cancellation, expressive consistency, native inference preemption and loaded Converse/Decisions contention remain unverified. The retained shortlist comparison and full M2 gate remain open. The existing reference backend/profile is not promoted by this opt-in implementation.

## Upstream provenance

- [SDK source df0f9686](https://github.com/supertone-oss-archive/supertonic-py/tree/df0f9686dac7fbbde391b759e2ee5286a3737622); installed pipeline/loader/core/module files were compared byte-for-byte with the pinned source.
- [Official Python example/runtime pin](https://github.com/supertone-oss-archive/supertonic/blob/1e9799e964ea4c0dad7cde993b65c3c813a7b373/py/requirements.txt).
- [Pinned model metadata](https://huggingface.co/api/models/supertone-oss-archive/supertonic-3/revision/aafc6e32416a594460b32413efc49d7fe4ce6d46?blobs=true); acquired graph digests matched upstream LFS SHA-256, and small-file Git blob IDs were verified before recording their SHA-256.
