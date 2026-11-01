# ADR 0100: Exact native STT worker settlement

Status: implemented in source; static review/CI qualification pending.

## Decision

ADR 0099 deliberately retained uncertain host-native STT admissions. A supported
first-party local voice worker now offers `native_stt_settlement_v1`: authenticated
capability, metadata-only reservation, one-use dispatch, and metadata-only lookup.
This is a settlement protocol, not an inference retry or a reset of uncertainty.

The worker mints a random 128-bit process instance and monotonically increasing
128-bit operation IDs. The complete descriptor includes the protocol, instance,
operation ID, audio SHA256 and canonical request digest. Reservation sends no
audio and runs no model. Dispatch requires the exact still-reserved descriptor,
matching audio and the captured resource revision; it never creates a missing,
expired, already-dispatched or pruned operation. Source/native/resource admission
is rechecked after each client handshake and before export. The worker rechecks
its exact resource revision at native entry and after acquiring the model lock.

Native recognition runs in a retained thread. HTTP cancellation cannot cancel or
retire that thread. A settled receipt is issued only after actual thread exit and
join. Startup limits faster-whisper/CTranslate2 to CPU, where producer exit/join
is the completion basis. The configured GPU path is the pinned OpenAI
Whisper/Torch adapter: an initialized CUDA context is synchronized before thread
exit. A refusal before Torch/CUDA initialization has no CUDA work to drain; the
code does not claim that such a refusal executed a CUDA barrier. Legacy multipart
callers use the same thread owner even though their old client has no descriptor
for recovery. No audio or transcript is stored in metadata receipts. Transient
thread input/result references are dropped after actual work/response ownership.
The new dispatch route uses a bounded in-memory raw body. The legacy framework's
multipart upload handling remains its existing interface.

The worker retains at most 32 uncompleted/reserved/unknown operations and 1,024
positive terminal receipts (plus the bounded completing batch). Unused
reservations expire after 30 seconds and cannot be dispatched afterward. IDs are
never reused in an instance. Only proven terminal metadata is evicted; a missing
receipt is unknown. Worker restart changes the instance and is never proof that
an earlier invocation stopped.

## Durable admission and publication

Converse persists the exact private loopback origin and descriptor before sending
audio. The protected STT store migrates schema 1 to 2 only after validating the
exact old schema, complete settings receipt history and every old admission.
Existing identity/settings/request/receipt bytes are preserved. Old admissions
receive a NULL binding and remain `legacy_unknown`; no current configuration is
used to invent an old worker identity. Schema 2 adds nullable admission binding
metadata and a bounded positive settlement receipt table. Existing empty or
corrupt storage still refuses startup rather than reminting a default.

Direct transcription records publication `none`; audio windows initially record
`not_admitted`. Before append admission, Converse durably changes that to
`required`, binding exact session/thread/epoch/key, canonical AppendRequest SHA256,
audio hash and original STT pin digest. No transcript is persisted in this owner.
The Session service offers a Converse-only positive lookup at
`POST /v1/transcript/stt-publications/lookup`. It reads the original writer/key row
and validates the complete original append, authenticated-user origin, native
event/sequence, audio hash and model pin. It returns metadata only. Neither a
latest window, current epoch, missing row nor replacement service is an absence
fence. An unobserved/indeterminate required append remains unknown.

Normal supported operations and explicit recovery retain up to 10,000 positive
terminal retirement receipts. Old terminal receipts may be evicted; eviction
does not create work or clear another admission. Legacy/unsupported normal calls
retain their prior successful completion behavior, but unknown legacy markers
cannot use the new protocol.

## Owner controls

- `GET /v1/converse/options/stt-admissions`: at most 64 redacted original IDs,
  pin digests, protocol/worker IDs, publication state and actual local ownership.
  It exposes no endpoint, credentials, audio or transcript and performs no probe.
- `POST /v1/converse/options/stt-admissions/reconcile`: exact schema/backend/
  admission/pin digest. A 4 KiB body has one five-second deadline. The endpoint
  waits at most five seconds; actual bounded exchanges and cleanup remain owned
  if the caller disconnects or that wait ends first.

Reconciliation uses only the currently configured known SpeechClient at the
original exact origin and worker instance. It never follows a model label to a
new endpoint, resends audio or starts inference. Retirement requires positive
worker settlement and, when required, exact positive publication proof; all
original local operation/transport/adapter cleanup must also be settled. An
independent cleanup failure is not erased by worker success. Successful retirement
is atomic and exact-replayable before current provider eligibility checks. The
existing 13 operation-counter keys remain unchanged; reconciliation ownership is
included in cleanup counters and therefore blocks model application/shared close.

## Limits and evidence

Only the known local Whisper voice-worker protocol is supported initially.
Whistle, arbitrary embeddings and historical pin-only markers remain explicitly
unreconcilable after uncertainty. A changed worker instance, pruned metadata,
failed native cleanup or missing required append proof cannot be cleared by this
control. The shared generic Journal append transport is unchanged; the new
metadata lookup is bounded and positive-only. This does not qualify multimodal
models, microphones/cameras, audio quality, latency or native backend behavior.

No local tests, endpoints, models, devices, credentials or paid services were
called for this increment. Static checks and independent review are reported
with the change; native lifecycle/restart behavior still requires CI/qualification.
