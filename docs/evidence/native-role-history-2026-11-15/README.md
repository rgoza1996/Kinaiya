# Native history architecture evidence (#483 / #626)

The adapter preserves the original first system instruction, ordered authorized
prior text roles/content and original current PCM through an explicitly
negotiated `native_history.v1` protocol. A count and canonical SHA-256 digest bind
the acknowledgment before PCM admission. Limited endpoints retain explicit
`system_json` provenance. Empty history leaves the first system string exact.
See the [contract](../../native-realtime-history.md) and
[downstream patch instructions](../../../patches/llama-cpp/README.md).

The [plan](plan.json) was recorded before the single native generation. The
[build receipt](build-receipt.json) pins the changed realtime source/object and
private implementation library. It reused the existing build's other objects
and libraries; it is not a clean rebuild or an owner-engine installation.
[Ordered patch application](patch-verification.json) verifies all six final
source files against the patch manifest on the exact engine base.

The compiled [template receipt](template-receipt.json) records 40 checks,
including the pinned actual LFM3 template, native roles/order/content, literal
marker text, refusal immutability and the original method's marker-collision
failure. Python and C++ independently agree on the Unicode history digest.
This is actual template reconstruction, not a model semantic score.

The [native results](native-results.json) record 13 real WebSocket controls and
one original public synthetic PCM call in a private CPU-only namespace. Roles
and the canonical digest match; a fitting 300 KB history keeps its acknowledgment
below the unchanged 256 KiB event bound. Unsupported/malformed/over-bound or
already-started history refuses, and ASR does not inherit realtime history.
The original socket closes and original engine exits zero; the intended private
implementation library was mapped. Input and generated text/PCM match the raw
frames. No owner home, outside network, GPU device or Agent was exposed.

The native run used the recorded dirty source over main `2cafdc0…`.
[Source association](source-association.json) retains those exact source hashes;
it is not described as a run at the final PR commit. The current provider's
offline replay checks all seven sent and 48 received frames exactly, with one
original close and zero additional inference. The portable fixture is under
`tests/fixtures/native_history/`; its hashes and the original raw artifacts are
in [verification](verification.json).

First failures are retained in [check history](check-history.json). The initial
full-echo history oracle had 20 failures/one pass. The bounded digest/metadata
revision had 14 failures/11 passes. Empty-history framing then failed both
pre-fix checks. No oracle was rerun against a model to select a favorable answer.
The first helper link spelling and output-directory errors were corrected in
the qualification driver; their logs remain in the local output directory.

Run the portable focused checks from the repository root:

```sh
PYTHONPATH=. python -m pytest -q \
  tests/unit/test_realtime_audio_history.py \
  tests/unit/test_realtime_audio_provider.py \
  tests/unit/test_realtime_audio_history_recording.py \
  tests/unit/test_native_audio_history_metadata.py \
  tests/unit/test_native_audio_converse.py
```

They pass 221 tests after the empty-history correction. The broader final check
passes 592 tests in 118.04 seconds across 14 modules, including task/delivery
context, disclosure, capacity, connection, retirement and transport readiness.
Ruff check/format and the schema export/diff checks pass; schema bytes are
unchanged. Results are recorded in `check-history.json`.
The `.py.txt` helpers retain the actual local qualification commands and paths;
they are receipts rather than a portable runtime installer. Raw frames/logs and
first errors remain under `output/issue-626-native-history/` in the original
workspace. The compressed public wire fixture provides the repeatable adapter
check without requiring those local paths or models.

Model answer quality, style, voice identity and speech quality are diagnostic
only. This evidence does not qualify microphone/ASR capture, browser/device
delivery, Agent effects, new engine platforms, or a change to an owner's running
engine/configuration. No default model, sampler, prompt tuning or semantic output
rewrite is part of this correction.
