# ADR 0032: Recover playback generation from native durable metadata

Status: implementation for [#79](https://github.com/rgoza1996/Kinaiya/issues/79),
following the retained stop ownership in
[ADR 0019](0019-streaming-window-control.md). Independent final review and
integrated CI remain release gates. This does not complete M2 or M3, change a
provider/default, or establish physical audio behavior.

## Problem and contract

The previous startup inferred the highest generation from `recent(limit=100)`.
A completed generation-1 reply followed by 105 ordinary speech windows lost its
counter on runtime reconstruction. WAIT then emitted `0 -> 1`, which a client
already at generation 1 ignored. This was reproduced at baseline `6336484` and
the completed-provider WAIT repair `5cbddb`; it was not introduced by #75.

The native journal now exposes `playback_generation(session_id, thread_id,
epoch)` through its Store, in-process adapter, declared Journal protocol and
HTTP client. `GET /v1/transcript/playback/generation` requires owner or Converse
authentication and those three scope parameters. A strict `PlaybackGeneration`
wire model returns only `schema_version`, the scope, and `maximum_generation`.
It carries no transcript body, prompt, credential, whole entry or pagination.
The exported JSON schema is part of the additive version-1.0 contract. Converse
requires this capability; an old/unavailable journal cannot silently fall back
to a recent-history guess.

The Store validates the current Converse session/thread/epoch before and after
the scan under its existing lock. The query uses native database columns for
scope and authenticated `writer='converse'`, restricted to:

- `conversation.user` and `conversation.converse`, including typed turns,
  speech-window replies and independently reported Agent outcomes;
- `converse.safety` and `converse.window_state`, including older restart and
  started/error receipts;
- `delivery.updated`, including older interrupts and current stop instructions.

The maximum covers `generation_epoch` on those kinds and
`next_generation_epoch` on delivery updates. No `runtime_id`, control tag or new
record version is required for old retained records. Native `writer` is crucial:
Converse-written user entries legitimately have `origin='authenticated-user'`.
An owner-supplied payload or unrelated kind/session/thread/epoch cannot allocate
this generation. Selected native records must also have consistent stored
event identity, scope, kind and projected origin.

Absent/null counter fields mean no allocation, as in preparing/error receipts.
Present non-null booleans, floats, strings, negatives, arrays, objects and
unrepresentable integers fail the read, rather than lower the recovered maximum.
Malformed selected metadata, database errors, a reset or a foreign response scope
also fail closed. Malformed unrelated writer/kind records are not inspected.

## Allocation and reset

The numeric range is `0..9_007_199_254_740_991`, the exact integer range shared
by JSON numbers and Flutter web. Zero denotes no prior allocation. Startup and
warm allocation refuse overflow. At the last representable generation a new
client barrier cannot be truthfully allocated; the operation errors instead of
publishing an unsafe number. Local cancellation and chunk invalidation still
occur, without claiming client/device stop. An owner-controlled native epoch
reset permits a new scope to start afresh; no automatic reset is performed.

Converse rechecks the response scope and native session before constructing
restart state, and retains the existing final session check before exposing
playback. The #75 immutable start/stop requests and lost-ack reconciliation are
preserved. The maximum is recovered on cold scope initialization, independently
of bounded dialogue retrieval.

Root review caught an interaction with the new overflow guard: discarding a
cached exhausted old scope still called `begin()`, which could prevent recovery
after a valid native reset. Twelve retained regressions failed for cached and
pending-start ownership with exhausted/invalid old counters over local and HTTP
adapters. Allocation-free invalidation now cancels local playback and clears
chunks when that owned obsolete state is discarded under the epoch lock. It
does not increment the old counter. Late asynchronous stop retirement remains
callback-free, so it cannot cancel speech belonging to a newer scope. The same
12 cases passed after the correction.

## Database work and evidence limits

There is no database schema migration, checkpoint, new index or larger recent
limit. One native metadata query streams rows through an existing scope index;
the response is bounded, but database work grows with retained session history.
On Python 3.12.3 / SQLite 3.45.1, `EXPLAIN QUERY PLAN` selected the existing
`sqlite_autoindex_entries_2 (session_id=? AND writer=?)`. Synthetic in-memory
observations, including both session reads, were:

| Retained native-writer rows | 50 rows | 2,000 rows |
| --- | ---: | ---: |
| Unallocated `converse.window_state` metadata | 3,847 VM steps | 150,077 VM steps |
| Excluded `conversation.window` records | 747 VM steps | 26,077 VM steps |

The scope-only response was 160 bytes in those cases. These are operation-count
observations, not elapsed-time, throughput, disk or sustained-load acceptance.
A checkpoint would add transaction maintenance and old-history validation/
backfill responsibilities; it is not required for this correctness repair.

Tests exercise every native generation field, old metadata, local/authenticated
HTTP boundaries, writer and scope isolation, malformed counters/identities,
read failures, reset races, numeric limits and restart without provider replay.
The first fixture run had 98 passes and three fixture failures: a non-entry fake
chunk and an overly specific expected index name. Those fixtures were corrected
without production changes; the actual existing unique index remains in use.

The byte-identical retained 105-window native regression and production
JavaScript audio-bridge probe now produce `1 -> 2`: one WAIT stop clears the
held synthetic queue, and an explicit later interrupt is a positive control.
The JavaScript control-field projection is source-derived from Flutter; this
probe does not execute its widget handler or establish physical audibility.
No model inference, paid call, live reference service or private prototype data
was used. Focused cross-version results and source/probe hashes are retained in
[the evidence record](../../evaluation/evidence/playback-generation-recovery-2026-10-28.json).

The same affected scope passed 339 tests on Python 3.12.3 in 102.11 seconds and
339 on Python 3.11.15 in 91.07 seconds. It includes the new generation tests,
session/contract checks, window recovery/HTTP/speech integration and provider
unit tests. Both reported the existing Starlette/httpx TestClient deprecation
warning. Imports were verified to this worktree, rather than the shared venv's
editable checkout. Ruff lint and 169-file format checks passed; schema export
adds only the new wire model, and `git diff --check` passed. Full integrated CI
and independent final review remain pending outside this implementation check.

Independent final review at `116afe5d` passed 113 cases in 24.77 seconds, with the existing TestClient warning, plus its own native local/HTTP/auth/reset/corrupt-writer probes. Lead integration with #76/#77 passed 449 cases in 123.46 seconds. A separate independent 16-case actual-adapter probe verified exhausted-scope reset while old HTTP cleanup and a native append were held: ownership remained retained, overlap was refused, fresh work began after confirmed close, and the late old write could not disturb its owner. The pending-start variant explicitly arranges that registry boundary around the real producer. Source bytes remained unchanged. Exact-head CI is still required before merge; full commands, hashes, fixture corrections and limits are in the verification record.

Initial final-head CI40739403204 exposed three obsolete-fixture failures on both interpreters: the MCP in-memory journal lacked the required scalar method, and a serialization test counted the replaced recent-history read. The fake now implements strict scope/writer/counter recovery and the serialization test instruments the new native read. External-only outcome rejection and every original durability/shared-identity/single-read assertion remain. No production byte or skip changed. The three failures reproduced locally before correction; 137 affected tests passed on each interpreter without warnings/skips, 22 in-memory fixture conformance cases passed on each, and the three former failures passed in the integrated checkout. The initial CI results, fixture provenance and exact commands remain in the verification record; fresh exact-head full CI is required.
