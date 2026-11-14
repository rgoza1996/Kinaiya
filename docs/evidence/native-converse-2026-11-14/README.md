# Native Converse qualification, November 14, 2026

Issue #483 remains **open**. These are fresh public synthetic observations,
not a fix for the earlier private rehearsal or a general model-quality claim.
No owner service, installed engine, configuration, microphone, cloud provider or
actual Agent task was used or changed.

## Observed boundaries

The actual current Store -> NativeAudioOwner ASR -> WindowConverseController ->
RealtimeAudioProvider path first exposed an engine compatibility gap. The
unmodified llama.cpp build omitted `tools: []` and `tool_choice: "none"` in its
session acknowledgment. Kinaiya refused before conversational input PCM or
`response.create`; all three ASRs had completed. This failure is retained and
must not be counted as a successful dialogue run.

A separate, compiled engine overlay acknowledges disabled tools and rejects
attempts to enable them in either session or response settings. Eighteen real
WebSocket negative controls passed. The installed executable, shared libraries
and clean engine source checkout remain unchanged. Kinaiya's existing strict
acknowledgment and output checks were not relaxed. The patch is a qualification
candidate, not an installed or generally qualified engine release.

## Dialogue observations

The compatible engine produced actual assistant text and 24 kHz mono PCM16 for
three public inputs under the existing persona and two explicit custom-persona
experiments. Every exported WAV/PCM digest matches its native receipt; every
published text matches the actual endpoint text. All nine original outputs are
retained in [observations.json](observations.json) and the adjacent WAV files.

- **Existing persona:** drafts the requested picnic checklist, revises it on
  follow-up, and correctly answers the unrelated triangle question. It does not
  provide the desired receipt-only work acknowledgment.
- **Appended Agent-answer preference:** still drafts and revises the checklist.
  This demonstrated no semantic improvement and is not a product change.
- **Concise custom persona:** initially acknowledges the request but repeats
  its details and speaks as though the task were its own. The follow-up claims
  an update and drafts a revised checklist. This also fails the complete
  handoff behavior. It is not a verified repair.

These fixtures are consumed observations, not an unseen holdout. Similarity
values are descriptive string comparisons, not calibrated echo detectors or
quality scores. Audio has not been listened to, decoded speech has not been
independently assessed, and browser/physical delivery is unqualified. No output
was replaced with a successful acknowledgment or input replay.

## Source and configuration review

The served chat template matches the
[published model template](https://huggingface.co/LiquidAI/LFM3-Omni-2B/blob/main/chat_template.jinja)
except for its final newline. The observed text sampler is already greedy
(`temperature=0`, `top_k=1`). The forced mixed opening mode corresponds to the
model's documented interleaved mode. This review does not establish equivalence
between quantized llama.cpp inference and the full reference implementation.
It does not identify an accidental ASR call as the dialogue-quality cause.

The JSON records app/engine/model/library hashes, public instructions, actual
ASR and output text, durations, native publication correspondence, and initial
setup/launch failures. Explicit synthetic Decisions dispositions release each
Converse turn; no Decisions accuracy or Agent execution is claimed.

Next work must provide and qualify the framework's authenticated task context
(#486) and editable handoff defaults (#559), preserving owner/model
responsibility (#557), original audio, complete qualified history, permission,
disclosure, currentness and output guards. A successful protocol exchange alone
cannot close #483.
