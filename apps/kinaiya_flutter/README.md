# Kinaiya Flutter client

Character View connects to Converse; Agent Chat connects directly to Agent. The web client supports text, microphone recording, user-armed continuous listening, importing an audio recording, streamed speech playback, task evidence, steering and approvals. Native voice remains a text-only stub.

## Run

The reference environment uses Flutter 3.47.8 / Dart 3.13.7. From the repository root:

```sh
cd apps/kinaiya_flutter
flutter pub get
node --test web/audio_bridge.test.js
flutter analyze
flutter test
flutter build web --release --no-web-resources-cdn
python -m http.server 8780 --bind 127.0.0.1 --directory build/web
```

Start the configured Python services separately. Export an owner-only credential with `kinaiya --data-dir data owner-login`; Connection setup can load that private file without copying other service credentials into the browser. Credentials remain in memory and never enter URLs, chat, logs or browser storage. Select an inference-disclosure policy matching the actual backend. A local-only selection blocks sending text/audio when the backend reports cloud inference; pressing Enter follows the same rule as Send. Contributor development profiles accept public or synthetic content only.

## Voice behavior

Live starts only after a user gesture. Capture continues while transcription and replies run; one latest pending utterance bounds the transcription queue. Separate speech/silence thresholds, minimum speech duration and selectable endpoint silence control turn-taking. Each complete recording has its own epoch and 60-second cap. During playback, sustained speech stops local queued audio before an asynchronous server interrupt. Repeated interruptions cannot clear a newer turn's state, and late permission/resume callbacks cannot restart capture/playback after disposal.

Sentences play in order. Synthesis completion is distinct from the browser's audio `onended`; delivery records mark playback only after that local event. Interrupted elapsed time is an estimate, not word alignment or proof of physical audibility. Speech controls do not cancel Agent work. Background evidenced Agent results arrive through the authenticated journal and join the speech queue.

## Agent interface

Agent Chat consumes the backend command registry, including canonical names, aliases, descriptions and argument hints. Only explicit single-line Agent command input dispatches controls. Quoted/code/path/escaped slash text stays literal; unknown commands never become inference tasks. Commands, new/resume sessions, actual task state and digest/revision-bound approvals use the real backend. Converse has no command UI or parser.

## Current limits

The default versioned mascot pack includes 16 sprite frames for idle, listening, thinking and speaking. Reduced motion freezes the first frame; invalid assets fall back to the unchanged original artwork. [Renderer and provenance](../../docs/adr/0015-mascot-sprites.md) describe the tested scope; a complete expression/work-animation library remains unfinished. Partial STT remains unfinished. Microphone quality, echo cancellation and physical audible latency need device testing. Native microphone/playback, mobile calling, complete settings/provider management and the remaining M5 gates are open. Browser verification with a synthetic recording does not establish human-microphone or latency-target performance.
