# 0052: Explicit owner controls for existing Converse notes

Issue #123, following ADRs 0047 and 0051 and clarification.md §12.

## Behavior and authority

An owner Character View Memory dialog lists and inspects existing notes for the
current Converse session/thread/epoch. Opening it does not create a session,
capture a note, configure either service, recall context, measure a prompt or
invoke inference. No companion control, appearance persistence, note editor,
automatic extraction, ranking or deletion UI is added here.

Saved namespace access and process-local selected use are distinct controls.
Namespace access can be enabled/disabled with its observed revision. Use selected
notes requires 1..8 active catalog entries, namespace access enabled/ready, and
the observed runtime identity plus use generation. Stop using notes has no
selection payload and remains possible when namespace inspection is unavailable
if a current runtime state is known. Use state reports configuration, never a
claim of model/counter readiness or actual context consumption. Disabled
namespace access retains notes; runtime restart starts use off.

Session availability/authentication flags do not establish Converse owner access.
Each memory endpoint enforces its own owner credential. Do not gate Converse
state/Stop on AppState.connected, sessionAuthenticated or inference readiness.
During a refresh in the same client/connection generation/full binding, retain
the last accepted runtime snapshot separately for explicit Stop if Session or
use-state refresh fails. Display its last-observed/unknown status; its runtime
identity and CAS still protect the actual off request. A changed client,
connection generation or binding discards that snapshot. Only freshly coherent
namespace/catalog/use state permits a replacement enable. A read error cannot
make a previously reachable Stop disappear solely because Session is down.

## Shared native contract

Owner-only Session POST `/v1/converse-memory/catalog` accepts MemoryBinding and
returns MemoryCatalogResult. It is absent without the existing Session opt-in.
Authentication precedes reading a request body. Existing 16 KiB request and
256 KiB response limits apply. The complete namespace has at most 256 summaries,
ordered by consecutive ordinal, with distinct IDs and count equal to state.
Each preview is the longest complete UTF-8 prefix of body within 128 bytes;
preview_truncated reports omitted suffix. A forgotten note has null preview and
false truncation. No facts or source message contents enter a summary.

Enabled listing reconciles existing source/corruption state before returning;
disabled listing validates the bounded stored graph without source reads,
reconciliation or writes. It is explicitly an owner management read, not recall.
Corrupt, quarantined or overflowing graphs are refused with a fixed error,
without a partial list. The Store lock binds state, rows and reconciliation.

## Shared Dart surface

New `api/memory_models.dart` defines immutable strict DTOs with native snake_case
wire keys, explicit version `1.0`, safe integers, UTF-8 bounds and detached lists:

- MemoryBinding(sessionId, threadId, epoch), `toJson`, equality and hashCode.
- MemoryNamespaceState: binding, enabled, revision, nodeCount, status.
- MemorySummary: nodeId, ordinal, kind, state, preview, previewTruncated.
- MemoryCatalog: state, entries.
- MemoryUseState: binding, runtimeId, useGeneration, enabled,
  implementationId, selectorIds, pending, notice.
- MemoryNode: binding and all native MemoryNode fields, with validated fact.
- MemoryInspection: state, node.

Each response factory is `fromJson(Object?)`; native enum values are String
fields, validated against their finite allowed sets. Binding is a property on
all scoped DTOs. MemoryBinding's public constructor validates its arguments.

`KinaiyaClient.memory` is a MemoryApi instance from `api/memory_client.dart`.
Its public methods are:

```dart
Future<MemoryNamespaceState> state(MemoryBinding binding);
Future<MemoryNamespaceState> configure(MemoryBinding binding,
    {required int expectedRevision, required bool enabled});
Future<MemoryCatalog> catalog(MemoryBinding binding);
Future<MemoryInspection> inspect(MemoryBinding binding, String nodeId);
Future<MemoryUseState> useState(MemoryBinding binding);
Future<MemoryUseState> configureUse(MemoryBinding binding,
    {required String expectedRuntimeId, required int expectedGeneration,
     required bool enabled, List<String> selectorIds = const []});
```

Enabled configureUse explicitly selects `korina-dag-v1`. Off sends no selected
implementation or IDs. All methods verify returned binding; inspect verifies
requested node ID; configureUse verifies returned runtime identity. New errors
are MemoryFailure(message, {int? status, bool unconfirmed = false}); toString
returns its fixed safe message. Timeout, transport or malformed acknowledgment
after dispatch of a write is unconfirmed; no automatic retry occurs.

Use a separate memory-only bounded transport component without changing existing
generation transports or closing their shared client. Real memory requests own
a per-request client with cleanup; tests can inject its factory in KinaiyaClient
and KinaiyaClient.mock via `memoryClientFactory`. Finite send/body deadlines,
redirect refusal, 16 KiB outbound and 256 KiB inbound limits, strict UTF-8/JSON,
duplicate-key/extra-field/nonfinite/unsafe-number refusal and fixed error text
apply. A small memory-only JSON reader may reject noninteger number syntax;
all numeric values in these contracts are integers. No new package is needed.
Scope, URLs and credentials stay in RAM and credentials travel only in headers.

## UI lifetime and uncertainty

The dialog captures AppState client identity, connectionGeneration and full
Converse binding. Every asynchronous update and action rechecks all three, plus
mounted state. Scope/reconnect invalidation discards old state/selection; no
write is replayed. Read refresh does not start use. A runtime ID change discards
local selection and requires a new explicit action. CAS conflict and uncertain
write acknowledgment clear actionable snapshots and require manual refresh.
An explicit refresh reads current state; an unchanged revision/generation cannot
establish that a timed-out old request will never arrive. Keep separate write
latches in owner UI RAM beyond the dialog lifetime, keyed by canonical service
endpoint and binding (bounded at 32 unresolved scopes, refuse new writes at the
bound). Closing/reopening or reconnecting to the same host cannot erase a latch.
No note content, credentials or selections belong in those latch records.

A namespace latch settles only on known request completion or an observed
revision strictly greater than that request's expected revision. A runtime
latch settles on known completion, a confirmed different runtime identity, or
the same runtime's generation strictly greater than that request's expected
generation. When a dispatched request remains owned/in flight, do not declare
its absence from an equal snapshot. Record the latch before dispatch and settle
it independently of widget mounted state; UI updates still require ownership.
For uncertain failures, equal state keeps the notice and blocks replacement
enables, even after manual refresh. Reads remain available. A new scope hides
old controls without resending old requests; its old latch remains retained.

Explicit Stop using notes may use the freshest runtime state while runtime use
is uncertain. A successful Stop fences an earlier uncertain enable only if its
generation strictly advances or runtime identity changes. A no-op off with no
previous choice can return generation zero; exhaustion can also prevent advance.
Equal-generation off does not clear uncertainty or permit a replacement enable.
Namespace no-op off likewise cannot fence a delayed enable; its writes stay
blocked until the namespace latch settles. No blind retries or automatic
configure on refresh are allowed. These RAM fences cover the running owner app;
this slice adds no persisted recovery across an owner application restart.

The dialog distinguishes unavailable feature, unauthenticated owner access,
offline/error and a genuine empty catalog. It shows literal, selectable note
text, active/invalidated/tainted/forgotten states, selected count and pending use.
It scrolls at 320 px width, short height and 2x text, with labelled keyboard
controls and no disturbance to the existing Character View composer, recording,
speech or Agent navigation. Focus returns to the entry after closing.

## Verification

Native contract and ASGI tests cover auth before body, opt-in absence, disabled
no source/reconciliation/write behavior, enabled invalidation, complete bounds,
Unicode previews, forgotten redaction and corruption refusal. Dart raw transport
and DTO tests cover malformed/oversize/redirect/error/timeout/scope cases; widget
tests cover explicit controls, stale responses, uncertain writes and accessible
layout. Independent review and exact-head CI precede merge. No human recall,
speech latency, installed tokenizer, physical phone or 8 GB claim follows.
