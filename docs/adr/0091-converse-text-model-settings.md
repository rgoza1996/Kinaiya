# 0091: Owner-selected Converse text models

October 31, 2026. A bounded increment of [#208](https://github.com/rgoza1996/Kinaiya/issues/208)
following the protected provider catalog (ADR0086) and Agent task selection
(ADR0089). Converse owns this independent text choice. Decisions, STT and TTS
retain their separate owners; no direct audio, camera, video or fallback route
is established by a saved model declaration.

## Selection, inspection and persistence

Owner-only `GET /v1/converse/options/model` reads the current selection, native
revision/state token, eligible catalog declarations and actual operation counts.
`POST` accepts strict `ConverseModelChange`: schema 1.0, expected revision,
expected state, original idempotency key and complete selection. Inspection and
saving do not construct an adapter, read a key, discover/load a model, count a
prompt, probe an endpoint or perform inference. Qualification is `not_checked`
and readiness stays `unknown`.

The selection is either `host` with null catalog/reasoning overrides, or an
explicit catalog `id`, `generation` and `configuration_sha256` plus supported
reasoning. Labels and IDs alone cannot retarget a selection. The initial catalog
adapters are native LM Studio text streaming (`none` reasoning) and Meta text
with an explicit context budget. Other declarations stay visible but ineligible
until their actual streaming/count interfaces are implemented and qualified.

The protected `converse-model.sqlite` stores one role, at most 10,000 exact
receipts, bounded state/receipt JSON, a monotonic native revision and CAS state.
A nonblocking lifetime file lock prevents a second process from changing the
same role while the first owns inference or retained cleanup. This lock survives
logical shutdown until the last real operation lease settles. A refused second
owner or corrupt/unavailable store makes the role unavailable; it never silently
falls back to the host model. Catalog unavailability remains independent of a
valid host declaration.

The native change boundary is `settled_converse_operations`. New settings may
commit only while admission reservations, typed operations, window work and
buffered publication, speech, transcription, memory work, playback cleanup and
model leases are actually settled. Saving does not stop active work or force a
service restart. The final busy/CAS/selection commit has no await gap.

## Capture and provenance

A new typed turn or Decisions-approved window captures a fresh adapter and exact
profile/credential recipe. Metadata inspection is separate from this admission.
The same captured provider performs context projection, measured count, memory
guarding and streaming. Meta Converse adapters disable Agent structured actions
and use the Converse resource role. Existing content-disclosure and source,
correction, epoch, playback and owner controls remain authoritative.

At most sixty-four actual model leases remain owned. The producer, buffered
consumer, shielded memory/count/publication tasks, upstream iterator and adapter
close all belong to that lease. Timeout, cancellation or a terminal publication
alone cannot release capacity. Failed or unconfirmed cleanup remains retained
and prevents another selection; no replacement adapter is created as recovery.
The host adapter keeps its existing service lifetime.

New `conversation.user`, reply and window preparation/start/reply payloads carry
`RoleModelPin` as `role_model`: role, source, settings revision, exact catalog
reference/revision when applicable, provider, model, locality and reasoning.
It contains no credential. A model alias is configuration provenance, not proof
of verified weights or readiness. Historical events without this additive pin
are unchanged. Exact accepted replay precedes consultation of today's model or
credential and never starts new inference.

## Receipts and UI recovery

`GET /v1/converse/options/model-changes/{key}` returns only the original exact
receipt. A response timeout or missing receipt cannot prove a save failed.
Historical retry checks the original request digest and returns its receipt
before current eligibility/busy/CAS checks. A fresh save requires a fresh native
snapshot and a new explicit owner action.

The embedded Models page uses a Converse-owned client and owner identity,
independent of Agent and Session service availability. It shows unavailable
choices and live-operation blockers, keeps exact pending request identity in
bounded RAM, validates the complete receipt and refreshes the actual selected
provider's disclosure metadata. Changing a model grants no cloud disclosure or
capture permission. Unknown saves keep inference blocked until exact recovery
or an explicit same-instance advanced-revision fence releases only local
blocking; actual HTTP operation ownership remains until transport cleanup.

The existing setup endpoint remains a read-only declaration projection. Its
conversation and reasoning reflect the current selection; STT/TTS/Decisions
remain host declarations. The setup revision is not a save authority token.

## Evidence and remaining scope

Source review, generated schemas, formatting/static analysis and release builds
are distinct from exact-head CI, runtime observations and real model/device
qualification. Record each on the associated PR; source presence alone does not
qualify live conversation or close any of the twelve Options acceptance groups.
No new automated tests, paid calls, credential probes or model downloads are
part of this increment. The cumulative $10 test budget and existing uncertain
reservations remain unchanged. Ordered fallback, independent STT/TTS/Decisions
editors, direct multimodal capture and broader sampling controls remain work.
