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

## Settings and design handoff

**Settings** opens a searchable hub for service connection/privacy, appearance,
voice options, saved Converse notes, Agent controls and MCP connections. It
reuses the existing editors and preserves both mounted conversation panes.
Opening, searching or closing the hub starts no connection, capture, task or
inference request. Unavailable destinations explain what is missing. Agent
controls fill a command draft; selection does not send it.

The [full options catalog](../../docs/options.md) records working, host-only and
planned controls separately, including their scope, defaults and native owner.
Its stable controls now use the owner-supplied Penpot visual style, with matching
sections for omitted advanced options as their native editors become available.
Provider choice, disclosure consent, permissions, isolation and cost limits
remain separate controls.

Build information in Settings uses optional
`--dart-define=KINAIYA_BUILD_LABEL=...` and
`--dart-define=KINAIYA_BUILD_REVISION=...` values. Without them it says
**Development build**; a label alone does not establish verification.

## Voice behavior

The [owner design implementation](../../docs/adr/0054-penpot-owner-interface.md) supplies navy/blue surfaces, local Inter fonts and responsive Options/Agent panels. Default Converse uses a background-only derivative of the moonlit illustration beneath the existing animated mascot; explicit palettes and imported packs keep the painter scene. The desktop rail becomes bottom navigation on narrow or enlarged-text layouts. Both views remain mounted, preserving their independent drafts, sessions and active voice capture while navigating. A global activity strip keeps microphone/speech controls reachable from Agent Chat. Changing views never starts capture.

**Call Kinaiya** arms the existing live browser listener; **Tell Kinaiya** records one message. These labels do not implement phone signaling or the mobile calling milestone. Global **Stop microphone** discards an unfinished recording without transcribing it or cancelling Agent work. Pending stops remain visible; a failed or three-second unacknowledged adapter stop stays unconfirmed and retryable. These acknowledgments do not establish physical device behavior.

Live starts only after a user gesture. Capture continues while transcription and replies run; one latest pending utterance bounds the transcription queue. Separate speech/silence thresholds and minimum speech duration guard acoustic overlap. Selectable endpoint silence ends only fallback clips; live window mode streams audio continuously and Decisions chooses when to wait, respond, or interrupt, with no silence timer ending the turn. Each fallback recording has its own epoch and 60-second cap. During playback, sustained speech stops local queued audio before an asynchronous server interrupt. Repeated interruptions cannot clear a newer turn's state, and late permission/resume callbacks cannot restart capture/playback after disposal.

Sentences play in order. Synthesis completion is distinct from the browser's audio `onended`; delivery records mark playback only after that local event. Interrupted elapsed time is an estimate, not word alignment or proof of physical audibility. Speech controls do not cancel Agent work. Background evidenced Agent results arrive through the authenticated journal and join the speech queue.

The character stage stays visible after messages arrive, with a current caption and **Conversation history** for complete retained text and delivery notes. **Voice options** shows live Decisions timing when streaming windows are available, or fallback clip silence otherwise; opening it leaves microphone capture and drafts alone. Idle Call/Tell capture is disabled when the adapter reports no microphone, while an active local stop stays reachable when backend readiness changes. The composer follows the selected light/dark surface colors. [Presentation rationale](../../docs/adr/0044-converse-stage-and-voice-options.md) records this focused pass.

## Saved Converse notes (experimental)

**Memory**, beside Voice options, opens the owner dialog for the current
Converse conversation. It lists existing notes, shows their full text on request,
and lets the owner select up to eight active notes with **Use selected notes**.
**Stop using notes** does not stop the conversation or Agent work. Saved access
and runtime use have separate controls; turning saved access off retains notes,
and restarting Converse starts runtime use off. Opening the dialog creates no
session or note and starts no voice or inference request.

This requires the Session host's optional Converse memory feature. An unavailable
feature or refused owner access is shown separately from an empty note list.
Configured use does not establish that a local model can count or consume a note
closure. Failed acknowledgments may remain unconfirmed; refreshing equal values
does not prove the old request failed, and the app does not replay changes.
Those uncertainty records stay in RAM beyond the dialog lifetime, with no note
or use settings saved in appearance storage. Note creation/editing and deletion
UI, automatic extraction and human recall/latency qualification remain later work.

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

The default versioned mascot pack has 36 atlas cells across seated-idle, core and activity textures. Idle and idle-blink use two curated seated-on-water frames; listening, thinking, speaking, generic work, waiting and attention retain their original frames. Schema 3 adds bounded weighted variants, cooldowns and semantic fallbacks. Confirmed playback takes priority over an armed microphone; work uses observed native task states. Voice-service restoration has skippable approach motion that never delays ready speech or claims model readiness. Reduced motion shows the actual state's first frame; invalid assets fall back to the unchanged original artwork. [Semantic playback and provenance](../../docs/adr/0026-semantic-animation.md) describe the scope. Full emotional cues and specialized work costumes remain unfinished. Microphone quality, echo cancellation and physical audible latency need device testing. Native microphone/playback, mobile calling, complete settings/provider management and the remaining M5 gates are open. Browser verification with synthetic inputs does not establish human-microphone or latency-target performance.
