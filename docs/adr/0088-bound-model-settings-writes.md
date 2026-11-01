# ADR 0088: Bound model-settings writes and uncertain recovery

Status: Implemented with local qualification; exact-head CI required before merge.
Tracking: [#191](https://github.com/rgoza1996/Kinaiya/issues/191).

## Requirement and failure

Roadmap §5.4 and [ADR0077](0077-options-workspace-and-harness-models.md) require
durable, idempotent owner changes and truthful uncertain outcomes. A pending
model save must remain attached to the authenticated authority that originated
it through navigation, timeout and reconnect.

The old Flutter registry uses only the Agent URL. A different owner or backend
at that URL can therefore inherit the old pending body and retry it using the
new client. Native revision/profile equality does not distinguish replacement
databases. Existing object-identity callback guards do protect replacement
records; they do not establish authenticated backend scope. Logical map size
also does not bound actual late transport ownership after logical settlement.

## Decision

The protected model-settings SQLite database owns a random, durable 32-character
lowercase hexadecimal backend ID. Each controller atomically registers a fresh
writer instance ID. Startup validates existing settings and identity and marks
the identity migration durably; missing/corrupt migrated metadata refuses
instead of silently creating another identity. Concurrent startup reads back a
single backend identity. A retired instance cannot re-register itself or clear
a newer writer when it closes.

An empty schema is initialized only after this constructor atomically created
the file with `O_EXCL`, or for an explicitly fresh in-memory database. An
existing empty/truncated file refuses startup instead of minting another
host-default selection and backend identity. Exact legacy two-table/version-0
stores still migrate without rewriting their settings, operation digests or
receipts; current three-table/version-1 stores retain their backend identity.
Creation and initialization hold a transient lock on the containing directory
with a five-second acquisition deadline, so concurrent native first startup
cannot race ahead of the actual creator's authority transaction. This is not a
new lifetime lease; the existing active-writer fencing remains authoritative.

Production uses the shared protected opener with the actual worker workspace,
including containment, ownership and file-identity checks. Older trusted
embeddings that supply a path without a workspace retain a separate supported
opener: it proves atomic creation and owned regular-file identity, and preserves
their existing conversion to owner-only permissions. That embedding branch
does not establish worker-workspace containment or replace production checks.

Inspection remains schema 1.0 with additive `write_authority`. New model changes
use strict schema 1.1 with required `expected_backend` and `expected_instance`.
Legacy schema 1.0 stays unchanged and is exact-persisted-replay-only. It cannot
create a new unbound mutation. Original canonical digests and receipts, including
the omitted null auxiliary purpose, are preserved.

Inside one serialized transaction, exact key/full-body receipt recovery comes
before current instance, revision, harness, profile and default-provider checks.
Recovery does not select a provider, assign the runtime provider, reapply saved
preferences or alter accepted task pins. A genuinely new application verifies
the database identity and both local/persisted active writer, checks current
revision and eligibility, then commits settings and receipt together. A 1.1
receipt carries the original backend and writer, never the restart generation.
Stored receipts require strict shape and canonical digest validation; corruption
is unconfirmed/unavailable, not absence.

An owner full-original-request lookup observes receipt, digest, identity, current
revision and writer together. It distinguishes recorded work from unobserved
work. An unobserved request is permanently fenced only if its writer is retired
or persisted revision is greater than its expected revision. Mere inequality
does not fence a future revision. A different backend cannot provide absence or
fencing evidence for the original. Lookup performs no harness/provider selection.

SQLite begin/commit failures retain uncertainty. If rollback cannot establish a
clean transaction, the connection is poisoned/closed rather than reused.

Flutter retains each immutable request within its original private authentication
scope, canonical Agent endpoint and stable backend identity. Credentials never
appear in registry keys, diagnostics or evidence. Another authority cannot view,
retry or settle it. Same-authority close/reopen/reconnect preserves the original
request, including its original writer generation. A changed instance alone
permits checking, not declaring success or failure.

Dedicated model-settings requests have separate result and actual-settlement
ownership. They refuse redirects and bound request/response bodies to 4096/16384
bytes, the result deadline to ten seconds and cleanup waiting to one second.
The UI waiting deadline is eight seconds. A timeout, cancellation request or
unconfirmed close does not release actual capacity. Global ownership retains up
to 32 outstanding mutation/lookup/cleanup owners even after logical receipt
settlement; there are at most 16 logical pending records, two concurrent mutation
owners and one lookup owner per record. These bounds are concurrent capacity,
not a lifetime retry budget. No automatic retry/polling is introduced.

Prior uncertain dispatch stays sticky through later refusals. A bare 404,
matching values or later 400/409/422 cannot erase it. Settlement requires a
validated exact receipt or same-backend native unobserved-and-permanently-fenced
proof. Display generation and object-identity guards remain independent.

The existing `/model` command uses observed bound authority for new saves and
native exact original-request recovery for retained inner receipts. Historical
command recovery returns its receipt explicitly, without pretending it selected
today's provider. An existing unresolved command intent permits only this
receipt recovery, never reconstructing a new write with today's authority.
Recovery bypasses skill discovery and uses a separate deterministic proof-audit
key, so a lost acknowledgment of the original audit cannot conflict with the
historical proof. Both original and recovery completions use digest-bound
NULL-only compare-and-set and return the stored winner. Audit precedes command
completion; an uncertain audit/save remains HTTP 503 with fixed diagnostics.

## Compatibility and trust boundary

Legacy/unsupported snapshot authority is read-only to the new client. Upgrade
must quiesce old binaries that ignore writer fencing. These guarantees assume
an authenticated native service and protected, unforked, non-rolled-back database
state. They do not authenticate arbitrary replacement servers, distinguish
cloned databases or provide a distributed execution lease.

Execution-settings behavior is preserved when separating its old shared Flutter
state type. Its adjacent pending-scope/transport issue is
[#213](https://github.com/rgoza1996/Kinaiya/issues/213); this repair does not claim
that issue or the full Options milestone is complete. Model writes still affect
new tasks only and cannot grant permissions or change disclosure policy.

## Qualification

Independent API/real-SQLite regression oracles were frozen against merged main
`6d9cbeb17f4935c82285ad79924eddc71682b898` before implementation. The baseline
has four positive controls and 36 failures; several failures stop at the absent
authority/lookup surface and therefore do not yet exercise their deeper fault
or concurrency assertions. Root command regressions add six baseline failures,
with their positive control and all 28 existing command tests passing. Raw
baseline logs and source pins are retained; these are defect evidence, not
qualification of the candidate.

The initial integrated 57-case run passed 47 and failed ten: three actual storage
faults were misclassified as ordinary refusals and seven public command cases
exposed the unresolved-intent/discovery/HTTP gaps. The native correction passes
53 owned plus the unchanged 40 independent cases. Root's command/auxiliary/task
receipt run passes 114 cases, and the independent command audit/uncertainty run
passes 14, including both audit/completion race winners and lost audit
acknowledgment. One new audit oracle initially expected the raw transport error;
its expectation was corrected to the required fixed uncertainty exception while
all actual-commit, fault, audit and compare-and-set assertions stayed intact.
Initial failure evidence is retained.

All 909 Flutter tests pass locally with unchanged source hashes. Real Dart-client
to production-native HTTP/SQLite checks pass new save, exact process-restart
recovery with the old profile removed, retired-unobserved fencing, future
revision non-fencing and replacement-database refusal on the same URL. The
foreign write and lookup both require actual HTTP 409; the original database has
one committed model receipt and its replacement has none. The three ephemeral
service processes have terminated. Providers/auth are synthetic; no inference,
user-service deployment, human voice or physical-device qualification follows.

Existing auxiliary/task fixtures move new saves to bound 1.1; 76 existing
assertions remain, with only two request-builder arguments changed for observed
authority and original-request restart recovery. Legacy 1.0 digest/receipt
assertions remain unchanged. See the [measured evidence record](../../evaluation/evidence/model-settings-authority-2026-10-31.json)
for source pins, commands, independent review and retained failures. All five
exact-final-head CI jobs remain required before closure; the broader Options,
voice and mobile gates remain open.
