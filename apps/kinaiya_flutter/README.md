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

Converse uses the moonlit concept in `assets/UIUX Concept`; Agent Chat uses the navy workspace concept. The desktop rail becomes bottom navigation on narrow or enlarged-text layouts. Both views remain mounted, preserving their independent drafts, sessions and active voice capture while navigating. A global activity strip keeps microphone/speech controls reachable from Agent Chat. Changing views never starts capture.

**Call Kinaiya** arms the existing live browser listener; **Tell Kinaiya** records one message. These labels do not implement phone signaling or the mobile calling milestone. Global **Stop microphone** discards an unfinished recording without transcribing it or cancelling Agent work. Pending stops remain visible; a failed or three-second unacknowledged adapter stop stays unconfirmed and retryable. These acknowledgments do not establish physical device behavior.

Live starts only after a user gesture. Capture continues while transcription and replies run; one latest pending utterance bounds the transcription queue. Separate speech/silence thresholds, minimum speech duration and selectable endpoint silence control turn-taking. Each complete recording has its own epoch and 60-second cap. During playback, sustained speech stops local queued audio before an asynchronous server interrupt. Repeated interruptions cannot clear a newer turn's state, and late permission/resume callbacks cannot restart capture/playback after disposal.

Sentences play in order. Synthesis completion is distinct from the browser's audio `onended`; delivery records mark playback only after that local event. Interrupted elapsed time is an estimate, not word alignment or proof of physical audibility. Speech controls do not cancel Agent work. Background evidenced Agent results arrive through the authenticated journal and join the speech queue.

The character stage stays visible after messages arrive, with a current caption and **Conversation history** for complete retained text and delivery notes. **Voice options** contains the existing silence/timing controls; opening it leaves microphone capture and drafts alone. Idle Call/Tell capture is disabled when the adapter reports no microphone, while an active local stop stays reachable when backend readiness changes. The composer follows the selected light/dark surface colors. [Presentation rationale](../../docs/adr/0044-converse-stage-and-voice-options.md) records this focused pass.

## Foreground companion (experimental)

The separate `lib/companion_main.dart` target uses the scoped host protocol from
#108. Enter the Session host's HTTPS address and an owner-issued one-time pairing
code. It never requests an owner credential. The device grant stays in memory;
disconnecting or closing requires a new invitation. Host revocation invalidates
the old grant independently of this local disconnect.

```sh
flutter build web --release --no-web-resources-cdn -t lib/companion_main.dart --output build/companion-web
```

While foregrounded, the companion polls bounded call pages and supports answer,
decline, end, and literal host notes retained after hangup. `accepted_waiting_media`
is shown as waiting for voice connection. This target supplies no voice media,
push handling or background/locked-screen calling. The host must
allow the companion's browser origin when testing the web target. Remote HTTP is
rejected; loopback HTTP is for a client and host on the same machine.

An uncertain action keeps its original revision and idempotency key for retry.
A superseded receipt triggers a refresh. Revocation clears displayed call data;
late responses from a prior pairing cannot repopulate the view. Notes are plain
text, including any markup or URLs they contain, and never become actions.

## Native development targets

The Android and iOS projects use the pinned Flutter 3.47.8 templates. They build
the same existing owner and foreground companion Dart entrypoints:

```sh
flutter pub get --enforce-lockfile
flutter build apk --debug --no-pub -t lib/main.dart
flutter build apk --debug --no-pub -t lib/companion_main.dart
# On macOS with the required Xcode/simulator SDK:
flutter build ios --simulator --debug --no-codesign --no-pub -t lib/main.dart
flutter build ios --simulator --debug --no-codesign --no-pub -t lib/companion_main.dart
```

These alternative development builds share an application identity and replace
each other on installation. CI copies each output before building the other
target and gives it a distinct owner/companion artifact name. The label is
**Kinaiya Dev**, with template launcher icons. Debug APKs and unsigned simulator
applications are development artifacts, not store releases.

Prefer the scoped companion for phone control; it never requests the host owner
credential. Native audio still reports unavailable, and voice media, system
incoming calls, push/background reachability and locked-screen calling remain
unfinished. Native credential-file and presentation import/export remain
unavailable. Appearance preferences and the selected character use private local
storage; manual owner credentials and the companion grant stay in memory.
A process restart loses the companion grant and requires pairing again.

Connect only to the reachable trusted HTTPS host you configure. A phone's
loopback address refers to the phone, not the host computer. The projects expose
no host ports, bypass no certificate checks and add no microphone/VoIP permissions.
See the [native foundation scope and toolchain](../../docs/adr/0048-native-app-foundation.md).

## Agent interface

The wide workspace searches and inspects actual tasks. Selecting a task changes the details panel, while new instructions retain the current Agent session. Task state, evidence, revision and approvals come from the backend; the concept's illustrative weather, harness versions and voice-context timeline are not fabricated. [Layout and lifecycle rationale](../../docs/adr/0024-concept-layout.md) records the tested scope.

Agent Chat consumes the backend command registry, including canonical names, aliases, descriptions and argument hints. Only explicit single-line Agent command input dispatches controls. Quoted/code/path/escaped slash text stays literal; unknown commands never become inference tasks. Commands, new/resume sessions, actual task state and digest/revision-bound approvals use the real backend. Converse has no command UI or parser.

## Character and appearance

Open **Appearance** from either view to import/preview/use a local character pack, export the current pack, restore the original, change colors, reduce motion or use a non-character view. An import changes artwork and palette; it does not import personality, voice, permissions or connection settings. Explicit color overrides take priority over pack colors and Kinaiya defaults. Device reduced motion always applies. Hiding character art retains the transcript and ordinary controls.

The web client stores only presentation preferences and an optional imported pack in its own IndexedDB record. Android/iOS use one bounded app-private presentation file configured to stay outside OS cloud backups. Both paths validate saved data again on restore; storage failures leave the current session usable and display a warning. Native import/export remains unavailable, and other platforms keep session-only settings. [Native storage ownership and verification limits](../../docs/adr/0049-native-presentation-storage.md) distinguish channel/build checks from device restart, backup and power-loss testing.

Use **Export current pack** to obtain the self-contained JSON format. [Pack format and local storage](../../docs/character-packs.md) explains its provenance, checksums and limits. ZIP archives, remote asset fetching, voice/personality import and automatic image generation are not supported by this importer.

## Current limits

The default versioned mascot pack includes 32 sprite frames across core and activity atlases: idle, listening, thinking, speaking, generic work, waiting and attention gestures. Schema 3 adds bounded weighted variants, cooldowns and semantic fallbacks. Confirmed playback takes priority over an armed microphone; work uses observed native task states. Voice-service restoration has skippable approach motion that never delays ready speech or claims model readiness. Reduced motion shows the actual state's first frame; invalid assets fall back to the unchanged original artwork. [Semantic playback and provenance](../../docs/adr/0026-semantic-animation.md) describe the scope. Full emotional cues and specialized work costumes remain unfinished. Microphone quality, echo cancellation and physical audible latency need device testing. Native microphone/playback, mobile calling, complete settings/provider management and the remaining M5 gates are open. Browser verification with synthetic inputs does not establish human-microphone or latency-target performance.
