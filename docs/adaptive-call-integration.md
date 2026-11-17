# Adaptive Live host and Call integration (#623)

Status: implementation in progress. The normal release gate remains false and
the Call screen still refuses Adaptive Live. This work is the host/transport
foundation for completing Call, migration and rollout together.

## Native host ownership

The trusted launcher assembles one `LiveConverseController` when the qualified
release gate is enabled. It retains the existing runtime's personality, model,
context, speech and cleanup owners. Its original API lifespan starts the live
consumer and required v2 Decisions monitor; the launcher preserves its existing
resource/model/journal cleanup and does not start a second window loop.

Rolling Windows remains independently selectable. Disabling its host capability
does not disable the native live consumer or advertise v1 window support.
The corresponding Decisions factory shares the original journal, Agent client
and model settings with the native live inference and Agent consumers. The
existing finalized-turn and optional rolling workers retain their own loops.
Readiness inspection starts no inference, capture or provider probe.

The trusted rollout value is an exact boolean. While false, implicit defaults
retain existing behavior. When the complete feature is qualified and the gate
is enabled, an implicit host pointer resolves to Adaptive Live; explicit saved
and configured choices remain unchanged. Preference revision and next-capture
semantics remain intact. An unsupported provider/input stays visibly unavailable.

## Frontend native stream contract

`KinaiyaClient.liveStreams` owns its independent authenticated, bounded transport.
It implements the existing `converse_live_stream_v2` ownership, begin, PCM,
finish, lookup, reconcile and cancel endpoints. The normal and labeled mock
constructors require the appropriate independent factory when using injected
transports. Inspection and construction acquire no microphone or inference.

An original operation detaches its complete request and canonical UTF-8 digest.
The client checks original phase, owner/capture, decoder, scope, counters,
registration and paired transcript/capture publication. Lookup and reconcile
send only exact metadata, never PCM. A retained publication beside an unknown
transport result does not become an audio acknowledgment. Request/response
bounds are 96,000 bytes/448 KiB; control operations retain their 4 KiB bound.
Duplicate keys, unsafe/fractional counters and malformed UTF-8 are refused.

The existing transport retains actual sends, subscriptions and cleanup beyond
facade deadlines and client replacement. A timeout remains unconfirmed; it
does not free actual capacity or automatically resend the original operation.

Decoder rollover requires the exact confirmed final original, positive paired
source, decoder completion and actual decoder/HTTP ownership retirement.
Process release is separate: the same logical microphone deliberately retains
its native STT lease across decoder segments. A begin/PCM proof or a model
completion cannot establish this final-source/cleanup boundary.

The [qualification record](evidence/adaptive-call-integration-2026-11-17/README.md)
retains commands, unchanged input pins, first failures and repairs. Actual
Dart transport runs against fresh native HTTP, SQLite and synthetic Whistle IPC
owners, using the pinned existing Flutter dependency cache with `--no-pub`.
It verifies two decoder starts, one PCM submission, one finish and one
cancellation, plus metadata-only observation and one retained STT lease.

## Remaining integration

Connect the actual CharacterView Call owner to this transport and the optional
incremental PCM adapter. Preserve bounded contiguous input through decoder and
utterance boundaries, retain pending native and browser operations through
stop/reconnect/replacement, and qualify actual playback/backchannel capture,
echo prevention, mute, accessibility, no-audio and dependency loss. Add timing
instrumentation and faithful same-conversation mode comparisons before enabling
the default. Capture/STT/model/playback observations remain distinct. Model
answer, classification, persona and speech quality are diagnostic evidence;
architecture and harness correctness determine acceptance.
