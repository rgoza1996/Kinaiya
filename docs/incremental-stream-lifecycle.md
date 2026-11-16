# Incremental stream ownership and recovery (#623, #654)

Status: native source implementation qualified locally; hosted CI and integration pending.
Adaptive Live remains off and is not wired to Call. The optional browser FIFO alone
does not qualify the full feature. The retained lost-acknowledgment and cleanup controls pass
with the original successful path. Full Adaptive Live qualification remains separate.

## Existing mechanisms and the gap

The existing Whistle begin/PCM/finish/cancel wire and native worker IPC are the
starting point. The broker serializes its native stream and retains real cleanup
ownership. Converse already has captured STT model leases, exact publication
validation, native settlement metadata and shared-client retirement in
`stt_operations.py`, `stt_reconciliation.py` and `stt_settlement.py`.

Those ordinary STT owners bind one transcription and one publication. A live
stream has a separate original begin, many ordered PCM consumptions/publications,
decoder finalization and cancellation. Reusing the single-publication binding by
overwriting it would lose the original proof. New stream bindings must be
versioned; existing one-shot settlement and Rolling Windows remain compatible.

Five retained controls run the real ASGI/Whistle IPC/SQLite flow with explicitly
synthetic engine/manager/coordinator fixtures. Owner-facing and upstream begin
ACKs are lost only after the original begin completes; PCM publication can commit
before losing its ACK or fail before inserting; finish can complete before final
publication fails. Each consumes its original at most once. All five then lack
exact read-only inspection (404); the existing successful-path case still passes.
These controls establish the described harness gaps, not real ASR performance or
physical worker retirement. Raw first receipts remain under `output/issue-654-*`.

## Required native owner

One Converse stream owner must capture the exact STT provider/client/model lease,
native runtime admission, session/thread/epoch, utterance, disclosure and capture
identity before dispatch. Native resource ownership remains in the broker and
supervisor. A preference, hash, model output or lookup response cannot authorize
inference, a new capture or Agent work.

Assign an original operation identity before each effect. An authenticated
metadata reservation/lookup must bind a broker-instance identity, unique
operation ID, phase and complete canonical request digest. PCM binds its actual
byte hash, sample count, sequence and sample offset. Lookup receives that exact
original without replaying audio. A changed broker instance, missing cache entry,
timeout or coincident current values is unknown rather than settlement proof.

Retain the original native task and result across caller cancellation and HTTP
disconnect. A request watcher may stop waiting and request cancellation; it must
not replace the task, lose a late stream ID or close its captured client early.
Cancellation uses the original captured endpoint/incarnation. A newly selected
model/client cannot certify cleanup of its predecessor.

Serialize one unresolved native operation/publication per stream. Native PCM
consumption, durable transcript publication, caller acknowledgment and native
retirement are distinct facts. Before accepting subsequent PCM, retain the exact
recognizer result and frozen original publication. Reconcile an unknown append
using its authenticated exact digest/receipt; never rerun STT or infer failure
from absence. A read-only lookup cannot silently create a replacement effect.
Controller/restart fences must prevent old originals from entering current
publication or new inference authority. Unreconciled history stays explicit.

Terminal decoder output and its publication survive removal of the active-stream
slot. A completed unary begin/PCM/finish call does not prove that the stream or
worker retired. Positive retirement requires the existing real native cleanup
proof, or irreversible proof that the exact reserved original never dispatched.
Results stay inspectable while inference is off; inspection must not wake it.
A completed original producer refused before the native-entry marker proves that
original never dispatched. Its reservation stays consumed, and this proof alone
does not claim that a process was released.

Keep record counts, encoded results, retained client/tasks, body acquisition,
native deadlines and journal requests bounded. Unknown owners cannot be evicted
to make capacity appear free. Preserve every recognized hypothesis and exact
native output within declared bounds; oversized/unpublishable output produces
an explicit retained refusal rather than cropped text or an invented decision.
Do not alter the journal's64KiB limit or one-shot settlement formats.

## Input and later integration

The optional browser adapter uses the existing mono16k AudioContext capture
graph and refuses a different actual rate. It relies on that context's continuous
conversion instead of resampling snapshots independently. It has one fixed
960,000-sample PCM16 allocation matching the native sixty-second stream limit.
Packets contain at most16,000 samples, preserve FIFO order and stay frozen until
an exact local acknowledgment. Consumed samples are cleared only after that
acknowledgment. That local action must follow independent native publication
proof; it does not establish native STT retirement.

One retained readiness promise wakes on new PCM or local capture retirement;
it is not a polling cadence. Faults stop local acquisition, preserve the original
packet/buffer and keep actual context-close ownership. Reconfiguration/release
cannot claim that a pending close completed. Capture epochs cannot alias across
disposal. Playback and packet acknowledgment leave the armed microphone and
utterance intact. Raw PCM and private device IDs are not logged or persisted.

The complete feature still needs conservative restart recovery, safe decoder-segment
rollover within the same ongoing capture, versioned five-outcome Decisions,
fresh-eligible-source/completion scheduling, independent Agent routing,
backchannel/echo/mute ownership, stage timing and qualified default rollout.
Decoder completion is not evidence that the user finished speaking. Model
answer quality and acknowledgment wording are diagnostic, never the release gate.

## Implemented wire and explicit limits under qualification

The broker exposes `GET /v1/audio/transcriptions/stream/ownership` and authenticated
`reserve`, `lookup`, `acknowledge` and `abort` POST controls under that path.
`whistle_stream_owner_v1` reservations bind broker instance, monotonic operation ID,
phase and complete canonical request digest. The existing effect paths accept
one exact descriptor in `x-kinaiya-stream-original`. Dispatch consumes only its
unused reservation. A missing/evicted/restarted original stays unknown. Sixty-four
unacknowledged/reserved/unknown records and1,024 terminal metadata receipts are
bounded independently; unused reservations expire irreversibly after30seconds.
Exact result ACK drops the bounded128KiB result, never an unknown native owner.

Converse exposes `GET /v1/converse/audio/stream/ownership`. Metadata-only
`/{begin,pcm,finish}/lookup` receives schema1.0, `converse_stream_owner_v1`, the
captured `owner_instance` and SHA256 of the complete normalized original JSON.
Normalization includes the request model's declared defaults; canonical JSON
uses sorted keys, UTF-8, unescaped Unicode, no spaces and no nonfinite values.
Lookup can observe the original broker result and exact Session append, update
its local observation and return a frozen ACK. It never dispatches audio, appends
a new window or acknowledges a broker result. A missing append is still pending.
`/{phase}/reconcile` is the explicit exact metadata ACK/retirement action; it also
cannot rerun STT or replace/retry the transcript append. A fresh authorized next
PCM may acknowledge its proven predecessor before its own dispatch.

Successful legacy begin/PCM/finish ACK fields remain compatible. Exact duplicate
PCM must also preserve speaking state and all original scope, rather than merely
matching bytes. One unresolved operation excludes every subsequent PCM/finalize.
PCM reserves sequence100,000 for finish. Decoder completion does not set
capture_ended. Decoder finish, process release and complete local ownership
retirement remain separate observations. Converse retains32 active streams plus
64 positive terminal histories; unknown owners are not evicted for availability.

Each stream captures the original known SpeechClient endpoint and actual STT
model lease. Catalog/other adapters without this explicit protocol refuse; there
is no provider/model/mode fallback. Each publication carries its captured model
pin where configured. Unary STT settlement remains unchanged. This first stream
owner uses a NULL-bound durable native admission, which deliberately remains
unreconcilable after a Converse restart; a new owner instance cannot invent old
stream state or clear that marker. This is conservative crash fencing, not full
restart recovery. Raw PCM is neither logged nor persisted.

The separate `whistle_stream_publication_v1` Session lookup accepts only the exact
original append descriptor from Converse auth, and validates protected writer,
request hash, event/sequence, origin and complete entry before returning it.
SQLite bounds the event ID to128 UTF-8 bytes before materialization; corrupt
metadata cannot bypass the existing JSON acquisition limits.
There is no absence fence. Lookup is available while inference is off. A failed
or unobserved original publication remains paused with its retained result;
there is no automatic append retry or recognition replay. Original HTTP exchanges
and their sole close task are retained through caller cancellation, timeouts and
lease retirement; physical cleanup failure cannot be erased by a valid transcript.

Ten strict additive schema exports have an independent pin. All528 pre-existing
schema files remain unchanged in the recorded qualification. The controlled
metadata lookup uses the digest rather than resending the original PCM body;
all18 original assertions are retained and eight further observations were added.
The original five404 controls, preliminary fixture mistake and raw repaired runs
remain under output/issue-654-*. No real model, microphone, installed-owner,
physical process-reaping or complete Adaptive Live qualification is claimed.
