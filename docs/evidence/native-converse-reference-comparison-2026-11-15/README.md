# Native and full-reference speech comparison, 2026-11-15

Both implementations failed the declared work-receipt and pending-follow-up
criteria, and both answered the unrelated question correctly. **Issue #483
remains open.** These are the first outputs of three fresh public synthetic
inputs, with unchanged built-in personality and actual authenticated task
observations. They supply no verified dialogue correction or owner promotion.

| Case | Native app path | Pinned full reference |
| --- | --- | --- |
| Ask Agent for a neighborhood seed-exchange welcome plan | Drafts its own plan; calls it a seat exchange | Drafts its own plan; also calls it a seat exchange |
| Add a station for returning empty seed envelopes | Drafts another plan despite pending steering | Drafts another plan and misses the requested station |
| What does water turn into when it freezes? | Correct: ice | Correct: ice |

The native ASR separately preserves “seed exchange” on the first turn. Its
follow-up transcript changes the input to “Inclusion station for returning empty
cd envelopes in that same request.” That discrepancy is retained. The full
reference consumes the original PCM, rather than the ASR transcript.

## Scope and provenance

The native application was clean main `abb0f87f947fc772b7da1fb5c1403045e46507e3`.
Actual Store, NativeAudioOwner, WindowConverseController and RealtimeAudioProvider
processed the original PCM through the owned loopback engine. A real native
Agent accepted the request; its controlled provider held one original request
without tools or effects. Steering remained pending at objective revision 1.
Twelve actual TaskContext HTTP observations retained the running state. Decisions
dispositions were controlled fixture inputs, not classifier-quality evidence.

The engine source was `8ca4060b7e30e76baaa5670ee1ec1b760b7f8552`, with the separately
qualified disabled-tool ACK overlay and existing Omni Q4_K_M core / Q8_0
projector. The initial minimal-environment launch exited 127 before readiness
because it omitted the existing ROCm library search directory. Its original
manifest/log remain here. The recorded correction adds that resolved directory;
inputs, seed, model, personality, sampling and application code are unchanged.

The reference uses [LiquidAI/LFM3-Omni-2B at revision
21d75123](https://huggingface.co/LiquidAI/LFM3-Omni-2B/tree/21d75123f86c1f7ffb4bef1ea202710c4e7ebe67),
complete SHA-verified main and codec weights, Torch 2.9.1 and a private verified
Transformers 5.20.2 wheel overlay. Full main-model and codec loads report no
missing, unexpected or mismatched keys or errors. It runs float32/eager on CPU
inside bubblewrap, with no network, GPU devices, owner home or credentials
mounted. Existing global packages and owner runtime configuration are unchanged.
The standard PyTorch causal-convolution fallback warning is retained locally;
no optimized package was installed to change that execution.

Both conditions declare seed 483, greedy text, 512 output slots, speech
temperature 0.4, repetition penalty 1.2 and repetition window 64. The native
projector metadata and reference configuration agree on the inspected settings.
Engine/RNG and numerical precision differ; equal seeds do not imply equal
sampled tokens or PCM.

Each reference call receives the **exact captured native generation
instructions and original PCM**. In particular, its later conditional input
contains actual prior native replies. This isolates a conditional comparison;
it is not a separate interactive reference conversation or independent Agent
integration. No input is rerolled for a preferred semantic result.

The actual GGUF chat template is byte-identical to the pinned source template:
SHA-256 `ea74cc1d14319ed436dc0f37632893004d436913113e7605267f2b01694d65e4`.
These observations and the earlier numerical feature audit do not demonstrate
a template/preprocessing correction or justify calling a quantization change a
fix. They do not establish encoder/backbone/codec numerical equivalence.

## Retained evidence and verification

- `plan.json` freezes exact inputs, criteria and settings before generation;
  `launch-correction.json` retains the launch-only correction.
- `native-observations.json`, `task-observations.jsonl` and
  `reference-conditional-inputs.json` retain original task/publication identities,
  instructions, ASR and text. `reference-observations.json` retains first outputs,
  all generated token IDs and loading metadata.
- Three original input WAVs, three native output WAVs, three reference output
  WAVs and three reference token/code NPZ arrays are unchanged copies. Original
  reference float waveforms, feature arrays and six full native wire recordings
  remain in the owned local evidence directory; their wire digests are recorded
  in `verification.json`.
- The reference generated 504/486/58 slots and 389/374/45 codec frames, decoding
  to 31.12/29.92/3.60 seconds at 24 kHz. All decoded samples are finite; conversion
  to the retained PCM16 WAVs required no clipping. PCM and token/code lengths
  were checked against the retained originals.
- Both owned generation processes exited 0, with no native cleanup errors.
  Independent retained-byte verification reconstructs raw ASR, text deltas and
  completed response, matches publication and native PCM, validates all twelve
  TaskContext packets through the versioned contract, and checks all nine WAVs.
  No inference runs during this verification.
- The initial export verifier confused a prompt-framing field with the raw
  TaskContext contract. Its failure is retained in `verification-failure-1.txt`;
  the corrected verifier validates the actual contract. No model output changed.

The exact orchestration, isolated reference and verification scripts are
retained as `.py.txt` files, with file digests in `files.json`. The model weights
and private runtime overlay are deliberately outside the repository; re-running
requires the same explicitly prepared assets and intended isolation. This
evidence does not claim audio listening, audible/device delivery, browser
playback, model-independent routing accuracy or completion of #483.
