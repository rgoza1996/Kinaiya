# 0093: Task-captured auxiliary fallback routes

October 31, 2026. Native and embedded editor source is implemented under review
for [#208](https://github.com/rgoza1996/Kinaiya/issues/208). Static source review
is distinct from exact-head CI and preview verification, which remain pending.
Neither source presence nor this ADR establishes provider qualification, preview
promotion, or completion of an Options acceptance group.

## Scope and selection

This extends the five text purposes in
[ADR0082](0082-task-pinned-auxiliary-models.md): `analysis`, `compression`,
`title_generation`, `delegation`, and `harness_design`. A purpose may have an
ordered list of one to three distinct, exact catalog generations, globally or
for an exact harness version. A harness route replaces the global list. Reset
removes that exact override and restores inheritance. When no ordered route
exists, the existing single-model selection remains applicable.

Primary Agent, Converse, Decisions, STT, TTS, and media routes are not covered.
An auxiliary result remains untrusted text, without tool authority, task-effect
evidence, automatic compaction, or a child Agent. Independent primary work can
continue when auxiliary route state is unavailable; unknown ordered overrides
disable auxiliary selection rather than silently substituting legacy bindings.

At new-task capture, the native owner reads each route's candidate recipes and
credential generations in one catalog read transaction. The captured generation,
configuration digest, reasoning, catalog revision, and private key material
cannot be replaced by a later alias or credential rotation. Candidate adapters
are constructed lazily, freshly, and only from those captures. No save or
metadata read constructs a model adapter, connects, counts, or runs inference.
This builds on [ADR0089](0089-agent-catalog-models.md).

The accepted task's auxiliary pin remains the initial selection. Actual attempts
and the winner carry their own exact pins in trace events and returned evidence.
After a predecessor is retired, later calls on that accepted task continue from
the current candidate; they do not reopen the failed predecessor. Recovery does
not reconstruct live adapters or keys from historical trace text.

## Advancement and disclosure

Advancement is permitted only after a measured context overflow, a candidate
that cannot accept the current disclosure, or a native typed pre-generation
availability proof. Current proofs are LM Studio connection refusal or an
affirmatively absent loaded model, and Meta count-endpoint HTTP429/503. They
prove only that generation did not start for that operation after real cleanup
settled. They do not prove zero count/export usage, a refund, or model readiness.

Typed proofs are off by default. Only task-owned auxiliary route construction
opts in; primary and Converse adapters retain their existing error behavior and
binding identities. Native configuration, disclosure, source, task, and resource
guards are checked again before a proof can permit advancement. Cleanup failure
cannot be converted to availability. Generic errors, authentication failures,
refusals, malformed protocol data, timeouts, cancellation, an unknown generation
outcome, or uncertain journal confirmation stop the route instead of advancing.

`cloud_scope` is a maximum disclosure ceiling, never a permission grant. Local
candidates use `local_only`; the current Meta Contributor route accepts only
`public_synthetic`. Combining that ceiling with native task/source floors can
only restrict a candidate. Retained foreign tool, skill, or plugin context still
requires a local auxiliary model. Exact count and generation exports use the
existing native source-admission contract and final request-bound transport
checks. A model declaration does not grant cloud disclosure or resource access.

Before advancement, the actual adapter closes under the task lease. Cancellation
does not release an unfinished close. Retiring close operations remain counted;
failed cleanup is sticky and blocks fresh task capture through the shared lease
gate. A later redundant close cannot erase an earlier unconfirmed cleanup.

## Protected state and owner API

The route database is derived from the full settings basename, for example
`agent-models.sqlite.auxiliary-routes.sqlite`. It must be an owner-only regular
file outside the worker workspace, without symlink aliases. The ambiguous
pre-release stem-derived filename is not migrated or treated as empty: observing
it makes ordered route state unavailable until an owner resolves that authority.

An empty schema is initialized only after affirmative native creation with
`O_CREAT|O_EXCL`, or for a new in-memory store. Opening an existing file does not
recreate it if it disappears, and the open file's protected inode is verified
against the database path. An existing empty or truncated file is unknown
authority, not a new empty configuration; it makes auxiliary routes unavailable
without falling back to legacy bindings. This creation proof is opt-in for the
route store; existing default callers of the storage helper retain their behavior.

A legacy embedding that supplies a persisted model-settings path without a
workspace authority does not create or read this new store. It permits unchanged
single-model choices only while native `lstat` affirmatively finds the route file
absent. A present file, dangling symlink, or inspection error becomes sticky
unknown state. No fake empty ordered-route snapshot is returned.

Owner-only routes below `/v1/agent/options` are:

| Method and route | Meaning |
| --- | --- |
| `GET /auxiliary-routes?purpose=…&harness_version=…` | Inspect own/effective route, source, native backend/instance/revision/state, writable state, and at most 32 catalog declarations. |
| `POST /auxiliary-routes` | Save/reset one exact scope using `AuxiliaryRouteChange`. |
| `GET /auxiliary-route-changes/{key}` | Read the historical committed receipt, without reapplying it. |
| `POST /auxiliary-route-changes/lookup` | Look up the unchanged original change and its native mutation fence. |

Changes bind `expected_backend`, `expected_instance`, `expected_revision`,
`expected_state`, exact scope, full ordered route, and idempotency key. Historical
exact replay is checked before current eligibility/CAS. New changes atomically
commit route, incremented revision, original request, and exact receipt. The
change boundary is `new_tasks`; accepted tasks keep their captures. Conflicting
state returns HTTP409, refused changes HTTP400, and unavailable or unconfirmed
storage HTTP503. Request-schema validation can return HTTP422.

HTTP404 receipt lookup is not proof of failure. Lookup distinguishes `recorded`
from `unobserved`; its explicit `mutation_fenced` can prove the unchanged old
request cannot make a new commit. It does not establish that a previous commit
failed, undo a change, cancel a request, or release actual transport ownership.
Models metadata exposes `auxiliary_routes_state`; readable/writable state is
separate from provider `qualification:not_checked` and `readiness:unknown`.

The embedded global/per-harness editor retains the original scope, ordered
generations, CAS, and body/key for an unconfirmed save. A retained retry first
requires exact native lookup; a confirmed receipt clears only its matching
submitted draft. Unrelated edits remain retained. A dispatched HTTP400 refusal
without an exact receipt is conservatively unknown because the current response
does not carry typed proof that mutation was impossible. Without an actual
native revision/instance fence, local tracking remains retained. This can delay
editing after a refused change; response status alone is not used as proof of
failure or permission to mint a replacement request.

## Trace and bounds

`agent.model_attempt` uses the closed `AuxiliaryAttempt` contract. Its phases
capture route, attempt start, export admission, attempt finish, and route stop.
The trace contains the logical request ID/hash, exact candidate pins, route
revision/hash, opaque credential references/generations, request/output hashes,
and generation/cleanup disposition. It does not contain prompt text, endpoints,
or secret values. Session validates the canonical accepted submission and initial
pin; later events must reference its captured route and exact ordinal/pin.

Each append request is frozen before dispatch. Missing acknowledgment triggers
bounded exact readback; an unconfirmed append remains retained and stops the
route. It cannot authorize another inference, a different result append, or
advancement. This is validated DTO/body confirmation over the existing journal
client, not a new strict raw-wire journal transport guarantee.

| Bound | Native limit |
| --- | --- |
| Saved routes / exact save receipts | 1,000 / 10,000; no silent eviction |
| Candidates / purposes per task | 3 / 5, at most 15 captured recipes |
| Task catalog leases / concurrently owned adapters per lease | 64 / 6 including primary |
| Logical call deadline | 120 seconds across the chain; real cleanup can remain owned longer |
| Trace records / export admissions per logical call | 24 / 16 |
| Calls per task route | 1,000 |
| Prompt or returned text | 8,000 characters and 16,000 UTF-8 bytes |

## Evidence and remaining work

The strict request, route, and trace roots are registered explicitly in
`scripts/export_contracts.py`. Source review, schema export, formatting, and
static checks are distinct from tests and exact-head CI. No new automated cases,
local test execution, paid probes, or model qualification are implied here.
Consult the PR for actual evidence. The native routes and embedded editor have
been statically reviewed as source; exact-head CI and preview verification
remain pending. Broader role, modality, spending, and fallback obligations remain
in the [delivery plan](../options-delivery-plan.md).
