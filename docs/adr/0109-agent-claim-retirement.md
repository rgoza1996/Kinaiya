# ADR0109: Current-process Agent claim retirement

Status: implemented and qualified in the offline harness at
`0cdf7a87d7468f7427263434472312cdd9fd3d75` for #239 / #228. The
[evidence record](../../evaluation/evidence/agent-claim-retirement-2026-11-02.json)
records 30 new checks and 414 affected existing regressions on identical
production bytes. Applicable exact-head CI remains required before merge.
This increment joins actual Agent work before automatic Resources finalization;
it does not settle native Tasks or release execution claims or active slots.

## Original authority and bounded ownership

Agent retains one current-process owner before its first standing preflight
lookup. It captures the frozen original intent, exact Journal and Resources
clients, original policy/configuration dependencies and restricted closure
capability. At most 64 live or uncertain claim owners and 128 captured client
dependencies are retained. Capacity refusal grants no replacement authority.
Healthy retired owners can leave the active hub; failed cleanup remains sticky.

Initial retained-proof lookup remains read-only. It cannot allocate a model,
adopt another process's work, bind Resources or manufacture retirement from
historical records. A genuine direct-created admission can consume one original
launch latch. Separate retained bind/admit children capture and validate genuine
late replies before launch guards; cancellation cannot turn a late reply into a
new worker. Restricted closure authority survives consumption of launch authority
and never travels to Resources or appears in public state.

The local fence synchronously prevents new effects and standing controls.
Controls such as interrupt, steer, approval resolution/renewal and revocation
run in distinct bounded retained children. Their external caller is not enrolled
in the work scope: a caller may remain waiting after its control returns without
making finalization wait for that caller. The original provider complete/count
request remains in the same worker Task, preserving its request-local context.

A resolved retained admission remains knowledge while an unused model is closed.
Suppressing launch always clears active/capability state. Only that already
resolved, unlaunched path avoids signaling a facade abort during cleanup;
cancellation, deadlines, shutdown and guards retain their aborting behavior.
A retained foreign initial still cannot establish positive local retirement.

## Work, finalization and uncertain replies

One creation root retains actual preflight, worker, model-lease cleanup,
control/publication children and native HTTP send/read/sole-close owners,
including children created later. Selected Session, history, complete-entry and
speech-window metadata reads reuse the existing Journal exchange registry.
Ordinary unscoped paths stay unchanged. Owned metadata has a defensive 1 MiB
response budget; the two context methods retain their existing 512 KiB raw JSON
limit, duplicate/coercion checks and complete native request/result validation.

The separately retained finalizer is outside the work scope it joins. Its own
metadata exchanges have a different rooted scope, so it cannot wait for itself.
A missing binding reply is reconciled only after the exact original bind work
and sole close settle. A bounded facade or a premature null observation is not
proof that binding failed.

After a genuine binding, the finalizer obtains permanent native admission
closure and the matching Resources seal on the captured original dependencies.
Those metadata fences do not prove Agent retirement. Physical finalization
requires positive joins of all original Agent work and healthy original model
and HTTP cleanup, complete closure/created-proof agreement and no foreign-owned
initial. A terminal Task or empty registry is insufficient.

The full immutable request uses `agent-standing-finalize:` followed by the
original claim digest as its stable key, plus the genuine binding's backend and
instance. The existing Resources client validates the complete receipt against
that original request and binding/closure. A lost final reply reconciles the same
request through native history after original cleanup; it cannot repeat physical
restoration under a new key. Failed cleanup supplies no positive report and
cannot be repaired by a later unrelated healthy close.

## Shutdown and compatibility

Shutdown fences new work, retains the same finalizers through their original
children, then closes dependent services and captured clients. Each original
client has one retained closer; cancellation or replacement cannot substitute a
new closer. Launcher Journal retirement waits through finalizer gaps between
RPCs. Workspace accounting includes pending and failed owners even when a
particular transport registry is momentarily empty.

For these standing records, the old per-worker `task_finished` restoration call
is unconditionally suppressed. Ordinary ResourceCompletion behavior remains.
An incomplete binding test double does not regain early restoration authority.
The affected old standing positives instead require the real automatic native
report after original model cleanup, preserving work truth, file evidence,
ordinary tests, negative checks and existing deadlines.

## Verification and limits

Qualification uses real temporary native ASGI/SQLite services, disposable CPU
owners and controlled inference outputs. It tests orchestration and authority,
not model accuracy or conversation quality. Source-aware fault observers only
surround original work and delegate to it; a manually fabricated final report
cannot satisfy the positive path. The evidence record distinguishes frozen
oracles, later source-aware controls, original failures, fixture calibration,
independent review and applicable exact-head CI. Overlapping runs are not added
as unique coverage.

The subsequent [validation qualification](../../evaluation/evidence/native-report-validation-2026-11-02.json)
retains CI441's two Python 3.12 timeout failures and the follow-up source
`15fbc836`. That source removes only duplicate synchronous reconstruction after
the same complete graph and native context have already been validated; fresh
pre-commit observation, transaction guards and public receipt validation remain.
All 188 affected local regressions passed with unchanged deadlines. The baseline
also passed the two cases locally, so no causal timeout repair or performance
claim follows. The changed publication head still requires passing CI.

The following [fresh-graph reuse qualification](../../evaluation/evidence/native-report-fresh-reuse-2026-11-02.json)
records source `5bae42ea`: the complete Context constructor replaces two
immediately repeated validators, and `_schema` locally encodes five freshly
parsed or constructed operands without recursively revalidating those same
private graphs. Every encoding still walks the complete actual graph. Public
validators, old native-state bridges, fresh observations, pre-commit passes and
transaction/cleanup guards remain unchanged; there is no cross-call authority
cache. All 188 unchanged regressions pass, with independent source and artifact
review. CI442 still failed the same two eight-second Python 3.12 cases on the
earlier `102913a7` head; iOS never acquired a hosted runner. These results remain
separate from the new local qualification. Changed-head CI is required; no
timeout cause or causal timing repair is established.

The later [Resource graph-walk qualification](../../evaluation/evidence/resource-finalization-graph-walk-2026-11-02.json)
records clean `970752b` with 251 passing checks: 63 Resource contract cases and
the same 188 affected regressions. `_fields` performs one complete actual-graph
scan at its checked entry; its lexical recursive helper retains every exact
model/dictionary/typed-child and strict scalar check. Complete encodings,
constructors, public validators, nested caps, old-kind bridges and native guards
remain. Independent source and raw-evidence reviews found no issues; all 192
old schemas and 336 old test paths are unchanged. There is no cross-call cache
or concurrent-mutation snapshot guarantee. The evidence retains terminal
CI456's two earlier Python 3.12 timeouts and distinguishes actual recovered
logs from missing earlier receipts. Changed-source CI remains required; this
local qualification makes no timing-repair, H64 or full-roadmap claim.

The original oversized conversation fixture was invalid: Store rejects a
serialized AppendRequest above 65,536 bytes. Its failed run is retained rather
than called a client-capacity regression. Corrected controls exercise exact
65,536-byte acceptance and 65,537-byte rejection through the real Store. The
initial uncapped-client review assumption is withdrawn; native limits and the
bounded transport remain enforced. Missing fixture registrations are separately
recorded as fixture changes, without changing existing test assertions.

Current-process ownership cannot be reconstructed after restart. Native Task
settlement, execution/reservation and active-slot release, repeated scheduling,
inbox delivery and the owner editor remain subsequent work. This increment
establishes neither M7 completion nor a model, live-device or demo qualification.
