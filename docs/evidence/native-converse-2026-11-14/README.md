# Native Converse qualification, November 14, 2026

Issue #483 remains **open**. These are fresh public synthetic observations,
not a fix for the earlier private rehearsal or a general model-quality claim.
No owner service, installed engine, configuration, microphone, cloud provider or
actual Agent task was used or changed.

That statement describes the original baseline below. The later task-context
qualification uses a separate real native Agent task with a controlled held
provider, as described next; it does not touch an owner task or run any tool.

## Fresh qualification with native task observations

The later [current-main qualification](../native-converse-current-main-2026-11-14/README.md)
uses the now-shipped explicit pending-steer field and three fresh inputs. It also
fails the receipt-only handoff behavior; the original observations below remain
unchanged.

At application commit `9f8cffc7e27155b9dd455f1aa26ac93d8c3b3a34` with a clean
worktree, three fresh public speech inputs exercised the same native model and
compatible engine overlay. Actual Agent acceptance and steering use the native
Store/AgentRuntime; the actual TaskContextClient reads its ephemeral Agent
service over genuine socket HTTP. Twelve original metadata responses and the
first system messages are retained in
[task-facts-observations.json](task-facts-observations.json). All six associated
input/output WAVs remain unedited and match the retained native text/PCM
publication receipts.

The work request asks for an imaginary camping packing checklist. Despite the
editable default preference and actual running-task observations, LFM3 drafts
the full checklist instead of acknowledging handoff. It then drafts a revised
checklist for the flashlight/socks follow-up. The unrelated hour question is
answered correctly. The follow-up's ASR also alters its wording; that output is
preserved. These consumed fixtures demonstrate **no verified handoff repair**.

The Agent model is an explicitly controlled, held fixture: one request enters,
no tools run and no task completes. The real native steering receipt queues a
pending change while that original remains held. The observed task remains at
revision 1; neither the steering receipt nor model speech proves revision 2
was applied. A subsequent draft contract adds an explicit pending-steer flag;
this run predates that field and is not evidence for its model behavior.

The engine exits normally (`0`); no cleanup failure is recorded. Audio has not
been listened to and browser/device delivery remains unqualified. The original
negative nine outputs below are preserved. Issue #483 remains open.

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
