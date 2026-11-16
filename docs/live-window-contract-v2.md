# Explicit live Decisions contract 2.0 (#623)

Status: classification/provider increment under qualification. Adaptive Live is
still unavailable/off. This increment does not enable a new worker, scheduler,
backchannel producer, microphone path or runtime default.

## Independent outcomes and authority

`LiveWindowLabels` requires schema_version 2.0 and all five model-output fields:
conversation, agent_route, target_task_id, expected_objective_revision and
reason_code. It is a strict closed immutable model. Model output cannot supply native source identities, permission, success or
playback fields.

Conversation has five outcomes: IGNORE, WAIT_FOR_NEXT_WINDOW,
RESPOND_INTERRUPT_USER, RESPOND_WITHOUT_INTERRUPTING_USER and WAIT_WITH_BACKCHANNEL.
The last continues listening and permits an optional short acknowledgment;
Converse owns its wording, personality, TTS and playback. It does not request a
full reply, stop capture, interrupt the user or grant Agent permission. The four
Agent routes remain independent, including for WAIT_WITH_BACKCHANNEL. No label
alone proves that the speaker finished or that any effect occurred.

`LiveWindowDecision` adds authenticated source session/thread/epoch, utterance,
window index, event/sequence/revision and native counters. The classifier obtains
these bindings from the native source after validating the closed labels. An
untrusted origin or mismatched envelope/payload scope is refused before inference.
A selected task/revision must match the frozen task references actually submitted
to the provider. Unknown targets are refused without replay or substitute labels.
These bindings describe the original input; a future effect consumer must still
recheck freshness, current authority and native lifecycle before acting.

## Explicit version selection and compatibility

`WindowClassifier(..., window_contract_version=2)` selects 2.0 deliberately. Its
provider request includes window_contract_version:2 and the 2.0 instructions.
The generic JSON response must explicitly declare schema_version:"2.0". Missing,
legacy, unknown, coerced or mixed versions are refused. The request builder,
projection/reuse digest and instruction stamp bind the selected contract. A future
scheduler must use the same selected version for projection and classification.

The default classifier remains 1.0. Its request/instructions, four outcomes and
WindowDecision export are unchanged. The Rolling Windows worker explicitly
refuses a 2.0 classifier before inference. No four-outcome response is silently
adapted into a five-outcome response, and no 2.0 decision is published through the
old worker/journal path. Existing window transcript 1.0 formats remain unchanged.

Laya, native d1 and OpenRouter SystemOne adapters recognize the explicit window
version and submit five conversation criteria for 2.0. Their historical IDs stay
stable: backchannel maps to IGNORE, unfinished to WAIT_FOR_NEXT_WINDOW, urgent to
RESPOND_INTERRUPT_USER and ready to RESPOND_WITHOUT_INTERRUPTING_USER. The new
acknowledge ID maps to WAIT_WITH_BACKCHANNEL. Its probability/choice grammar is
explicitly five-way. A four-way response cannot satisfy that request. Finalized
classification remains unchanged. No threshold, model, provider, selected owner
prompt, retry or runtime default is changed.

## Evidence and remaining integration

The independent vectors exercise all five conversation outcomes with all four
Agent routes through the actual compatible HTTP adapter; all three typed HTTP
adapters submit the frozen five-choice grammar and preserve its outcomes. Checks
cover old response refusal, forged targets/revisions, model-supplied native fields,
coerced versions, private input/prompt disclosure, source scope and the two frozen
additive schema exports. See the [evidence receipt](evidence/live-window-contract-v2-2026-11-16/README.md).

This is architecture and protocol qualification using synthetic HTTP responses.
It makes no claim about answer quality, acknowledgment wording, real model
performance, physical audio or complete Adaptive Live readiness. Remaining work
includes completion/fresh-input scheduling, qualified publication/consumer routing,
same-capture decoder rollover, backchannel/echo/mute ownership, timing evidence and
rollout. No future requirement is presented as shipped.
