# Native live Converse consumer (#623)

Status: gated trusted embedding. `LiveConverseController` consumes native v2
`converse_backchannel` and `converse_reply` effects through the existing Converse
personality, context, model, speech and cleanup owner. Normal host, browser Call
and default Adaptive Live rollout remain unfinished and off. This increment
does not qualify physical audio or complete #623.

Construct one controller for the original `ConverseRuntime`, then explicitly
supply that same instance as both `window_controller` and `live_consumer` to
Converse `create_app`. Construction starts no inference or microphone. A second
window owner is refused. The optional lifespan runs one journal/completion-driven
consumer; ordinary embeddings retain the previous behavior.

## Original effects and output

Each bounded pending reference is checked against its actual native Decisions
publication. One reservation and a separate one-use start precede generation.
The original Tasks survive caller cancellation and observation deadlines.
Unknown metadata is not written again. A different executor observes existing
native evidence or retains uncertainty; it never reruns the model. Native
evidence binds the original effect, source, scope, behavior, first-chunk key and
sequence after start. `recorded` means a native chunk is durable, not that a
speaker played it, cleanup finished or an Agent task succeeded.

Converse retains its configured personality and qualified context. Native v2
action/source/speaking facts join the instructions before the existing context
budget is applied. `WAIT_WITH_BACKCHANNEL` permits an acknowledgment; it does
not require wording, frequency or even nonempty output. Exact provider deltas
are also retained separately from speech sentence boundaries, within the
existing 16,000-character text-output bound. Cancellation/errors are identified
without presenting incomplete output as a completed provider response.

V2 state phases use `live_` names and effect-derived keys. A backchannel does not
consume v1's full-utterance response slot. A later full reply can use the same
capture and utterance. This output owner never closes or resets the microphone.
There are at most 32 prepared session slots and 64 retained local effect routes;
native ledger limits and existing provider/resource limits still apply.

## Native authority and playback

The original live capture permission must remain active. Session, thread,
epoch, capture identity, user text, disclosure scope and STT identity must still
match. Session checks every intervening native capture digest; changed-then-
restored text cannot revive an old reply. Harmless speaking/end-hint revisions
can release a buffered noninterrupting reply without another generation.
Decisions inference/publication freshness remains strict and separate.

A later authenticated v2 wait fences an older buffered reply. Another selected
conversation action on a different utterance/text also fences it. Ordering uses
the original source sequence, so an older wait cannot cancel newer input.
Switching between backchannel and full-reply intent supersedes the incompatible
older mode, including a queued acknowledgment that must not follow a newer reply.
`IGNORE` alone grants no new action or cancellation; it cannot revive changed
user authority. Existing v1 controls and strict v1 event identity remain intact.

Speech validation delegates live-marked chunks to this same trusted owner. It
requires the actual cached native event, original effect/source/start and current
authority, alongside runtime, owner admission, playback generation, memory and
task-context checks. Missing or wrong ownership refuses. A raw speaking-hint
event-ID change therefore does not by itself suppress an authorized v2 reply.

## Completion and recovery

Provider completion wakes the loop even while its one original native wait is
still pending. It does not cancel/replace that read or spin on its own status
observations. A noninterrupting buffered reply remains pending while the user
speaks; provider EOF alone is not a declined response or a released consumer.
A gated Call client therefore cannot use `live_output_observed` as a logical
utterance boundary. The additive v2 state phase `live_no_audio_ready` is separate:
the original full-reply producer must be reclaimable, unabandoned, have no
published chunks and still pass native source/selection/authority checks. Its
effect-keyed observation repeats the original source, utterance, behavior,
generation (possibly null) and disclosure scope. The original retained append
owner prevents replay. This supports an empty/no-audio reply without treating
provider completion, model answer quality, or a held reply as acoustic completion
or physical playback. Backchannels do not emit this full-reply readiness phase.
A terminal failed wake observation leaves the loop slot before its error is
reported, so a later native publication can be observed. Any unsettled original
read/close still belongs to the original run owner; this is not a model or
mutation retry (#676).

Close fences new admission and the original pending output, then performs a
bounded join. Unsettled native waits, provider/model/stop cleanup and failed
operation owners remain owned and make its cleanup result false. Later positive
settlement of those same originals can confirm cleanup. Lifespan always closes
the stream registry and required dependency even if consumer close raises;
errors remain visible. Logical effect completion and physical cleanup are
separate facts.

The native controls are in
[`test_live_converse_consumer.py`](../tests/integration/test_live_converse_consumer.py).
Normal host/Call wiring, complete owner/process replacement, echo/self-
transcription, mute/accessibility/no-audio qualification, representative timing
comparison and qualified default migration still require full #623 integration.
Model answer, persona, classification and speech quality are diagnostic outcomes,
not architectural acceptance gates.
