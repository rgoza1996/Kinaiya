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

Issue [#73](https://github.com/rgoza1996/Kinaiya/issues/73) repairs admission after
a burst or restart. The native journal selects at most 100 undisposed windows,
ranking the first pending window from every session before second windows.
Completed decisions and terminal `error`, `expired`, `superseded` or `obsolete`
receipts are excluded before allocating the 32-entry in-memory admission cache.
`busy`, `retrying` and `classifying` remain recoverable backlog, independently of
that cache. The owner/Decisions-only journal queries work over local and HTTP
interfaces; the result and native attempt counter are bounded. New indexes are
additive; transcript contents and the existing schema version remain unchanged.
Status receipts now also bind their authenticated window and exact causal,
session/thread/epoch references in the journal before affecting admission.
The SQL result, transferred payloads and in-memory admission are bounded; total
query work still grows with retained window history. Indexed receipt lookups
avoid a correlated full receipt scan. This is not a constant-work journal
projection or a measured throughput/latency guarantee.

Only the latest source event for an utterance may classify or publish a decision.
Superseded entries receive an explicit non-dispatch status. Freshness is checked
again after inference; expired entries receive `expired` without a decision.
The default age limit remains 60 seconds and is checked at admission and result
publication. Reset/deletion is a durable native fence, so obsolete epochs are
excluded without appending old-epoch receipts into the new session.

Numbered admission receipts retain the two-attempt limit across restart. A
transient provider/transport failure has one 350 ms retry delay; recovery of an
uncertain interrupted attempt applies the same delay. Exhaustion or a permanent
refusal records `error`, not an invented ignore decision. Budget/output-limit
refusals are never retried. An unnumbered legacy receipt cannot distinguish the
old worker's first and second calls, so recovery conservatively exhausts it.
Native journal transport, decoding or storage failures leave work pending after
a 350 ms local backoff; reconciliation checks existing receipts before another
admission. Such a failure cannot create a terminal classifier error or prove a
decision append did not commit.
A fresh corrected/resubmitted window can classify normally. The scheduler does
not cancel an admitted remote request merely because a newer hypothesis arrives;
it waits within the existing provider deadline and discards the stale result.

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

Issue [#75](https://github.com/rgoza1996/Kinaiya/issues/75) repairs native journal
recovery in this consumer. The cursor advances only after decision admission is
confirmed; an unavailable source lookup leaves the retained decision recoverable.
Prepare, start and reply appends retain the exact request across an uncertain
acknowledgment. A native start receipt reconciles an allocated generation before
publication. Native I/O has a one-second operation deadline and at most three
attempts with 100 ms backoff. Each attempt rechecks source, current intent, owner
admission and playback generation; backoff releases the per-session lock. An admitted
healthy explanation has no new universal 60-second reply deadline; the existing
source age bound still governs admission.

A brief native outage pauses the same provider iterator. Exhaustion fences its
generation, records a best-effort journal error with confirmed partial state and
unknown delivery, and never replays inference or resumes its buffered text after
healing. Provider errors remain distinct. Owner-off and interruption own the
stream before its first delta. Cancellation and provider cleanup are separate:
unconfirmed cleanup retains its original slot in the existing 32-entry bound,
and a decision deferred solely for cleanup keeps its cursor position. Shutdown
waits at most one second, retaining unfinished ownership rather than asserting
that cancellation closed a resource. An optional iterator `aclose` is used when
present; a conforming iterator without it remains supported. A hard process
restart cannot resume an in-memory model stream; this repair makes no
exactly-once inference or audible-delivery claim.

Review extended #75 narrowly into the two in-repository HTTP text adapters:
HTTPX may close a response inside its iterator at natural EOF, before the
provider context exits. Within an explicit task-local ownership scope, one
shielded task owns the actual response-stream close at EOF, provider error or
cancellation. The window controller enables this scope while retaining its
bounded slot. It defaults off for ordinary typed Converse and Agent calls;
concurrent reuse of one provider instance does not transfer window ownership.
Failed or self-cancelled HTTP
cleanup reports `ProviderCleanupError` and keeps its original bounded slot;
an exhausted generator's later no-op `aclose` cannot prove resource release.
Request bodies, authentication, proxy/budget handling, disclosure and provider
deadlines are unchanged. An opaque third-party iterator's hidden transport
cleanup cannot be introspected or universally guaranteed by this controller.

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

#73 focused verification passed 124 Python 3.12 tests covering backlog, speech
windows, window dispatch, Laya Decisions, window HTTP, partial-speech action
approval and session boundaries. The 37 backlog tests also passed on Python
3.11. Both runs explicitly imported this worktree's source; no inference or
running reference service was used. Ruff lint/format passed and schema export
was unchanged. The Python 3.12 run reported the existing Starlette/httpx
deprecation warning. Full-project and PR CI remain lead integration checks.

The original 35-window/restart reproduction left three sources without a
decision. Before final integration, review also found and corrected ambiguous
legacy retry counts, JSON boolean/integer collapsing, quadratic receipt scans,
and retirement after a transient native journal failure before classification.
Committed regressions exercise those failures, lost decision acknowledgments,
expiry during inference, source-bound status receipts, session fairness,
owner-off/reset fences and duplicate dispatch. The fixed 2,000-source query
fixture requires indexed point lookups and a generous SQLite VM-operation
budget; it is a deterministic regression, not a loaded latency benchmark.

#75 focused verification passed 174 Python 3.12 tests: the new local/authenticated
HTTP recovery matrix, existing speech-window/cancellation/window-HTTP integration
checks and provider unit tests. The 107 recovery/provider tests also passed on
Python 3.11. Imports explicitly resolved to the isolated worker checkout on both
versions. Ruff lint and format checks passed for all 207 Python files.
These fixtures use native journals and deterministic text or mocked HTTP bytes;
they perform no paid inference, reference-service requests or physical audio.
Full-project and PR CI remain lead integration checks.

The original #75 reproduction on main `6336484` lost a retained decision after
one transient source read: Converse made no provider call while independent
native Agent dispatch still submitted its fixture task. Prepare append failures
lost admission; a failed start could strand an allocated generation; uncertain
start/reply acknowledgments closed the provider after its first delta. The
blocked `IGNORE` backchannel control preserved both chunks and refuted the
separate truncation hypothesis. Tests now cover the actual failures, exact
append identity, stale intent/owner/epoch changes during uncertain appends,
timeout recovery/exhaustion, long healthy replies, and 32 cleanup owners holding
the 33rd durable decision until cleanup confirms.

Draft checks are retained rather than relabelled as passes: the first combined
run had 129 passes and four fixture failures. A committed native start receipt
correctly reconciles a permanently lost acknowledgment, so it is now a positive
case; the final-read interrupt fixture now enters through `tick` so its decision
cursor is actually consumed. A later run had 139 passes and four fixture failures:
the capacity test now awaits its bounded parent completion, and the mocked
remote provider URL uses required HTTPS. A subsequent 147-test run passed before
review exposed HTTP EOF/error cleanup cancellation: the targeted reproduction
had two passes and four failures. The response-close guard then passed all 12
blocked/EOF/error adapter cases; final checks additionally cover failed and
self-cancelled HTTP cleanup without releasing the original slot.

Deterministic checks exercise the real journal, authenticated HTTP, browser PCM
encoding/bounds, independent timing, correction/replay, held/interrupting replies,
native Agent file execution and cancellation, disclosure and UI capture/playback.
Synthetic providers in these tests do not establish model classification quality.
Real-provider evidence is recorded separately with its exact source revision.

[Two actual-provider runs](../../evaluation/evidence/streaming-windows-2026-10-27.json)
exercise the five examples, retained partial/full/opening STT snapshots, native
revision-2 steering and cancellation, genuine queue boundaries, real file writes
and an authoritative completion notice. Each retained seven windows and activated
no Agent for backchannel/unfinished speech. The full-workflow runtime source is
`059064e149e803194293f02da605fbcbc7f3d0f9`. Classification prompts used 1,541–2,260 characters, with
61–263 history characters in the repeated run; tasks received source references.

The repeated Contributor development run classified windows in 2.56–3.96 seconds.
Native text-to-reply publication was 7.81 seconds for the interrupting reply and
12.44 seconds for the queued-work reply, including its opening hold. Kokoro HTTP
first PCM was 393/509 ms across the two runs. These are distinct measurements;
first PCM is not end-to-end or audible TTFA. The synthetic HTTP adapter declared
one-second capture intervals, so these runs do not benchmark acoustic endpoint
timing or PCM capture duration. Browser encoding/duration is checked separately.
The roadmap's real-time target and broader M3 acceptance remain open.

The later scheduler refinement finishes one admitted classification and retains
only the latest pending hypothesis for that utterance. It discards stale results
without repeated HTTP cancellation, which cannot recall remote inference. An
actual-provider burst on the recorded final scheduler source retained eight
overlapping windows, made two classifier starts, produced the correct latest
WAIT decision and activated no Agent. Per-run source hashes distinguish this
check from the preceding full audio workflows.

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
