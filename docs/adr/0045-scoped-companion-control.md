# ADR0045: scoped foreground companion control

Tracks [#108](https://github.com/rgoza1996/Kinaiya/issues/108), Roadmap 19.3 and a bounded foundation for M8. This adds pairing, invitation signaling/control and a literal retained note to the existing Session service. It does not implement media, notifications/push, Android/iOS calling integration, background/locked-screen delivery, or complete M8.

## Authority and persistence

`services/session/companion.py` uses the existing Store SQLite connection/RLock, with additive `companion_schema=1` tables. Existing journal schema_meta 1, role tokens, event provenance, cursors and append semantics are unchanged. All grant/call authorization, native current Session(channel=converse,thread,epoch), deadline reconciliation, idempotency and mutations occur synchronously while holding that same lock. No cached authentication survives an await. Session deletion cascades the whole companion scope; reset invalidates effective authority immediately, and old open invitations become terminal on the next reconcile. Owner may still read historical metadata/notes after reset, but cannot mutate them. Devices cannot read historical scope after reset, expiry or revocation.

Owner creates a 120-second pairing code, bound to the exact current Converse session/thread/epoch and label. Anonymous redemption atomically removes that code and issues one 24-hour device bearer. Both use 32 bytes of randomness; only SHA256 hashes are stored. Neither issuance is automatically retried or recovered as a raw secret. If a redemption response is lost, re-pair; the retained unreachable device still counts toward the 32-record cap. Owner may revoke it through the normal owner-only controls. There is no refresh endpoint.

The device bearer grants only `companion_control`. It is never inserted into Auth.tokens and cannot authenticate ordinary protected Session/journal/Agent/approval/resource/provider/audio endpoints. Device-only routes accept only the companion bearer, not an owner/service credential. No new journal kind, task, model/resource request or general capability is dispatched by these APIs.

## Wire/API

All responses use the existing 1.0 wire version with strict extra-forbidden companion models in `packages/contracts/companion.py`. New schemas are exported alongside existing schemas. IDs are at most 128 ASCII letters/digits/underscore/dot/colon/hyphen; idempotency keys are at most 128 printable nonspace ASCII characters. Names/labels/titles are nonblank, at most 120 Unicode characters / 480 UTF-8 bytes without controls. Timestamps are integer Unix seconds, revisions/cursors are strict finite integers within the JavaScript-safe range.

Owner pairing builders explicitly project the Session's `session_id`, `thread_id`
and `epoch`, then add `label` and the wire version. They must not spread the full
Session response into a pairing request: `channel`, `name`, and additive Agent
`harness_version` metadata are outside the companion contract, even when null.
The ordinary Session API retains the harness association; projecting a pairing
binding neither hides that association nor makes an Agent session pairable.

Owner Bearer routes:

- POST `/v1/companion/pairings`: `{session_id,thread_id,epoch,label}` -> binding/label/`pairing_code,expires_at`.
- GET `/v1/companion/devices` -> `{devices:[...]}`; POST `/devices/{device_id}/revoke` with `{}` -> device metadata.
- POST `/v1/companion/calls`: `{device_id,title,idempotency_key}` -> call. Scope comes from the captured grant, never from a caller-supplied alternate session.
- GET `/v1/companion/calls?session_id=...&device_id=...&after=0&limit=20` -> `{calls,next_after}`; GET `/calls/{call_id}` -> historical/current call.
- POST `/calls/{call_id}/end`: `{expected_revision,idempotency_key}`.
- POST `/calls/{call_id}/note`: `{expected_revision,note}`.

Anonymous POST `/v1/companion/pairings/redeem` accepts `{code,device_name}`, with no Authorization header, and returns `{device_token,device}`. Device metadata contains ID/name/label/scope, immutable session/thread/epoch, created_at/expires_at/revoked_at and active/expired/revoked/stale status.

Device Bearer routes: GET `/v1/companion/self`; GET `/self/calls`; GET `/self/calls/{call_id}`; POST `/self/calls/{call_id}/actions` with `{action:answer|decline|end,expected_revision,idempotency_key}`. Every operation re-authenticates the current grant and checks call ownership.

A call contains ID/sequence/device/binding/title, state/revision, created_at/ringing_deadline/media_deadline/end_reason and literal note. Pages are ascending by monotonic call sequence, with a sparse-safe after cursor: next_after is the last returned sequence only when another row exists, otherwise null. At most 20 rows/page and 100 retained calls/device allow a full device refresh in at most 5 pages, including updated old notes. Metadata and all 20 maximum-size notes fit a 256 KiB response ceiling, including JSON escaping that can double each note’s byte count.

## Truthful state and replay

Only one open invitation/device. Ringing expires after 30 seconds to `missed(unanswered)`. Answer enters `accepted_waiting_media`; after 15 seconds it ends with `media_unavailable`. Decline or either authorized end is terminal. There is no active/connected/played state, fake media-ready endpoint, media token or phone authority over Agent. Terminal invitations never reopen.

Creation replay with the same key/body returns the existing current call; conflicting reuse refuses. Action replay returns its saved result only while the current revision still equals that result. If a deadline, note update or later action supersedes it, reply 409 and require GET/reconciliation; never replay a new action automatically. Grant revocation/reset/expiry is checked before every exact replay and defeats it. Stale expected revisions and invalid transitions refuse. Revisions increase for every transition or note update. At most 2 successful actions/call are possible under the state machine; durable action receipts are bounded by those transitions, not an arbitrary action history.

The note is one literal UTF-8 string of at most 4096 bytes, with NUL/nontext controls refused and newline/tab allowed. JSON-looking snippets, markup punctuation and URL text remain literal text: clients must render a Text widget, with no HTML/JSON interpreter, auto-link, executable bindings, media fetch or dispatch. Notes survive hangup/restart. Terminal notes may be edited by owner while the grant/binding remains current; historical stale/expired/revoked notes are read-only.

Restart lazily reconciles stored wall-clock deadlines using an injectable clock. There is no timer/daemon/websocket. An observed device expiry is persisted and remains terminal after rollback or restart. A wall-clock rollback before observation can delay a deadline; this module makes no distributed-clock or hard real-time guarantee. Unknown module schema refuses startup without changing journal version.

## Bounds and transport

Maximum 8 pending pairings globally (expired/stale entries pruned), 32 retained device records globally, 100 retained calls/device, 20 rows/page. Admission refuses at retained caps; it never deletes old notes/calls to manufacture capacity. Sessions may be deliberately deleted by the existing owner API.

All companion routes enforce the existing allowed_origins policy, including anonymous redemption. They require HTTPS, except when both actual ASGI peer and Host are loopback. No X-Forwarded header is interpreted here; deployers must establish truthful ASGI transport/peer metadata. Bearers use Authorization only, never query fields; unknown/duplicate query parameters refuse. Body receipt is capped 16 KiB before JSON parsing, with strict UTF-8/duplicate-key/nonfinite-number rejection. Validation errors do not echo submitted secrets. Responses are no-store and capped 256 KiB. These checks do not replace TLS deployment, a client secret store or platform transport validation.

## Verification scope

Synthetic ASGI/store tests cover concurrent single redemption, hash-only storage/restart, privilege/origin/transport separation, deadline/revoke/reset/delete fences including exact replay, current/wrong device and binding, stale/conflicting revisions, capacity and sparse paging, literal note persistence, and additive schema compatibility. No model, native mobile hardware, external network service, credential or media backend is used. Native platform/incoming-call integration and end-to-end media remain open.
