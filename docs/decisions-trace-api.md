# Decisions owner inspection API (contract 1)

The Session service owns these endpoints. They are read-only and require the
owner bearer plus the existing browser-origin check. No inspection operation
invokes a classifier, executes work, or reconstructs missing historical data.
All strings are literal untrusted text. JSON field names below are frozen for
the owner client. Unknown/duplicate query parameters are rejected.

## List

`GET /v1/decisions/inspections`

Optional query parameters: `session_id`, `source` (`all`, `conversation`, `agent`;
default `all`), `outcome` (literal native label or disposition), `search` (literal
substring, maximum200 characters), `from_date`, `to_date` (`YYYY-MM-DD`, inclusive),
`timezone` (IANA identifier, default `UTC`), `through_sequence`, `before_sequence`,
`limit` (default20, maximum20). Dates filter the **Source date**, not inference
completion time. A single date sends equal from/to dates. Both dates must be
supplied together. Server converts local midnight through the midnight after
the end date into the half-open UTC interval echoed below, including DST.

One row represents one actual native source event. Conversation sources are
`conversation.user`, `conversation.revision`, `conversation.window`, and
`conversation.converse`; Agent sources are existing `agent.command`,
`agent.state`, `agent.tool`, and `agent.outcome`. Newest source sequence comes
first. Continuations send both the returned watermark and before cursor with
the same filters. At most20 rows and256KiB are returned.

```json
{
  "contract_version": 1,
  "through_sequence": 123,
  "next_before_sequence": 90,
  "exhausted": false,
  "timezone": "UTC",
  "range_start_utc": null,
  "range_end_utc": null,
  "rows": [{
    "source_event_id": "native-id",
    "source_sequence": 122,
    "session_id": "session-id",
    "thread_id": "thread-id",
    "epoch": 1,
    "source_kind": "conversation.user",
    "source_origin": "authenticated-user",
    "source_revision": 1,
    "created_at": "2026-10-30T12:00:00Z",
    "preview": "Literal source text, at most1000 characters",
    "disposition": "classified",
    "outcome": "ignore",
    "decision_id": "native-decision-id",
    "provider": "laya",
    "model": "english",
    "confidence": null,
    "call_count": 1,
    "capture_state": "retained"
  }]
}
```

`decision_id`, `outcome`, `provider`, `model`, and `confidence` may be null.
Malformed historical revision counters are explicitly `source_revision: null`.
Confidence is a recorded model signal, never a calibrated certainty. Summary
identity comes only from a captured historical call, never current settings.
`disposition` is one of `classified`, `refused`, `error`, `pending`, `reused`,
`no_inference`, `unavailable`. Native override reasons remain in detail receipts.
`capture_state` is `retained`, `expired`, `unavailable`, or `not_applicable`.
Agent rows use `no_inference`/`not_applicable` and retain their real event identity.
Historical conversation rows without capture remain `unavailable`; any existing
native route is still displayed without inventing a script or model.

## Detail

`GET /v1/decisions/inspections/{source_event_id}`

Optional queries: `through_call_sequence`, `before_call_sequence`, `limit`
(default5, maximum5). A continuation supplies both watermark and before cursor.
Unknown sources return404. A detail response is below4MiB; a byte bound may
return fewer calls than the requested count. Receipts are bounded to100; their
`receipts_truncated` flag explicitly reports additional native records.

```json
{
  "contract_version": 1,
  "source": {"event_id": "native-id", "sequence": 122, "session_id": "session-id", "thread_id": "thread-id", "epoch": 1, "kind": "conversation.user", "origin": "authenticated-user", "created_at": "2026-10-30T12:00:00Z", "payload": {"text": "Literal source"}},
  "summary": {"source_event_id": "native-id", "source_sequence": 122, "session_id": "session-id", "thread_id": "thread-id", "epoch": 1, "source_kind": "conversation.user", "source_origin": "authenticated-user", "source_revision": 1, "created_at": "2026-10-30T12:00:00Z", "preview": "Literal source", "disposition": "classified", "outcome": "ignore", "decision_id": "native-decision-id", "provider": "laya", "model": "english", "confidence": null, "call_count": 1, "capture_state": "retained"},
  "receipts": [],
  "receipts_truncated": false,
  "through_call_sequence": 7,
  "next_before_call_sequence": null,
  "exhausted": true,
  "calls": [{
    "call_sequence": 7,
    "call_id": "native-call-uuid",
    "attempt_id": "native-attempt-uuid",
    "attempt": 1,
    "source_event_id": "native-id",
    "source_sequence": 122,
    "source_revision": 1,
    "session_id": "session-id",
    "thread_id": "thread-id",
    "epoch": 1,
    "created_at": "2026-10-30T12:00:01Z",
    "completed_at": "2026-10-30T12:00:02Z",
    "status": "completed",
    "provider": "laya",
    "model": "english",
    "artifact_sha256": null,
    "request_format": "laya_systemone",
    "request": {"state": "Exact rendered state", "questions": {}, "model": "english"},
    "raw_response": "Exact literal response before mapping",
    "validated_output": {"route": "ignore"},
    "error_type": null,
    "reused_from_call_id": null,
    "capture_state": "retained"
  }]
}
```

`source` and each receipt are complete native JournalEntry JSON objects
(including their existing schema/idempotency/causal fields). Call status is
`started`, `completed`, `refused`, `error`, `cancelled`, or `reused`. A started
call with no completion is **unknown**, not proof that no provider processing
occurred. Request/response/output and historical artifact fields are nullable.
Reused records have no new request and link to the original call ID; a retained
original call is retrievable through its original source's detail. Reuse also
supplies `reused_from_source_event_id` for that navigation. Maximum
request256KiB, response1MiB, validated output64KiB. Invalid/oversized responses
never become validated output. Raw response text is retained only when complete
and valid UTF-8; unavailable raw data is explicitly null.

## Capture authority and retention

Decisions-authenticated writes use `POST /v1/decisions/calls/start` and
`POST /v1/decisions/calls/{call_id}/finish` with strict native source binding.
Other service roles cannot write or read call records. These rows are not
generic transcript entries and never appear in transcript reads or events.
Start/finish are immutable and idempotent for identical requests; conflicting
replay is409. Capture occurs in the concrete provider at its actual request
body assembly and response decoding boundary, before native mapping/overrides.
Credentials and transport headers are excluded. Capture I/O has a half-second
budget per write. After that await, the provider rechecks exported native
sources, revision heads, disclosure floors, and the session with a half-second
validation budget; unavailable validation preserves the native backlog rather
than recording a terminal refusal. Actual native mismatch or restricted scope
prevents dispatch. These checks retain the existing native admission semantics;
they do not claim atomic revocation across separate service reads. Failure leaves
unavailable/unknown evidence without inventing successful capture. An uncertain
start acknowledgement is reconciled with the identical immutable request, never
relabelled as `not_submitted`. Oversized or unrepresentable request capture stays
unavailable. Raw non-success HTTP bodies also have a half-second capture deadline
and retain the original provider-error and retry policy when capture cannot finish.

Retained request/response/output payloads share a256MiB host quota. Oldest
payloads expire first, leaving historical identity/call metadata and explicit
`capture_state: expired`; expiration never invents data. Session deletion
cascades calls; reset retains old epoch history. Late completions cannot recreate
a deleted session/source. Read operations do not expire, reconcile, or write
records. Capture failures must neither grant work nor masquerade as retained
evidence; routing retains its existing authority and retry policy.
