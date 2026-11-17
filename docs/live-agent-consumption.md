# Native live Agent consumer (#623)

Status: gated trusted embedding. `LiveAgentDispatcher` executes native v2
handoffs and can be supplied explicitly to Decisions `create_app` with the same
native journal owner. Normal launcher, Call and Capture Mode rollout remain off.
Converse backchannel/output and complete process replacement qualification remain
separate unfinished parts of #623.

The consumer discovers bounded native publications through
`POST /v2/transcript/live-effects/pending`. Each delivery is checked against its
actual original Session publication. A passive read grants no reservation or
execution. Pending pages contain at most 32 references and 256 KiB, with strict
forward native sequence progress. Completed records are excluded; existing
unstarted and uncertain owners remain discoverable across restart and reset.

One reservation and a separate native start bind the original handoff. A queued
request waits for native Agent status. An explicit target must exist in the same
session/thread/epoch at its expected revision and a terminal state; absence is
not completion. A different started, unconfirmed handoff in that scope blocks a
new queue. Published Agent work can survive microphone closure, while current
user text, disclosure scope, STT provenance and later interruption fences remain
required. Converse effects retain their independent live capture permission.

The one actual producer forwards exact authenticated source text/IDs through the
existing native Agent submit, steer or interrupt API. The interrupt reason uses
the existing 2,000-character metadata bound; its full source reference remains
available. Agent still owns task admission, revisions, permissions, execution
and completion. Decisions grants no tool permission and determines no task
success.

The producer survives facade cancellation and deadlines. It marks the actual
Agent call entered before its first dispatch await. A lost acknowledgment never
replays that call. Restart observes a prior start and reconciles native evidence
instead of acquiring another execution opportunity. Definitive native refusal
of a start permits a later attempt with the same metadata identity after its
blocker changes; unknown metadata requests are not replayed. Healthy retired
operation owners are removed; failed or active cleanup owners remain held.
Close stops new admissions and waits at most two seconds for original consumer
Tasks and enrolled metadata. It does not mean accepted Agent work stopped.

`POST /v2/transcript/live-effects/evidence` reads exact native command evidence.
Submit/interrupt bind authenticated supervisor origin, actor, original scope,
source, receipt-derived key and sequence after start. Steering uses the protected
original intent, request, receipt and event proof. The frozen `steer-requested`
payload stays unchanged; its intent is not proof of later execution. Native
interrupt events additionally retain the original request key, source IDs/raw
revision and expected objective revision, with a stable hashed journal key
(#671). `recorded` means that original native command/intent is durable, never
that the task succeeded or cleanup finished. Missing evidence remains uncertain
and retains capacity; positive late evidence may resolve it without another call.

`POST /v2/transcript/live-effects/source-authority` implements the separate
`native_live_agent_source_v2` read contract. It qualifies a historical requested
raw revision only for an actual captured source. Every intervening captured
revision must preserve the native digest of text, disclosure and STT identity;
only the three valid speaking/capture-end hint combinations may differ. This
accepts harmless storage duplicates without rewriting or resubmitting text, and
rejects changed-then-restored input. Owner-authored legacy windows explicitly
return `live:false`; their strict revision checks remain intact. Source
qualification is factual metadata, not permission or proof the speaker finished.

The owned loop uses native journal wakes from fresh publications and Agent
transitions. A one-second maximum idle heartbeat is not admission pacing. Reads
and unchanged pending work do not create a busy loop. Errors use a short local
recovery backoff; no provider inference, fallback or output reroll is performed.

Owner, Converse, Decisions and Agent can read pending/evidence/source metadata;
Resources cannot. Mutations remain restricted to the selected consumer role.
Bodies authenticate before and after bounded acquisition. All 588 prior schema
files, including the original nine consumption roots, retain their bytes. Six
additive discovery/evidence/source roots have a separate pin
`3569f118f7f114e1927c5e54c6a0e6812ff8458fae3117a9afd043ef8451d888`.

The [qualification record](evidence/live-agent-consumption-2026-11-16/README.md)
identifies exact checks, retained first failures and limits. Actual Converse
consumption, Call integration, full owner/process replacement, echo/mute/
accessibility/no-audio behavior, representative timing comparison and qualified
default rollout remain outstanding. Model answer, classification, persona and
speech quality are diagnostic observations, not architecture acceptance gates.
