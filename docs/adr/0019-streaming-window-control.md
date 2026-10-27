# ADR 0019: retained speech windows and independent Decisions channels

Tracks [#49](https://github.com/rgoza1996/Kinaiya/issues/49) and
[clarification §10](../../clarification.md#10-decisions-real-time-converse-control-and-agent-routing).
The previous silence-ended recording path did not implement content-based
conversation timing. Live listening now records rolling PCM and submits a
snapshot about once a second while speech changes, including the first physical
speaking opening. This capture cadence is not a conversation decision.

## Native contracts and persistence

`SpeechWindow` 1.0 binds session/thread/epoch, utterance, window index, revision,
text, audio interval, speaking/capture flags and disclosure scope. The journal
retains every recognized snapshot before classification. Corrections increase
the revision of the same window slot; other snapshots remain separate events.
Resolve follows same-slot corrections. Recent utterance lookup selects the
greatest window index/revision. Authenticated owner/Converse writers alone may
publish windows; model output cannot invent their provenance.

The owner-only audio endpoint decodes bounded temporary audio, validates scope
and revision before STT, and records the actual recognized text and audio digest.
Request text is not authoritative STT. Exact retries return the recorded event
without another transcription; identity reuse with different audio/metadata is
rejected. Raw audio is not persisted. The browser keeps at most thirty seconds
of PCM, one request in flight and one latest pending raw snapshot. Dropped raw
snapshots have not been transcribed; they are not represented as retained text.
All produced transcript windows, including ignored and superseded hypotheses,
remain available through authenticated transcript retrieval.

`WindowDecision` 1.0 has two distinct labels: the four Converse outcomes and
the four Agent routing outcomes in clarification §10. Native code binds the
source event, sequence and revision after inference. The classifier sees a
bounded context and task IDs/states/revisions, never task objectives. It sees
the full current hypothesis or refuses an over-budget request. Repeated,
unchanged hypotheses reuse classification within a bounded utterance cache.

Classification retries a transient transport/timeout failure once, while the
source remains fresh. Budget refusal is recorded explicitly and never retried.
`streaming_windows.classification_timeout_s` defaults to five seconds and accepts
0.1–30 seconds; slower development providers can choose an explicit bound without
claiming that they meet the real-time latency target. Cache reuse also binds the
current task identities, revisions, states and Agent availability.

## Independent consumers

Converse observes the durable decisions separately from the Agent dispatcher.
`IGNORE` preserves existing speech. `WAIT_FOR_NEXT_WINDOW` yields old speech
without making a new reply. `RESPOND_INTERRUPT_USER` may publish a reply during
user speech. `RESPOND_WITHOUT_INTERRUPTING_USER` prepares bounded reply text and
holds publication until a later snapshot reports an opening. The browser also
checks its current speaking state before scheduling the first PCM of that reply.
Neither acoustic overlap nor a fixed end-silence delay chooses the semantic
outcome. Typed conversation retains its independent immediate-response path.

Replies use the existing runtime identity, playback generation, delivery receipts
and true local playback completion. Corrections and newer conversation controls
revoke stale synthesis. A native start receipt prevents repeated responses to
one utterance. Browser ownership prevents historical window replies from playing
after reconnect/disarm; first actual scheduled reply audio starts a new capture
utterance without closing the explicitly armed microphone.

Agent routing uses native submit/steer/interrupt with exact authenticated source
text and current task revision. Queue receipts wait for task completion and
release work in order while Agent is idle. Cancelled targets, newer interrupts,
source corrections, epoch resets and Agent disablement cannot revive old queued
work. Indexed receipt queries avoid prefix-history truncation during replay.
Unknown executor acknowledgment remains explicit and is not automatically retried
as another action. Agent status failure means unavailable, never idle.

The Agent rechecks pending steering at its actual execution boundary. Windows
and derived replies carry disclosure scope through context and transcript tools;
Contributor receives public/synthetic scope only. Outcome notices inherit window
source scope. Retrieval authorization may withhold content from a cloud model
without deleting it from the owner's full journal.

## Verification and limits

Deterministic checks exercise the real journal, authenticated HTTP, browser PCM
encoding/bounds, independent timing, correction/replay, held/interrupting replies,
native Agent file execution and cancellation, disclosure and UI capture/playback.
Synthetic providers in these tests do not establish model classification quality.
Real-provider evidence is recorded separately with its exact source revision.

[Two actual-provider runs](../../evaluation/evidence/streaming-windows-2026-10-27.json)
exercise the five examples, retained partial/full/opening STT snapshots, native
revision-2 steering and cancellation, genuine queue boundaries, real file writes
and an authoritative completion notice. Each retained seven windows and activated
no Agent for backchannel/unfinished speech. The final runtime source is
`059064e149e803194293f02da605fbcbc7f3d0f9`; subsequent changes add verification
and evidence only. Classification prompts used 1,541–2,260 characters, with
61–263 history characters in the repeated run; tasks received source references.

The repeated Contributor development run classified windows in 2.56–3.96 seconds.
Native text-to-reply publication was 7.81 seconds for the interrupting reply and
12.44 seconds for the queued-work reply, including its opening hold. Kokoro HTTP
first PCM was 393/509 ms across the two runs. These are distinct measurements;
first PCM is not end-to-end or audible TTFA. The synthetic HTTP adapter declared
one-second capture intervals, so these runs do not benchmark acoustic endpoint
timing or PCM capture duration. Browser encoding/duration is checked separately.
The roadmap's real-time target and broader M3 acceptance remain open.

The initial failed provider attempt and a startup preflight failure remain in the
evidence. Conservative admission permitted two concurrent full-context
reservations near the end of the run; the cap was preserved. The probe waits for
the previous native steering/approval boundary before its next example. Reported
token estimates are not billing receipts, and retained uncertain reservations
are not removed to create spending headroom.

This is an experimental Linux/web implementation. Browser AudioWorklet has a
ScriptProcessor fallback where available; unsupported audio capture fails
explicitly. Classification timeout and bounded concurrency are operational
limits, not proof of natural turn-taking quality. No acoustic-only test proves
audible latency, echo robustness, production ASR accuracy or the full M3 gate.
The older clip interface remains for hosts that do not advertise this capability;
it is not claimed as semantic streaming. `streaming_windows.enabled=false`
explicitly disables the new path during deployment rollback.
