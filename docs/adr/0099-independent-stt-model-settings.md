# ADR 0099: Independent owner STT model selection

October 31, 2026. Source implementation for [#208](https://github.com/rgoza1996/Kinaiya/issues/208).
This increment adds a native speech-to-text owner and embedded Audio editor.
It does not complete the Models group or qualify live conversation, physical
microphones/cameras, direct multimodal input, TTS or automatic fallback.

## Selection and provider catalog

Converse independently owns either its existing host STT adapter or one exact
`meta_stt` catalog generation. The new declaration contains only its kind,
explicit `/v1` origin, fixed `muse-voice-transcribe-1.0` model, `allow_cloud:true`,
protected credential generation and integer timeout (1–300 seconds, default120).
The actual existing adapter is buffered, bounded Muse Voice Transcribe. A text
model declaration is not an audio transport. Existing four catalog kinds keep
their original normalized configurations and digests. STT capability metadata
declares audio input, not measured text streaming or readiness.

Inspection and Save do not read credentials, construct an adapter, connect,
load a model, open a microphone or make paid calls. Catalog admission captures
the exact immutable declaration and active credential generation together in
one native read transaction. Each actual operation owns a fresh Meta STT HTTP
adapter; the host adapter retains its shared service lifetime, including TTS.
Cloud disclosure and Converse/resource policy are checked independently before
decode/export and after awaited validation. Saving a model grants no capture,
input-disclosure or execution permission. No implicit host/cloud fallback exists.

## Protected state and recovery

Owner-only endpoints are:

- `GET/POST /v1/converse/options/stt-model`
- `POST /v1/converse/options/stt-model-changes/lookup`

Changes carry strict schema1.0, expected durable backend ID, owner instance,
revision and state token, original idempotency key and complete selection.
Lookup accepts that exact original request; it never reapplies it. Recorded
receipts are historical confirmation, not a claim that the selection is active
today. Same-backend absence is permanently fenced only when the original owner
instance is retired or the native revision strictly advanced. Foreign-backend
lookup refuses rather than manufacturing absence. Unknown writes retain their
exact body/key; no status-code guess, matching-value inference or automatic retry
settles them.

The protected `converse-stt-model.sqlite` uses affirmative atomic creation,
an exact three-table/version1 schema, bounded SQL/JSON, a durable backend and
complete unique receipt history. Existing empty/corrupt storage refuses instead
of resetting host defaults. Creation through initialization has a bounded
five-second directory-lock acquisition; a separate nonblocking lifetime lock
protects real operation ownership. Logical shutdown cannot release that lock
while a real lease or unresolved native admission remains. There are at most
64 leases/admissions and 10,000 immutable settings receipts. Requests are bounded
to4KiB before parsing with one five-second body deadline; responses are bounded
to64KiB and carry `Cache-Control:no-store`.

New saves require `settled_converse_operations`: actual preparation, text,
windows and buffered publication, speech/transcription, memory/playback work,
leases and cleanup must settle. The final admission/CAS/commit has no await gap.
Snapshot `selected_available` describes an implemented configured interface;
`selected_info` may remain null for a trusted callable host with undeclared
identity. Neither field claims verified weights, loaded state or quality.

## Operation ownership and provenance

`/transcribe` and `/audio/windows` capture the server's exact STT lease and
immutable `STTModelPin`. The pin includes role/source, backend/instance,
settings revision, exact catalog identity/revision when applicable and public
provider/model/locality when declared. It contains no credential, origin URL or
local artifact path. Host identity remains explicitly unknown when undeclared.

Actual decoder/HTTP tasks, publication and adapter cleanup remain owned through
caller cancellation. A cancellation request or waiting timeout does not release
capacity. The finalizer waits the actual worker and validates any successful
native append against its complete captured request before settling publication.
A late valid success can therefore settle its own uncertainty; it cannot erase
an independent worker or cleanup failure. Session append still atomically checks
thread/epoch and window revision. Off after publication admission does not imply
rollback of an authenticated hypothesis already admitted for persistence.

The native pin is returned by `/transcribe` and persisted on new audio-window
events. Client-supplied pins are rejected on both window inputs; Session accepts
window STT provenance only from Converse. Legacy adapter JSON cannot supply this
reserved field. Historical absent pins keep their original serialization and
digest. Exact audio-window replay precedes current model/credential capture and
returns its original entry without transcription. The existing generic journal
transport still has its prior parsing/cleanup limits; this increment adds a
complete local append projection check, not a new generic strict transport.

## Unresolved local-worker gate

Extension: [ADR 0100](0100-native-stt-worker-settlement.md) adds exact positive
settlement for supported local Whisper workers and their original transcript
publications. The limitation below still applies to unsupported/legacy markers,
changed worker instances and missing required publication proof.

Legacy Whisper uses `to_thread` in a separate service; a closed HTTP request
does not prove its native decoder stopped. Managed workers also need actual
settlement proof. Before dispatching such host work, STT durably records an
exact bounded admission marker. Only that owned marker can be removed, after
confirmed successful native work and required publication/cleanup. Failure,
cancellation, hard death or uncertain commit/publication leaves the marker.
Restart does not clear it or reinterpret it through today's host/model.

An unresolved admission is exposed as `cleanup_unknown` and blocks fresh capture
and model changes. There is intentionally no UI reset. Native worker status and
positive settlement/restart reconciliation are an explicit remaining capability;
without that proof this owner stays unavailable for further inference. Normal
successful transcription remains usable. Shared Session/speech clients are
retained while their actual users or uncertain cleanup remain, rather than being
closed underneath late publication or independent TTS cleanup.

## Evidence and remaining work

Source/static review and generated schemas are separate from exact-head CI and
real backend/device qualification. No tests, paid/model calls, credential probes,
downloads or physical capture were run for this source increment. The cumulative
$10 test budget remains unchanged. Native worker reconciliation, independently
selected TTS, other STT backends, direct Omni input and wider model/fallback
controls remain open. Do not infer completion from the presence of an editor.
