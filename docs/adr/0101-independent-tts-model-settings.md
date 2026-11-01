# ADR 0101: Independent owner TTS selection

October 31, 2026. Source increment for #208; exact-head CI and real backend
qualification remain separate evidence. No inference, device capture or paid
request was made while implementing this slice.

## Actual interfaces

Converse owns a protected TTS choice independently from text, STT and Decisions.
Selection is the existing host synthesis interface or an immutable
`openrouter_tts` catalog generation. The existing cloud adapter supports exactly
`hexgrad/kokoro-82m`, voice `af_heart`, streaming mono24kHz signed16 PCM and
`https://openrouter.ai/api/v1` (or explicit loopback HTTP `/api/v1`). The
declaration contains those fixed model/voice values, origin, protected credential
generation, `allow_cloud:true` and integer timeout1–120 seconds (default120).
It does not claim a measured text interface, loaded weights or readiness.

Local workers accept only their startup-configured profile and voice. The owner
reports their safe native declaration where available; it does not offer
unsupported live profile/voice switches. Undeclared trusted host identities stay
null. Reconfiguring local workers through their actual Resources owner remains
separate work.

## Budget and disclosure

The catalog contains no ledger path, allowance or key. Catalog use requires the
existing explicit native host CharacterBudget path and unchanged ceiling, a
valid owner-only retained ledger outside the worker workspace, and exact
credential capture. Missing/invalid budgets make choices ineligible. Inspection
reads bounded retained budget metadata without creating a lock, allowance or
reservation. It exposes only configured/source/reason, never private paths or
amounts presented as billing.

Actual OpenRouter calls use require-existing ledger mode. Deleting a ledger
cannot silently create a fresh allowance. Existing reservations, byte estimates
and caps remain intact; the cumulative development $10 budget is unchanged.
The existing host adapter's default constructor semantics remain compatible.
The new per-call synchronous before-export hook records native uncertainty
after budget/preflight checks, immediately before HTTP dispatch. Ordinary
pre-export budget refusal does not become an unknown model operation.

Each operation independently checks reply provenance, disclosure scope,
Converse intent, resource revision, exact provider/voice and actual iterator
ownership. Saving a declaration or selection grants no disclosure or spend.
There is no fallback or automatic retry.

## State and original-request recovery

Owner-only endpoints are `GET/POST /v1/converse/options/tts-model` and
`POST /v1/converse/options/tts-model-changes/lookup`. Requests carry strict
schema1.0, expected durable backend/instance/revision/state, original key and
complete source selection. Historical exact receipts do not reapply settings.
Lookup requires the exact original body. A foreign backend refuses; same-backend
absence is fenced only by retired instance or strictly newer native revision.
HTTP errors alone never prove a dispatched save failed.

`converse-tts-model.sqlite` has an exact three-table/version1 schema, bounded
SQL/JSON and complete unique settings history. Only affirmative atomic creation
initializes it. A bounded five-second directory lock spans publication through
initialization; a separate lifetime file lock remains retained for actual
leases or uncertain admissions. Existing empty/corrupt state never resets to
host. There are at most64 live leases/admissions and10,000 settings receipts.
Requests are bounded to4KiB with one five-second body deadline; responses are
bounded to64KiB, owner authenticated and `no-store`.

Saving requires settled native Converse preparation, text, windows, STT, TTS,
memory, playback, publication and cleanup. The final CAS/commit has no await
gap. Catalog edits cannot rebind an accepted operation or its credential.

## Producer, playback and provenance

Actual speech captures an immutable `TTSModelPin`: source, backend/instance,
settings revision, exact catalog generation/digest/revision and safe declared
provider/model/locality/profile/voice. It contains no credential or private
path. New `converse.synthesis` admissions bind this pin to the original native
reply, generation and normalized-text digest, without claiming audibility or
successful synthesis. Session validates the Converse actor and source. Frames
and native delivery acknowledgements retain the same pin and operation ID.
Historical events and delivery request bodies without this optional provenance
retain their original shape/digests.

Either reserved delivery tag requires both tags and the exact pinned delivery
projection: original source/chunk, generation, causes, idempotency key and
normalized source-text hash. An authenticated historical `interrupted` or
`delivery_unknown` report may settle an exposed captured turn after its local
generation fence. It must still bind the retained native source and current
Session epoch/thread; it cannot revive playback or report stale audio as
currently played.

The native response owns its lease even before iterator startup. Actual producer,
monitor, publication and iterator cleanup remain owned through cancellation.
Catalog HTTP clients close only after producer cleanup; retiring a host lease
never closes its shared STT composition. Exposed audio keeps playback pending
until an authenticated terminal delivery publication or exact native generation
stop publication settles. That is protocol state, not proof of exact words or
physical-device silence.

Delivery publication retains at most one actual task per status. An active
exact retry joins that task; a successful result replays its receipt. After an
actual failed task settles, an explicit retry may reconcile only the frozen
original append body/key. Publication-only uncertainty is tracked separately
from sticky native worker or transport uncertainty. Its exact positive receipt
clears only that status; it does not clear a native failure or revoke an already
owned producer. New capture, Save and final settlement still wait for all
uncertainty to resolve. The finalizer joins dynamic publication tasks and closes
admission of new statuses synchronously before releasing a settled turn. The
generic JournalClient parsing and cleanup limits are unchanged.

Durable exact admissions precede unobservable local dispatch or the known cloud
export boundary. They clear only after positive producer completion, actual
cleanup and required playback/publication settlement. Cancelled/failed/unknown
work or hard death retains its marker; restart never treats it as stopped.
There is no reset button or fake worker reconciliation. A local barge-in whose
actual worker settlement is unavailable can therefore leave this role blocked;
native worker status/reconciliation remains an explicit next capability.

One event/generation cannot allocate a fresh TTS attempt while its captured
attempt is retained. This avoids duplicate spending after a lost stream reply.
The bounded process-local history is pruned only for obsolete generations with
no active owner. Recovered old-runtime replies remain ineligible under existing
playback barriers. Shared Session/speech clients stay retained under actual
unfinished users and cleanup.

## Remaining work

Unified budget administration, local worker/profile reconfiguration, additional
voices/providers, native worker reconciliation, wider fallbacks and real model,
audio-device or quality qualification are not completed by this editor. Source
analysis and generated schemas do not replace CI or that qualification.
