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

**Settings** opens the persistent [Options workspace](../../docs/adr/0077-options-workspace-and-harness-models.md).
Categories live in its sidebar; paired categories open separate sections with
Back navigation. The overview automatically fits the complete approved
Kinaiya/gear illustration into the available workspace.
Character visibility and imported packs follow the presentation preferences.
Appearance, character, voice, memory, connection and MCP controls stay inside
Options. Home/Converse and Agent remain mounted, preserving drafts, audio and
sessions. Opening or navigating settings does not start inference or work.

**Models & providers** separates Models, Providers and **Harness Customization**.
Each native harness opens its own model editor. Choose inheritance or a
configured profile and supported reasoning level; Save applies to new tasks.
Accepted tasks keep their captured model. The host persists revision-bound
settings and native operation receipts; an unconfirmed save remains visible
across close/reopen, and an explicit retry uses the identical request.

Models and each harness editor also contain **Auxiliary models**. Choose an
analysis, compression, title generation, delegated reasoning or harness design
purpose, then inherit or save a supported profile/reasoning choice. Harness
purpose overrides take priority over global purpose defaults and primary model
inheritance. These are explicit bounded text side calls through `consult_model`;
they do not run tools, replace protected history or create a child Agent.
Only host-declared measured text adapters are offered; unavailable routes refuse
without a hidden fallback. Accepted tasks retain their native purpose pins.

Provider credentials and model profile definitions stay on the host. The
provider page includes Meta Model API and LM Studio information; Meta account
sign-in is not implemented. Converse/STT/TTS/Decisions model editing still uses
host configuration. The [full catalog](../../docs/options.md) separates working,
host-managed and planned controls. Model selection, disclosure, permissions and
isolation remain separate owners.

Build information in Settings uses optional
`--dart-define=KINAIYA_BUILD_LABEL=...` and
`--dart-define=KINAIYA_BUILD_REVISION=...` values. Without them it says
**Development build**; a label alone does not establish verification.

## Voice behavior

Options → Voice exposes session-scoped Dynamic VAD, speech sensitivity and
room recalibration on the web adapter. Calibration starts with an explicitly
armed Call; requesting recalibration while off only queues it. Clip fallback
also exposes speech/interrupt confirmation and ending silence. Continuous
windows retain Decisions timing. See [ADR0079](../../docs/adr/0079-adaptive-vad-and-top-aligned-options.md)
for defaults, bounds, prototype provenance and qualification limits.

The [updated owner design](../../docs/adr/0059-home-and-optional-converse.md) opens
on moonlit **Home**, with the animated mascot seated on water. Drag the handle
on the Home connection panel to move it around the scene; its position is kept
while that Home view remains mounted. **Talk with Kinaiya** arms the existing live browser listener; **End conversation** stops
that listener. The mascot and **Setup** open model/conversation settings.
The optional **Converse** view uses the supplied close-up character art, complete
scrollable transcript, text composer, microphone controls and model summary.
The default portrait's mouth follows the played audio, closing during silence
and when playback stops. Optional timed viseme cues select mouth shapes; current
providers use the audio-volume fallback. **Speech mouth movement** in Character
& motion controls this behavior; Reduce motion also keeps the mouth still.
Imported packs retain their own speaking animation. The
[mouth playback contract](../../docs/adr/0064-speech-mouth-playback.md) records
cue format, timing, lifecycle and verification limits.
**Tell Kinaiya** remains available in Converse and Converse chat for one recorded
message. These browser controls do not implement mobile calling or signaling.

The connected sidebar has Home, optional Converse, Agent Chat, Character & motion,
Projects, Generated Documents, Generated Media and Options; narrow/large-text
layouts use a Menu drawer. Character and motion controls live in Options.
Projects groups Agent sessions. The generated libraries list native write
receipts with creation times, file types and source sessions, without scanning
workspace directories ([ADR0080](../../docs/adr/0080-owner-projects-and-generated-libraries.md)). Home and
Converse share one mounted voice owner; Agent stays independently mounted. View
changes preserve drafts, sessions, microphone and playback and never start capture.

**Kinaiya setup** reads configured conversation, STT, TTS and Decisions model
roles from the authenticated host endpoint. Configuration does not prove another
worker is loaded/ready. Models and reasoning are host-managed and read-only;
this host supports text dialogue with STT, not direct audio, image/video or camera
input. **Save setup** only saves the optional Converse view on this device.
Hiding it preserves Home voice and does not change backend Converse enabled state.
**Sleep** and **Wake Kinaiya** separately control the authenticated Converse
owner intent. Home shows the sleeping sprite on the same moonlit background when Converse is unavailable
or the owner has explicitly put it to sleep; this appearance does not assert
owner intent. A resource pause alone does not imply sleep. Waking waits for
actual service readiness and does not start the microphone. Agent stays usable.

Connection/disclosure, model selection and permissions remain separate owners.

Global **Stop microphone** discards unfinished audio without transcription or
Agent cancellation. Pending stops remain visible; failed or three-second
unacknowledged adapter stops stay unconfirmed and retryable. These acknowledgments
do not establish physical device behavior.

Live starts only after a user gesture. Capture continues while transcription and replies run; one latest pending utterance bounds the transcription queue. Separate speech/silence thresholds and minimum speech duration guard acoustic overlap. Selectable endpoint silence ends only fallback clips; live window mode streams audio continuously and Decisions chooses when to wait, respond, or interrupt, with no silence timer ending the turn. Each fallback recording has its own epoch and 60-second cap. During playback, sustained speech stops local queued audio before an asynchronous server interrupt. Repeated interruptions cannot clear a newer turn's state, and late permission/resume callbacks cannot restart capture/playback after disposal.

Sentences play in order. Synthesis completion is distinct from the browser's audio `onended`; delivery records mark playback only after that local event. Interrupted elapsed time is an estimate, not word alignment or proof of physical audibility. Speech controls do not cancel Agent work. Background evidenced Agent results arrive through the authenticated journal and join the speech queue.

Home retains its scene after messages arrive and shows a current caption; **History** in Converse and **Converse chat** preserve complete retained text and delivery notes. **Voice options** shows live Decisions timing with streaming windows, or fallback clip silence otherwise; opening it leaves capture and drafts alone. Microphone start is disabled when unavailable, while active local stop stays reachable through backend readiness changes. Converse chat retains recording import, one-shot Tell, Voice options and Memory; Options also reaches the existing owners. Imported packs and explicit palette overrides retain their presentation precedence.

## Saved Converse notes (experimental)

**Memory**, available in Settings and beside Voice options in Converse chat, opens the owner dialog for the current
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

The Agent workspace has independently collapsible Harnesses and Workspace sidebars,
with separate sheets on narrow screens. Harnesses shows the protected Core and real
registered context selectors; Workspace lists the selected harness's native sessions,
including empty sessions. Native eligibility controls New session. Session selection
reads actual journal history and restores that session's draft; it starts no work or
promotion. Historical null associations remain explicit. Task cards follow the
selected session, while pending approvals remain global. Unsupported inspection,
uncertain creation and unavailable versions are visible. See the
[sidebar ownership and verification limits](../../docs/adr/0065-agent-sidebars.md).

On wide screens, drag the divider beside any Agent sidebar to resize it or drag
it toward its outer edge to collapse it. The divider remains visible and can be
dragged outward to reopen the sidebar; its arrow also reopens it at the previous
width. Focused dividers respond to the left and right arrow keys. Widths adapt
to the available window and are remembered while the view remains mounted, not
across app restarts.

The conversation groups recorded tool activity under expandable summaries.
Recorded shell output appears as selectable monospace text; the recorded shell
summary may be shorter than the original command. No tool is run by opening a
group. The message rail jumps to loaded recorded messages; load additional history
before expecting markers for it. The [workspace plan](../../docs/agent-workspace-panel-plan.md)
describes the proposed Files and Browser panels. An interactive terminal is deferred.

Agent Chat consumes the backend command registry, including canonical names, aliases, descriptions and argument hints. Only explicit single-line Agent command input dispatches controls. Quoted/code/path/escaped slash text stays literal; unknown commands never become inference tasks. Commands, new/resume sessions, actual task state and digest/revision-bound approvals use the real backend. Converse has no command UI or parser.

## Character and appearance

Open **Appearance** from either view to import/preview/use a local character pack, export the current pack, restore the original, change colors, reduce motion or use a non-character view. An import changes artwork and palette; it does not import personality, voice, permissions or connection settings. Explicit color overrides take priority over pack colors and Kinaiya defaults. Device reduced motion always applies. Hiding character art retains the transcript and ordinary controls.

The web client stores only presentation preferences and an optional imported pack in its own IndexedDB record. Android/iOS use one bounded app-private presentation file configured to stay outside OS cloud backups. Both paths validate saved data again on restore; storage failures leave the current session usable and display a warning. Native import/export remains unavailable, and other platforms keep session-only settings. [Native storage ownership and verification limits](../../docs/adr/0049-native-presentation-storage.md) distinguish channel/build checks from device restart, backup and power-loss testing.

Use **Export current pack** to obtain the self-contained JSON format. [Pack format and local storage](../../docs/character-packs.md) explains its provenance, checksums and limits. ZIP archives, remote asset fetching, voice/personality import and automatic image generation are not supported by this importer.

## Current limits

The default versioned mascot pack has 36 atlas cells across seated-idle, core and activity textures. Idle and idle-blink use two curated seated-on-water frames; listening, thinking, speaking, generic work, waiting and attention retain their original frames. Schema 3 adds bounded weighted variants, cooldowns and semantic fallbacks. Confirmed playback takes priority over an armed microphone; work uses observed native task states. Voice-service restoration has skippable approach motion that never delays ready speech or claims model readiness. Reduced motion shows the actual state's first frame; invalid assets fall back to the unchanged original artwork. [Semantic playback and provenance](../../docs/adr/0026-semantic-animation.md) describe the scope. Full emotional cues and specialized work costumes remain unfinished. Microphone quality, echo cancellation and physical audible latency need device testing. Native microphone/playback, mobile calling, complete settings/provider management and the remaining M5 gates are open. Browser verification with synthetic inputs does not establish human-microphone or latency-target performance.
