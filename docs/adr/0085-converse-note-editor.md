# 0085: Owner-curated Converse notes and historical receipts

October 31, 2026. Requirement: the memory and retention group in
[#208](https://github.com/rgoza1996/Kinaiya/issues/208) and
[the full Options plan](../options-delivery-plan.md). Extends ADRs 0047, 0051 and
0052; no change to Agent/Decisions memory ownership.

## Owner behavior

The embedded Options Memory editor and Converse Memory dialog share the same
controls. Creating a note requires a freshly inspected, authenticated, finalized
`conversation.user` source from the current session/thread/epoch. The editor
shows its bounded literal preview and captures its exact event revision.
Assertions and observations have a typed fact (text, safe integer, boolean or
null). Inferences require active prerequisites, at most eight. The native store
remains authoritative for source provenance, dependencies and contradiction
handling. Opening or editing a form does not invoke a model or start recall.

"Create revised note" prefills a new immutable capture. It requires another
explicit current source inspection; the old note remains unless separately
forgotten. The UI does not claim an atomic replacement or edit historical
evidence. Forget requires explicit confirmation, supports the native known-ID
administration path even when listing is disabled/quarantined, and retains the
existing descendant-erasure behavior. This is distinct from transcript deletion.

The editor retains at most sixteen scoped drafts in RAM. Unconfirmed content
writes retain the exact bounded original request in at most thirty-two scoped
pending records, also only in RAM. These records may contain private note text;
they are never written to preferences, logs, recovery files or receipts. Owner
application restart loses this client-side recovery. The original content-free
access/use latches remain separate. Form drafts and pending writes are fenced
by client identity, connection generation, endpoint and full memory binding.

## Read-only receipt contract

Opt-in owner Session `POST /v1/converse-memory/operations/lookup` accepts:

```json
{
  "schema_version": "1.0",
  "operation": "capture",
  "request": {
    "schema_version": "1.0",
    "session_id": "example-session",
    "thread_id": "example-thread",
    "epoch": 1,
    "expected_revision": 3,
    "idempotency_key": "example-capture",
    "kind": "assertion",
    "source_event_id": "example-final-user-event",
    "source_revision": 1,
    "body": "I prefer short answers.",
    "fact": {"schema_version": "1.0", "key": "preference.answer_length", "value": "short"},
    "prerequisite_ids": []
  }
}
```

For `forget`, `request` is the original `MemoryForget`. The existing strict
16 KiB request and 256 KiB response limits, owner authentication before body
parsing, JSON validation and `Cache-Control: no-store` still apply. The new
`MemoryOperationLookup`, `MemoryOperationReceipt` and `MemoryOperationResult`
contracts and generated schemas define the exact wire shapes.

The native owner compares operation and the canonical digest of the complete
original request against the existing receipt. It does not create a new ledger,
reconcile sources, recall text, enable memory or reapply a write. Historical
lookup can succeed after namespace/epoch changes or disabled/quarantined memory.
Only redacted metadata returns: original binding, operation/key, `committed` or
`not_found`, and (when committed) an opaque receipt identity, saved revision,
node ID and current stored disposition. No source/body/fact or erasure list
returns. A stored disposition is not a claim of current source freshness or
recall eligibility. Invalid receipts fail closed.

`not_found` includes missing, erased and valid no-op receipts. It never proves
that a delayed POST failed or cannot arrive. The independent `mutation_fenced`
boolean observes irreversible native guards under the store lock: a later
namespace revision, a deleted Session, a different immutable thread, or a
strictly newer Session epoch. Invalid metadata supplies no proof; a lower
current epoch does not fence a future requested epoch. A true fence means that
the old request can no longer create or forget a note, not that its earlier
outcome is known. Ordinary source reconciliation before CAS can still occur.

## Recovery and resource ownership

Manual lookup uses the exact retained request. Manual identical retry is offered
only for the current scope, never an earlier scope, and is disabled after a
native fence. No automatic retry occurs on refresh, reopen or reconnect. A
matching committed receipt settles the historical write; an absent receipt
leaves its outcome unknown. An irreversible fence permits the owner to
explicitly acknowledge and dismiss that unknown outcome without classifying it
as success or failure. Stale read results cannot clear a newer pending record.

Memory HTTP requests retain independent bounded transports and cleanup. Source
inspection has at most eight global admitted futures; a display timeout does
not release ownership before actual completion. The existing composer, voice
and Agent transports are not borrowed or cancelled by these controls.

## Implementation and evidence

Native: `packages/contracts/memory_operations.py`,
`services/session/memory_operations.py` and the existing opt-in memory router.
Flutter: `api/memory_client.dart`, `api/memory_models.dart`,
`ui/converse_memory.dart` and `ui/converse_memory_editor.dart`.

Static Dart analysis, Python Ruff/compilation, generated-schema export and
whitespace inspection were performed during implementation. No new automated
tests or live memory writes were performed for this slice. CI and browser
evidence, when collected, must be recorded separately with their exact revision.
Automatic extraction, relevance ranking, backend replacement and transcript,
audio or diagnostic retention/export/deletion remain separate unfinished work.
