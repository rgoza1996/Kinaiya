# ADR 0102: Native installed capability administration

November 1, 2026. Source increment for #208. Source analysis and synthetic CI
remain separate from real model, extension and device qualification.

## Native availability

Agent owns a protected availability mask independently of execution permissions,
disclosure and the actual configured host. It can withhold individual native
operations; group controls are a presentation over the same canonical mask.
It cannot install a gateway, widen permissions, modify an evaluator or disable
`finish`. `NativeToolSnapshot` keeps its legacy constructor invariants unless an
explicit native owner mask and revision are supplied. Production intersects the
mask with the actual task-qualified host snapshot. The request-local prompt,
provider wire, catalog, action-membership fence and final dispatch use that same
snapshot. Explicit new skill invocations refuse before acceptance when loading
is unavailable. Existing tasks are not silently restarted or rebound.

Availability changes require genuinely settled Agent work and extension
operations. Skill/plugin disable keeps existing native revocation behavior;
revocation cannot recall content already exported or claim that a running child
stopped. Plugin disable can remain responsive during evaluation, which creates
evidence but does not activate code. It cannot overtake unresolved activation.

## Owner inventory and installed actions

Owner inspection includes disabled and disclosure-blocked installed records.
It does not use the provider-filtered model tool catalog as an installed list.
Each section is separately readable without inference, source loading into a
model, dependency installation, transport probing or key resolution. Missing
model metadata yields unknown provider-specific eligibility. Availability,
installation, enabled state, approval and authorization remain separate facts.
`requires_action_checks` never establishes permission for an action.

Skills expose safe source/version/digest, compatibility, invocation flags,
disclosure scope and dependent task identities. List integrity is explicitly
registered-only; native enable verifies the exact installed package again and
refuses unsupported setup/adapters. Installed enable/disable neither executes
instructions nor changes a disclosure grant.

Plugins expose verified immutable source/manifest identity, current pointer,
enabled/approved state, owner-suite presence, rollback eligibility and bounded
report summaries. At most eight report summaries per version are returned, with
the total separately disclosed. Owner-suite inputs and paths are not returned.
Directory enumeration is bounded before opening version or report metadata;
each loaded report is immediately reduced to validated source-bound summary
fields rather than retaining private case bodies. Whole-row page packing stays
within64KiB without truncating a row or advancing past an omitted row.
Evaluation is an explicit isolated native computation using the configured
owner suite. Promotion separately requires the exact passing report, unchanged
source/manifest/suite binding and explicit owner review. Rollback names the
exact current previous approved target. Disable retains native pin revocation.
No installer, arbitrary dependency configuration or model-authored approval is
introduced. Missing retained activation/pin files refuse plugin startup rather
than manufacturing an empty active registry.

## Protected changes and uncertain outcomes

`agent-capabilities.sqlite` has two exact version1 tables, a durable backend
identity, process instance, monotonically increasing revision and at most
10,000 exact change records. Only affirmative atomic creation initializes it.
A bounded startup directory lock spans creation and initialization; an owner-only
lifetime file lock prevents a replacement writer while actual operations remain.
The path is outside the worker workspace and rejects aliases. Existing empty,
corrupt or unsupported stores never become a default enabled mask.

Owner endpoints are `GET/POST /v1/agent/options/capabilities` and
`POST /v1/agent/options/capability-changes/lookup`. GET takes an independent
tools/skills/plugins section and pages of at most eight rows. Writes carry the
complete original action, backend/instance/revision/state CAS and original key.
Tools settings and their receipt commit together. Installed actions durably
reserve that identity before touching native lifecycle state. The real operation
is independently retained through caller cancellation and workspace changes.
Shutdown joins those operations before closing their services or store.
Pointer-write threads remain owned until their actual completion even when a
caller or parent operation is cancelled. Pending summaries contain unique
canonical key/operation pairs, both explicitly included in the state token.

An exact successful receipt replays without repeating an effect. An error after
reservation remains pending/unknown: a pointer or report may already have been
written. Lookup never executes an operation, and matching current state is not
proof of the earlier outcome. Pending extension outcomes withhold their Agent
gateways and block competing activation. Unobserved original CAS requests can
be fenced by a retired native instance or newer revision of the same backend;
pending recorded intents cannot be released by that argument. Recovery of an
unresolved multi-file lifecycle intent remains manual native reconciliation;
there is no false reset or automatic effect replay.

Old skill/plugin lifecycle mutation endpoints and slash-command mutations are
explicitly fenced when this production owner is configured. Existing inspection
and direct isolated computation keep their native checks. Trusted legacy
embeddings without this owner retain their previous interfaces. Installation,
update, removal and scope editing require a future bound lifecycle extension;
they cannot bypass the new owner through the legacy endpoints.

Requests are bounded to16KiB with one five-second read deadline, responses to
64KiB and full inventory fingerprints to8MiB. Routes are owner-only and no-store.
Static checks are reported separately; implementation performs no test, suite,
model, network or device calls.

## Embedded Options and original-request recovery

Tools, Skills and Plugins use traditional embedded pages. The full installed
inventory is separate from the model catalog. A saved requested mask, actual
host availability, effective lifecycle withholding and action authorization are
displayed independently. Plugin promotion requires review bound to the exact
rendered version and selected passing report; changing reports clears review.

Bounded owner-scoped RAM retains dirty tool drafts and every unresolved original
request across navigation and same-owner reconnects. A native receipt settles
only its exact action/body/key; refreshes and unrelated receipts do not discard
other edits. Unknown requests must be looked up before an explicit unchanged
retry. Pending native intents never re-execute. Only an explicit unobserved
native fence releases local tracking, leaving the historical outcome unknown.
Actual HTTP owners retain their bounded capacity until real response/cancellation
and client cleanup settle, independently of local recovery-card release.

Plugin Disable remains separately available after a fresh native snapshot when
all retained same-section intents are evaluations. The client and UI keep both
originals visible and recover them independently; the native owner rechecks the
authoritative pending operation kinds, exact current CAS and immutable target.
Activation and new evaluation remain blocked while any competing intent exists.
