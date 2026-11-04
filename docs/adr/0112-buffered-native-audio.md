# ADR 0112: Buffered native audio turns

Issue: #451, under #204 and #357. This increment admits original audio to an owner-managed external realtime endpoint. It does not implement duplex capture, video ingestion (#449), or model-specific inference.

## Admission and transport

An explicit host `native_audio` declaration pairs `kind: realtime_v1`, `base_url`, `model`, and optional `api_key_env` with the existing local host Converse selection. Optional `max_output_tokens` (16–4096, default512) and `timeout` (at most90 seconds) bound generation. The endpoint is literal loopback HTTP, optionally ending in `/v1`; the adapter uses its `/v1/realtime` WebSocket interface. Install the pinned `native-audio` extra explicitly when this transport is needed. Ordinary text/voice configurations remain unchanged when the declaration is absent; malformed or unavailable declared audio never falls back to text or TTS.

This first host-only mode refuses resource-managed or independently selected catalog models. The operator manages the external engine and qualifies its protocol. A matching alias does not prove immutable model weights or server incarnation. No native audio token-count interface is available here: qualified journal history is paged to exhaustion under existing time/byte bounds and sent as labelled historical data, while the current original PCM is sent as audio. Kinaiya neither fabricates an exact token count nor clips old turns to a fixed reply count. Endpoint overflow refuses the operation. Selected memory recall is explicitly unavailable in this mode.

A transcription-intent connection may initially announce a valid realtime session before applying the explicit transcription update. Both initial transcription and initial realtime PCM envelopes are validated. In either case, the matching transcription acknowledgement is mandatory before any input audio is sent; the URL intent alone grants no release.

The authenticated `POST /v1/converse/native-audio/clips` accepts strict session/thread/epoch, utterance/request UUIDs, disclosure scope and base64 WAV. Only canonical 44-byte-header PCM16 mono16000 Hz WAV, nonempty and at most30 seconds, is accepted. A bounded raw-body gate precedes JSON validation and never reflects invalid audio. Original decoded PCM is transcribed through the same captured endpoint with transcription intent. The authoritative authenticated-user `conversation.window` retains its audio digest, scope and native identities for Decisions and the normal transcript. Native conversational generation receives that same original PCM, not a text replacement of its transcription.

## Ownership and replay

A protected SQLite intent ledger durably reserves request and utterance identity before ASR. Exact in-process duplicates join the same original task. Changed requests, lost clips after restart, stale epochs and unavailable originals refuse without a second ASR. The ledger retains at most4096 intents without eviction; RAM retains at most32 clips and16MiB, reserving room for input copies and up to2MiB output before admission. Saturation refuses rather than evicting a live receipt.

The provider retains original acquisition, send/receive, response and close futures across cancelled observers. Repeated cancellation cannot restart the bounded cleanup wait or transfer custody to a replacement transport. Export requires confirmed close and a final authority/deadline check. Unconfirmed cleanup stays unavailable with its original work retained. Owner shutdown similarly retains the original provider and closer.

## Native output and browser consent

Strict matching session/response/item identities, terminal completion, exact text consistency, bounded PCM and declared sample rates are required. Only an unchanged guarded reply can retain native PCM: if the runtime replaces an unsupported work claim, the original speech is suppressed. The confirmed journal append caches the reply before it becomes visible to speech consumers.

The existing `/speech` endpoint validates that exact native event and emits its retained PCM through existing generation, stop and delivery ownership. It bypasses separate TTS configuration and never synthesizes a replacement for missing native speech. Flutter's explicit audio-import gesture owns only its matching turn. It defers fast-polled output until the exact upload receipt confirms the original source, and retires that permission on source, connection, session, epoch or turn changes. Playback completion is reported only by the real player completion callback. This import permission does not arm a microphone or authorize unrelated queued speech. The web import gesture requests playback-context activation before opening the file picker, without waiting for resume, starting PCM, or acquiring a microphone. Later queueing requires the actual context to be running; only original source completion acknowledges playback.

## Verification and limits

Independent scripted-transport and real Store/runtime/API tests cover original PCM, journal identity, complete history, no-count/no-TTS traps, replay, limits, stale authority, held cleanup and native delivery. Flutter tests cover canonical WAV, HTTP/receipt identity, fast polling, unarmed import playback and retirement. Existing schema hashes remain unchanged. Exact local test and CI outcomes belong in the PR; a scripted transport is not real inference or audible playback proof. The rehearsal must separately record actual external-engine speech input/output and browser playback.
