# Original live window publication (#623)

Status: native journal increment under qualification. Adaptive Live remains off.
This increment enables no capture, inference worker, reply, backchannel, playback,
model selection or runtime default.

## Two records, one transaction

Converse's explicit `LiveWindowPublication` carries protocol
`live_window_publication_v2`, schema version 2.0, its native owner/capture identity
and the original unchanged 1.0 `AppendRequest` for `conversation.window`.
Authenticated service credentials grant this writer role; transcript/model
content cannot supply that authority. The source cannot supply causal/task grants.

The sole Session writer atomically inserts the authenticated-user transcript and
a separate `conversation.window_capture` annotation in `BEGIN IMMEDIATE` under
the original Store lock. The annotation's version is2.0; its enclosing journal
entry remains 1.0. It records Adaptive Live, original request SHA256, raw source
event/sequence/revision, native owner/capture and eligibility references. Its
cause is exactly the raw source and its sequence immediately follows that source.
A failure between inserts rolls both back. Another SQLite connection sees both
committed records or neither. Existing transcript/history/UI fields stay intact.

One utterance belongs to its original publication mode and live capture owner.
An unannotated legacy utterance cannot be converted after admission; a live
utterance cannot switch owner/capture or accept legacy appends. A new capture
uses a new utterance identity. The ordinary append endpoint cannot create capture
annotations. The old pending-window query excludes annotated live sources, and
the native writer refuses old `routing.window_decided`/`routing.window_status`
publications against them. Legacy unannotated sources retain the 1.0 path.

## Original eligibility, not model output repair

Every new raw snapshot persists. Exact text, speaking/capture-ended hints,
disclosure scope or native STT identity changes advance the eligible revision.
Storage index/revision, timing and audio digest alone do not advance it. This
comparison contains no semantic, confidence or persona heuristic.

An identical snapshot points to the same original eligible source/capture pair;
it never rebinds an earlier classifier result to another raw event. The candidate
contains that eligible pair and the latest raw pair, with matching owner/scope,
input fingerprint and eligible revision. A future worker must explicitly select
the 2.0 contract and recheck current authority and this native eligibility before
dispatch/publication. Journal notifications remain recheck hints only.

## Recovery and bounds

`POST /v2/transcript/live-windows` requires Converse and returns an exact positive
pair proof. An identical explicit publication returns the same existing proof.
`POST .../lookup` requires Converse and performs read-only lookup by complete
original reference; it never transcribes, appends or substitutes a latest window.
A missing receipt is unknown, including after deletion or backend replacement;
it is not proof that an earlier write failed. Historical positive proof survives
restart/reset while the original journal rows survive, without granting current
capture/effect authority.

`POST .../latest` requires Converse or Decisions and a strict current
session/thread/epoch/utterance scope. It returns the two original pairs or null.
Stale or unavailable scopes refuse. LocalJournal enforces the same role rules.
JournalClient retains each original HTTP send, response read and sole close
through caller cancellation, using the existing bounded exchange owner. Its
publication request is detached from the caller's nested mutable v1 object
before sending. No automatic lookup polling or mutation retry is introduced.

Requests require identity JSON, no query or duplicate envelope headers/keys,
strict integer counters and bounded structure. Publication is at most 68 KiB,
including an unchanged journal request at most 64 KiB. Lookup/scope bodies are at
most 4 KiB. Pair/state responses are at most 144 KiB; two-pair candidates at most
288 KiB. Body collection has a five-second deadline. Protected SQLite request,
value, identity and integer columns are bounded before Python allocation and
compared against the serialized original. Responses are uncached. Unavailable
proof never grants dispatch, disclosure, playback, task success or no-write state.

## Verification and remaining work

See [the source-pinned receipt](evidence/live-window-publication-2026-11-16/README.md)
for actual SQLite/HTTP controls, original failures and regression results.
Seven additive schemas have an independent byte pin; all prior schema bytes and
the original 184-file schema-family count/hash remain unchanged.

This is synthetic architecture/provenance evidence. It does not qualify physical
audio, real ASR/model performance, process reaping or the complete feature.
Completion-driven admission/coalescing, v2 decision publication/consumption,
same-capture decoder rollover, backchannel/echo/mute lifecycle, stage timings and
rollout remain required. Model answer and speech quality are diagnostic
observations outside Kinaiya's architectural acceptance gate.
