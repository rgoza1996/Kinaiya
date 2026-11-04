# ADR0110: owner companion pairing, device and call controls

Tracks [#125](https://github.com/rgoza1996/Kinaiya/issues/125), following [#108](https://github.com/rgoza1996/Kinaiya/issues/108) (ADR0045) and [#109](https://github.com/rgoza1996/Kinaiya/issues/109) (ADR0046) under parent [#11](https://github.com/rgoza1996/Kinaiya/issues/11) (M8). This slice adds the owner-only Flutter client and Companions panel over the existing Session companion endpoints. It does not add media transport, duplex audio, host speech-credential sharing, relay provisioning, push/locked-screen delivery, CallKit/Telecom, or native microphone integration; M8 remains open.

## Authority and boundary separation

The Session companion state machine (`services/session/companion.py`), routes (`services/session/companion_api.py`), wire contracts (`packages/contracts/companion.py`), and foreground phone grant parser/client (`apps/kinaiya_flutter/lib/companion/`) remain unchanged.

The owner application uses separate owner-facing DTOs (`apps/kinaiya_flutter/lib/api/companion_models.dart`) and an independent per-request owner transport (`apps/kinaiya_flutter/lib/api/companion_client.dart`, exposed as `KinaiyaClient.companion`):

- Owner DTOs parse all retained device statuses (`active`, `expired`, `revoked`, `stale`) so historical, revoked, expired, and cross-session directory entries remain inspectable and revocable without weakening the phone client's `active`-only grant parser.
- `OwnerCompanionBinding.fromSession` projects only `session_id`, `thread_id`, and `epoch` for `channel == 'converse'` with `schema_version: '1.0'`. It never spreads `channel`, `name`, or `harness_version` into companion requests and refuses non-Converse sessions.
- `OwnerCompanionApi` authenticates with the in-memory owner Bearer header only (`Authorization: Bearer <token>`). It provides `issuePairing`, `listDevices`, `revokeDevice`, `inviteCall`, `listCallsPage`, `listCalls`, `getCall`, `endCall`, and `updateNote`. It exposes no `redeem` or `/self` device methods and never transfers the owner token to a phone or body field.

## Transport and wire bounds

Every request uses an owned single-request `http.Client` with `followRedirects = false`, a 5-second request deadline, a 300 ms stream-cancellation deadline, a 16 KiB request body ceiling, and a 256 KiB response body ceiling:

- `_OwnedRequest` subscribes to the response stream before status/header checks, closes the underlying client before awaiting stream cancellation, and retains unsettled requests against the 8-in-flight transport cap.
- `_JsonReader` enforces strict UTF-8, rejects duplicate JSON keys, unpaired UTF-16 surrogates, non-integer numeric tokens, leading-zero numbers, and values outside the safe integer range (`1..9007199254740991`, or `0..9007199254740991` for timestamps/cursors).
- Paging through `/v1/companion/calls` enforces at most 20 rows per page, 100 retained calls per device and 32 devices (at most 160 session inventory pages, or 5 for an exact device). Every retained row counts toward these bounds before projection, with strictly monotonic `sequence` ordering and unique `call_id`s. `listCallsPage` continues to require the exact binding and optional exact device. `listCalls` also validates prior epochs of the same session/thread, rejects future epochs and foreign session/thread/device rows, then returns only the exact requested binding. Session reset retains history; it must not make the current inventory unreadable.
- Errors map to fixed owner-facing messages without reflecting host error bodies, URLs, or credentials.

## Owner Companions panel and uncertainty reconciliation

`ConverseCompanionsDialog` (`apps/kinaiya_flutter/lib/ui/converse_companions.dart`), reachable from Converse via `ConverseCompanionsButton` and the Converse interaction header, obeys the following lifetime and recovery rules:

1. **Read-only open**: Opening or closing the panel performs only owner `GET` reads (`listDevices` and, when a current Converse session binding exists, `listCalls`). It never creates a session, issues a pairing code, sends an invitation, alters owner drafts/capture/playback, or invokes inference.
2. **RAM-only pairing codes**: Issued 120-second single-use pairing codes stay in widget memory only, display a live expiration countdown, and require deliberate owner copy (`Clipboard.setData`). Lost pairing issuance responses are never auto-retried and cannot recover a raw code.
3. **Binding-governed actions**: Every operation captures `(client, connectionGeneration, OwnerCompanionBinding)`. Devices outside the captured current Converse binding (or in `expired`/`revoked`/`stale` states) are read-only except explicit owner revocation (`POST /v1/companion/devices/{id}/revoke`), which remains available for any non-revoked retained device entry.
4. **Truthful call states and literal notes**: Call rows display `ringing`, `accepted_waiting_media` (explicitly noting that answering on the companion awaits voice media and falls back to `media_unavailable` after 15 seconds), and terminal states (`declined`, `missed`, `ended`) with their `end_reason`. Retained call notes render as literal `SelectableText` and are edited via native CAS (`expected_revision`).
5. **Visible serial polling**: Polling runs only while the panel is mounted and visible/foregrounded, scheduling the next 2-second poll only after the active refresh settles. Refresh never generates new actions or auto-resubmits uncertain writes.
6. **Bounded uncertainty across close/reopen/reconnect**: Unconfirmed `invite`, `end`, and `note` writes are retained in a bounded in-memory map (up to 32 scopes) keyed by canonical Session endpoint and Converse binding:
   - Each immutable request payload enters the captured scope before dispatch. In-flight identities prevent pruning, fresh same-operation admission, replay, or read reconciliation while its captured client still owns the pending request. Late outcomes settle only the matching original attempt independently of widget lifetime; presentation updates retain the client/generation/token/binding guards. Refresh never treats absence as proof that a write failed.
   - Uncertain `invite` retains the original `(device_id, title, idempotency_key)` for explicit same-key replay or settles when a refresh observes the created call.
   - Uncertain `end` retains `(call_id, expected_revision, idempotency_key)` for explicit same-key replay or settles when an observed call snapshot is terminal or advances past `expected_revision`.
   - Uncertain `note` fences further CAS note edits on that call until an explicit `GET /v1/companion/calls/{call_id}` reconciliation (or refresh) confirms the current call revision and note text. Only IDs actually returned by an authoritative read may reconcile; cached rows for other calls cannot do so. Reads begun before the note request settles cannot establish its next CAS baseline, and an older response cannot replace a newer cached revision.
