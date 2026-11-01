# 0089: Owner-bound execution settings recovery

Requirement: [#213](https://github.com/rgoza1996/Kinaiya/issues/213), a reliability
repair within the full Options goal [#208](https://github.com/rgoza1996/Kinaiya/issues/208).
This extends [ADR0084](0084-owner-execution-settings.md). Source pins, retained
failures and observed checks are recorded in the
[evidence record](../../evaluation/evidence/execution-settings-authority-2026-10-31.json).
The linked PR records final-head CI and integration status; this repair does not
complete the full Options goal.

## Existing protections and remaining defect

Execution settings already bind revision and actual policy to an opaque state
token containing a fresh service UUID. A replacement service therefore refuses
a stale new request. The final SQLite transaction also rechecks state/revision;
neither protection is missing.

The pending client operation was instead indexed only by endpoint. Another
credential at that endpoint could see/retry it; a later refusal could erase an
earlier uncertain dispatch. Logical counters did not bound hidden actual late
transports after settlement or client replacement. An older native controller
also lacked a durable settings-writer fence if another opened the same store
while the older controller remained alive.

## Runtime-specific authority and compatibility

The protected runtime-settings database owns a durable random 32-hex backend ID
and an atomically registered active writer. The writer keeps the existing
36-character service UUID and existing state-token inputs. It is independent
of the model-settings database and its identity.

Startup serializes migration/validation and writer registration. An identified
legacy store can migrate once; missing or corrupt migrated identity refuses
instead of creating a new identity. Older binaries must be quiesced first.
An older controller can read historical receipts but cannot regain write
authority, clear a newer generation or apply another preference change.

The outer snapshot remains version 1.0, with additive `write_authority` containing
`mutation_version: "1.1"`, `backend_id`, `instance_id` and `writable`.
Bound mutation 1.1 adds required `expected_backend` and `expected_instance` to
the existing revision, state token, key, preferences and process-local YOLO.
Legacy runtime mutation 1.0 keeps first writes and exact replay: its state token
already binds the service. Its canonical digest and historical receipts receive
no new defaults or fields. This differs from model mutation 1.0 in ADR0088.

Exact key/full-request receipt recovery precedes current eligibility checks and
never reapplies historical settings or retires MCP connections. New writes
retain the integration lock, settled-service/worker/child cleanup gates, MCP
retirement, post-await check and final transaction check. No await separates
commit from synchronous native application. Deny/Ask/Allow, action approvals,
workspace containment and process-local YOLO behavior remain native controls.

## Proof and uncertainty

Full-original-request lookup observes the digest/receipt, backend, persisted
revision and active writer in one serialized transaction. A recorded result
returns the original receipt. Bound unobserved work is permanently fenced only
when its original writer is retired or the persisted revision is greater than
its expected revision. A foreign backend refuses; unreadable storage is unknown.

Busy state, directory changes, YOLO changes and state-token inequality can be
temporary. They do not prove that an older request cannot later commit. Neither
does a future expected revision, current value equality or a 404. Unobserved
legacy lookup stays unfenced because its opaque token cannot establish the
original backend/writer. Positive legacy recovery can verify the supplied full
request against its retained digest without inventing missing identity.

Storage/commit failures remain unconfirmed. Failed rollback poisons/closes the
connection; dirty uncommitted receipts cannot become later evidence. Stored JSON
is checked for duplicates, nonfinite values and excessive depth. Fixed HTTP 503
diagnostics do not disclose storage details.

## Client ownership and resource limits

Immutable pending requests, drafts and notices belong to the original private
credential, canonical endpoint and runtime backend. Credentials are compared
inside the API and never become registry keys or diagnostics. Same-scope reopen
or reconnect retains the original body/key; a restarted writer permits lookup
without silently rebuilding/reapplying the request. Another scope hides the
older operation without discarding its uncertainty. Navigation never retries.

Model and runtime adapters share a small owned JSON transport pool with at most
32 actual request/late-cleanup owners. Each domain retains at most 16 logical
pending operations, with two mutation owners and one lookup owner per operation.
Requests are bounded at 4,096 bytes and successful proofs at 16,384 bytes. The
total result deadline is 10 seconds including awaited cleanup, cleanup wait is
at most one second and the display deadline is eight seconds. A display timeout,
unconfirmed close, logical settlement or facade replacement does not release
actual ownership. Capacity is reusable only after genuine settlement.

The runtime registry also bounds retained draft scopes at 16. A clean idle scope
can be evicted; a dirty draft or unresolved operation cannot. If every retained
scope requires review, a new scope is visibly read-only until one is released.

There are no redirects or automatic retries. Injected clients require an
explicit dedicated transport factory. Ordinary Options metadata reads keep
their separate bounds; this is not a whole-application HTTP limit. A confirmed
receipt establishes a historical save; active values are refreshed separately.
Hosts lacking supported authority are visibly read-only for new UI writes.

## Verification and limits

The frozen independent API/SQLite suite recorded 12 passing controls and 29
failures on the original source: nine existing defects and 20 missing new
capabilities. All 41 cases then passed unchanged on the integrated native patch.
Native author checks passed 153 cases; full Flutter passed 929, and independent
client execution passed 42 focused cases. Existing tests remained byte-exact.

Actual Dart/native HTTP checks passed save, same-database process restart and
replacement at the same URL. Historical requested YOLO did not reapply after
restart; foreign write and lookup returned actual HTTP 409. Read-only SQL found
one original receipt and zero replacement receipts. All three owned fixture
processes exited zero with observed zero model calls. These are synthetic
protocol checks, not human voice or physical-device qualification. All five
exact-final-head CI jobs are a merge gate; their observed results belong in the PR.

The guarantee assumes an authenticated native service and a protected database
that is neither forked nor rolled back. It is a settings-writer fence, not a
distributed task/policy lease, general external exactly-once guarantee or live
deployment. Full Options, voice/mobile qualification and other roadmap work
remain separate acceptance gates.
