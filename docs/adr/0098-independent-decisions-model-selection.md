# 0098: Independent Decisions classifier selection

A bounded source increment of the Options goal #208. Decisions remains an
independent classifier and grants neither capture, disclosure nor Agent work.
This increment does not qualify model quality or multimodal transport.

## Owner and supported classifiers

The Decisions service owns `GET/POST /v1/decisions/options/model`, historical
`GET /model-changes/{key}` and exact-request `POST /model-changes/lookup` below
that Options prefix. Every route requires owner authentication and returns
`Cache-Control: no-store`. A caller must select this independent service URL;
Session's Decisions inspection API and Converse do not own this setting.

The strict selection is `host` plus a null catalog reference, or `catalog` plus
exact profile ID, generation and configuration digest. Catalog selection admits
only the existing typed Laya Jev adapter. Host selection preserves the configured
classifier, including explicitly labelled legacy host JSON classifiers. No
reasoning, speech, sampling, autoregressive output control or fallback is added.

Inspection and Save inspect bounded declarations only. They do not resolve a
credential, construct an adapter, discover/load a model, count a prompt, probe a
server or perform inference. Readiness is unknown and qualification not checked.
Existing Laya token-window and artifact constraints remain provider configuration.

## Storage and actual operation ownership

The protected `decisions-model.sqlite` has a durable random backend identity,
revision, exact selection and at most 10,000 original request/receipt pairs.
Initialization requires atomic new-file proof; existing empty, incomplete or
unknown schemas refuse. Startup validates bounded history and retained selection.
A lifetime nonblocking file lock survives logical shutdown until actual owned
operations and provider cleanup settle. It prevents another native process from
applying a competing role while this process owns classification.

Changes bind backend, service instance, revision, state token and original key.
Exact positive replay precedes eligibility and busy checks. Commit requires all
finalized/window preparation, classifications, health probes and real model leases
to settle. A save does not cancel work or stop an external/managed model process.
Failed/unconfirmed cleanup remains retained and blocks further admission/saves.

At most 64 actual model leases are owned. Each catalog classification captures
one coherent exact declaration and credential generation before constructing a
fresh Laya adapter. Later removal or rotation cannot retarget that operation.
The host adapter retains its existing service lifetime. Source/session/epoch,
correction/disclosure and resource checks still run at the actual export boundary.
Cancellation owns response cleanup through actual close; it cannot recall remote
inference or prove that a model process stopped. Resource lifecycle stays with
the existing managed resource controller.

An unreadable store, unavailable selection or failed catalog capture preserves
native backlog. It is not a fabricated Ignore result or automatic host fallback.
Window reuse binds selection/revision and catalog provenance in addition to the
existing transcript/task inputs. Original request wording, question types/options,
validated output and raw provider results remain unchanged.

## Trace and uncertain outcomes

An additive optional `DecisionsModelPin` accompanies real native call capture:
role/backend/instance, settings revision, selection, catalog revision and actual
provider/model/locality/protocol. It contains no credential. Historical captures
without the pin retain their original digest and remain inspectable. Reused
captures inherit the original call's pin. Capture retention/availability remains
observational and never grants work or fabricates missing request evidence.

Receipts prove only a historical selection save. Exact-request lookup returns a
positive receipt or an unobserved result; absence is not failure. Within the same
backend, a retired instance or advanced revision fences any new commit from the
unchanged original request. A different backend refuses reconciliation. This
supports later explicit UI tracking release, not cancellation or rollback.

## Evidence and limits

This increment is under source review. Static checks, exported schemas, exact-head
CI and real-model/device qualification are separate evidence. No new tests, paid
calls, credential probes, downloads or running-provider changes are implied.
The cumulative $10 budget and all earlier uncertain reservations remain intact.
Flutter role editing, explicit Decisions connection UI and broader role acceptance
are subsequent work; source presence alone does not complete the goal.

## Embedded owner interface

Flutter Models embeds Decisions selection with an optional independent Decisions
service URL in Connection settings. Existing profiles leave it unconfigured;
the client never derives it from Session, Agent, or Converse endpoints. Declaring
or losing this connection does not grant or revoke another role's inference.
Owner metadata reads are separate, bounded, and fenced by connection generation
and client identity. They establish neither readiness nor model qualification.

The selector distinguishes host configuration (legacy compatibility) from exact
typed Laya catalog generations. It has no speech, reasoning, or sampling controls.
An unavailable retained generation remains explicit rather than retargeting to
the latest declaration. Metadata refresh preserves owner drafts; a changed native
backend, instance, revision, or state token requires explicit draft discard.

At most 16 owner drafts and 16 unresolved exact saves remain in RAM. The first
POST rechecks its rendered choice after synchronous state notifications. Every
retained retry first looks up the exact original request. A validated receipt
confirms that save; a positive native mutation fence permits only explicit local
tracking release, leaving the historical outcome unknown and actual HTTP cleanup
owned. Transport owners have independent per-client/global bounds across
reconnections. The existing journal Decisions inspector remains unchanged.

This interface is implemented in source and statically analyzed. CI, preview
observation, classifier qualification, and real model/device calls are separate
verification gates; none is implied by this section.
