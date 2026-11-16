# Native live Decisions attempts and publication (#623)

Status: implementation increment under qualification. Adaptive Live remains off.
These APIs and the request-bound provider facade supply the native inference and
result boundary. The completion-driven scheduler, capture/consumer integration,
backchannel playback and qualified rollout are still required for full #623.
No microphone, inference or playback starts merely by constructing the service.

## Explicit source and attempt protocol

`native_live_decisions_v2` uses a `LiveDecisionsSource` containing the original
six-field Decisions source identity, immutable native capture registration
receipt, exact source/capture publication reference and original eligible revision
and input digest. The complete source request digest, owner/capture/scope and
registration-before-source sequence boundary must agree with native records.
Only the canonical eligible source is admissible. Identical retained ASR snapshots
can advance the raw journal without rebinding that original source or admitting
another classification. A genuinely changed input invalidates earlier eligibility.

Lookup, admission, completion and state require explicit `contract_version: 2`.
They use the existing charged-attempt SQL ledger and cutover/uncertainty semantics.
Finalized v1 endpoints continue refusing windows; v2 refuses finalized or
unannotated input. Existing v1 JSON schemas retain their bytes. The nine additive
v2 exports have a separate byte pin.

Admission requires an actual model pin matching the selected provider, model and
protocol, original task/attempt identities, expected charged count, exact request
and transport digests and the complete original disclosure-source list. Native
Session atomically rechecks capture permission, eligible source, session/epoch and
every sticky disclosure floor before reserving. The trusted startup attempt
allowance remains 1..16, default 2, with the original lowering-only override;
this does not change the number of concurrent calls or authorize quality rerolls.
Lost acknowledgments, cancellation and unknown remote outcomes retain charges.

`NativeLiveDecisionAttempt` is owned by the actual classifier Task. It uses the
existing request-bound HTTP transport, synchronous model-current fence and sole
response close. The original provider, client, headers, endpoint, transport,
request body and task must still match before the one provider entry is consumed.
The native admission is the disclosure linearization point; later revocation
cannot recall an already admitted remote request. Its original HTTP/model owners
must settle even after cancellation. The existing two-second admission/completion
facade deadline does not prove their physical cleanup or absence of a write.

## Completion, result publication and recovery

Exact original completion retains raw response and either the strict five-outcome
`LiveWindowDecision` or a distinct refused/error/uncertain state. The output's
source identity, scope and utterance must match the original request. No native
code edits the model's conversation or independent Agent-routing labels.
Stale or fenced completions remain diagnostic history; they do not become usable
merely because their original request completed.

`LiveDecisionsPublication` contains the exact source and original owner/attempt,
admission, completion and decision digests. It contains no fresh caller-supplied
labels. The sole native publisher derives `routing.live_window_decided` from the
stored completed attempt and rechecks capture/eligibility, session and all original
disclosure floors inside its transaction. Generic append cannot create this event.
The outcome retains both routing axes and exact attempt/source provenance. It
grants no provider, Agent, tool, task-success or playback authority.

The positive publication receipt additionally checks the full decision digest,
native origin, source cause, scope, task references and idempotency key. A changed
source or label cannot masquerade as the original outcome. Exact retries return
the historical original receipt. Read-only lookup never re-enters a provider or
replays a mutation. A missing receipt is unknown, not proof of a failed write.
Consumers must still establish their own current native admission and deduplication.
The [completion-driven worker](live-decisions-worker.md) holds/rechecks its actual
selected-model lease before publishing; Session's source ledger does not replace
that service-owned lease. Its exact-proof recovery policy is documented separately
from these read-only lookup and idempotent native publication APIs.

Owned mutations refuse an already-active SQLite transaction before entering the
connection context, preserving the original caller's uncommitted work (#665).
The shared finalized ledger receives the same correction.

## Internal HTTP boundary

Under `/v2/transcript/live-decisions`, authenticated Decisions alone may POST
`attempts/lookup`, `attempts/admit`, `attempts/finish` and `publish`. Decisions or
Converse may POST `publication` for read-only original publication lookup.
LocalJournal uses the same role rules; owner, Agent and resources cannot acquire
this native service authority.

Requests require identity JSON, a closed envelope and no query parameters.
Duplicate fields, nonfinite numbers, coercion, unsupported versions and unknown
fields refuse. Metadata is bounded to depth 16 and 4,096 nodes. Body collection
has a five-second deadline and rechecks credentials afterward. Lookup/publication
requests are at most 16 KiB; admission is at most 128 KiB. Completion retains the
existing 1 MiB raw-response and 64 KiB validated-output limits within
`MAX_ATTEMPT_BYTES`. JournalClient bounds reads to the same response ceiling and
retains each original acquisition/read/sole close through caller cancellation.
No automatic inference or mutation retry is added.

Success responses are uncached. Invalid envelopes return 400, unsupported encoding
or media type 415, excess body 413, body deadline 408, invalid metadata 422 and
unavailable/conflicting original native operations 409. These errors never prove
that an uncertain earlier side effect did not occur.

See [qualification evidence](evidence/live-decisions-attempts-2026-11-16/README.md)
for exact checks, unchanged inputs and retained first failures. Architectural
correctness is the acceptance criterion; model answer, style and speech quality
are diagnostic observations.
