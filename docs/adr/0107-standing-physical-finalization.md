# ADR0107: Retained physical Resources finalization

Status: in-process implementation qualified in the offline harness at
`40c239e8721340a8764a54d2b1997fdca983dd16`; main integration and exact-head CI
remain pending for #239 / #228. This increment provides an in-process producer;
Agent-triggered finalization, claim-wide Agent
retirement, native settlement, recurrence and inbox delivery remain separate.
It does not establish M7, a live model stack or demo readiness.

## Boundary and authority

`ResourceCoordinator.finalize_standing_claim(StandingResourceFinalizeRequest)`
returns the existing `StandingResourceFinalizationReceipt` only from genuine
current-instance prework coverage, native admission closure and physical
ownership evidence. `lookup_standing_finalization` reads exact committed native
history; it cannot restore, adopt old state or manufacture a report. Neither
method is a new HTTP route or a permission granted by Decisions/model output.

The producer uses the existing shared 64-operation barrier and Resources-role
Session client. All six public packets, canonical digests and byte limits stay
unchanged, including full packets larger than 262,144 bytes. Revision values
must fit the existing wire maximum before covered effects; unavailable
testimony must not disable independently authorized owner Off.

## Durable chronology

The protected action ledger gains `standing_resource_witnesses`, bounded to
10,000 lifetime claims with no eviction/backfill. Its exact schema, row types,
raw bounds, file/controller identity and complete canonical joins are checked
before acceptance. Private fields retain the original binding, action key,
reservation, lease, phase, acquisition/effect flags, restoration, revisions and
immutable full report. Schema disagreement or missing/corrupt coverage refuses
new positive testimony.

Fresh coverage commits before the native binding. Exact pending action,
digest and reserved lease commit before either existing effect latch, including
preemption. Acquisition commits only after actual original release, fresh
configured headroom checks and the exact local-work state save. A gap or
cancelled facade remains uncertain. Null lease metadata cannot stand in for
never acquiring resources.

Already admitted legacy work with no local binding may use its old authorized
handoff as uncovered work. It gains no report coverage or first-launch
authority. A corrupt/missing witness for an existing local binding cannot
downgrade to that compatibility path.

## Original owners and independent Off

Matching ordinary restore, task-finished and owner-Off operations enroll before
clearing lease identity. Their original Manager spawn/reaper, selected endpoint
streams, control requests/reads/sole closes and native database owners remain
retained, including children created later. New and reused lifecycle scopes
hold an initial root before enrollment and the first pending SQL await.
Initialization failures retain uncertainty and the original children.

Wire-revision requirements belong to the actual invocation context, not a
sticky flag on a reused ownership scope. Earlier covered work retains its
checks while independent Off remains usable. Off/ready state saves and bounded
publication returns are candidates, not physical proof. Failed acknowledgment
or actual cleanup stays sticky even after public IDs or registries clear.

Positive restoration captures exact original worker identities and selected
endpoint bindings. Readiness/control observations are rechecked after awaited
publication and original-owner joins, with a synchronous guard before report
COMMIT. An exited/replaced worker cannot satisfy an old restoration witness.
This is an observation at publication, not a promise of future uptime.

## Final report and replay

Finalization drains matching original owners outside lifecycle/admission/SQL
locks and never restores unrelated same-ID ordinary work. It reconciles the
full native context with durable prework/acquisition/restoration evidence.
Only then does the immutable full report commit locally, before native report
publication. Positive return also requires the original response's sole close.

A local report cache cannot bypass an earlier pending publication close or
failed owner. Lost acknowledgment may reconcile the exact frozen report/key
without repeating physical effects. Restarted controllers may read explicitly
committed native history; pending, local-only, uncovered or failed work remains
unresolved. Shutdown fences new production and drains original owners before
retiring their dependencies. Private collaborator replacement is not qualified
as a general reconfiguration/retirement protocol by this increment.

## Verification scope

Independent contract cases and separately identified review-derived regressions
use actual temporary SQLite/ASGI services, disposable CPU workers and original
HTTP/stream/COMMIT/reaper handles. Source-review findings, initial failing runs,
fixture maintenance and later qualifications must remain separately recorded.
An empty registry, terminal task or mocked result alone proves no physical
retirement. No model accuracy, inference, live GPU/device operation or preview
change is part of this qualification.

The [qualification record](../../evaluation/evidence/standing-physical-finalization-2026-11-01.json)
retains the full original 53-pass/two-timeout run and the corrected 99-case
physical/ownership run, alongside 1,291 passing contract/client cases and
unchanged schemas. These groups overlap. The two original eight-second gates
pass after removing repeated semantic reconstruction only for freshly parsed
local witness snapshots in one synchronous call; every encoding still walks
the complete actual graph. Public parsing, caps, canonical/native joins and
all checks after awaits remain unchanged. This is no general timing guarantee.

Source-aware fixture corrections positively join original owners before retry
and observe the actual fence before releasing held work; their old assertions,
expected dispositions and finalization deadlines are retained. A separate
failed-Off race first reproduced false-positive publication, then passed with
durable failed-witness preservation. Passing cleanup-negative cases preserves
uncertainty rather than proving healthy retirement. PR259's failed worker-start
CI remains unresolved and is not cleared by these local checks.
