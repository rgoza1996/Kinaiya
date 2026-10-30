# 0059: Home and optional Converse share one conversation owner

Issue: [#138](https://github.com/rgoza1996/Kinaiya/issues/138), following #128.

The owner updated the native Penpot design on October 30, 2026. The connected
sidebar file now has a moonlit Home, a separate optional Converse interaction,
and model/conversation setup. This explicit change supersedes the older
character-to-Agent navigation described in Roadmap §15.2: clicking the Home
character opens setup. Agent remains a separate, direct destination.

The initial source export was preserved at `assets/penpot/Kinaiya-Converse-Agent.penpot`,
SHA256 `2cf97cfe945a22dfebb358d96928f4aa0c6b658fc0fa97e32a80bf10b95d64d8`.
Its first page, “00 · Home & Converse · Updated”, includes the 26 connected
boards inspected for this change, including the latest sleeping Home state. Existing example messages, weather, projects,
model names and task outcomes are design examples, never runtime fixtures.
The later consolidated master replaces that archive at the same path;
[ADR 0060](0060-converse-handoffs-and-motion.md) records its new digest and scope.

## Ownership and compatibility

One mounted `CharacterView` continues to own microphone capture, the live audio
window, pending transcription, playback, cancellation epochs and Converse draft.
Home and Converse are presentations of that same owner. Agent stays mounted
independently. Navigation, opening setup and saving view visibility do not create
sessions, dispatch commands, call inference, stop audio or change disclosure.
The global audio strip preserves stop controls outside the interaction panel.

`show_converse_view` is a local presentation preference. Old six-field preference
records remain valid and default this field to true; new records require a
strict boolean. All old fields, pack validation and strict unknown-field refusal
remain. Hiding the entry returns an open Converse view to Home without setting
the backend's separate Converse enabled intent. Save merges into current
presentation preferences, so unrelated changes are retained. Storage uncertainty
keeps its existing presentation notice and session-only behavior.

Home uses the existing water-anchored illustration and seated semantic sprite.
Converse uses the unmodified supplied close-up atlas for the default character;
thinking and speaking poses follow actual generation/playback state. These
static expression references do not establish phoneme timing or new emotion
inference. Imported packs, character visibility, theme overrides and reduced
motion retain their existing owners. The interaction canvas and history remain
bounded; narrow and enlarged-text layouts scroll instead of hiding controls.

## Owner-off sleeping Home

The latest native file adds **Home / Offline · Sleeping**, with a separate full
scene illustration and editable Sleep/Wake controls. Sleep and Wake operate the
existing authenticated Converse owner intent. They never disable Agent or treat
the optional-view preference as a service switch. Confirmed owner-off uses the
supplied sleeping scene; resource handoff, failed health reads and connection
loss keep distinct status instead of claiming intentional sleep. An accepted
Wake is not readiness and never starts microphone capture automatically.

Control responses and reconciliation remain bound to the connection that issued
them. A timeout leaves the outcome uncertain until a fresh owner observation;
it does not justify retrying a mutation or presenting a successful state.
Existing capture/playback cancellation owns local cleanup and prevents late
recording or speech completions from rearming after Sleep. The static sleeping
reference respects character visibility and custom presentation precedence.

## Capability boundaries

The [read-only setup contract](../converse-setup.md) projects host declarations
for the conversation, STT, TTS and Decisions models. Configuration is not proof
that another process has loaded or is ready. The renderer never treats that
endpoint as consent or a model-load receipt. Reads are connection-bound; late
responses after disconnect or owner replacement cannot replace current data.

The current host supports text dialogue with microphone transcription, not
image/video/direct-audio dialogue or camera capture. Independent model hot-swap,
STT bypass and runtime reasoning editing are not implemented. Setup explicitly
shows those limits with read-only declarations; Save changes only optional view
visibility. A future picker requires owner-authored profiles, resource admission,
operation-boundary switching, native model verification, cleanup and disclosure
revalidation. A vision-capable checkpoint alone does not implement its transport.

The sidebar connects Home, Converse, Agent, Character & motion, Projects, Files,
Media and Options. Character & motion opens the existing appearance owner;
Options retains all existing editors and adds model setup. Projects, Files and
Media expose their actual unavailable integration state, not the concept's
invented folder/media library. Home's clock uses device time; Agent status uses
current authenticated task observations, including an unavailable state when
those observations are stale. No location/weather access is added.

## Verification

`model_setup_test.dart` covers declaration validation, read-only requests,
Save/Cancel, narrow 2× text, merging preferences and stale reads.
`penpot_navigation_test.dart` checks the retained owner, armed microphone,
sessions, independent drafts, Agent pane and real appearance destination through
Home/Converse navigation and hiding. `home_sleep_test.dart` covers owner intent
versus effective resource availability, malformed/obsolete observations,
uncertain writes and guarded late responses, mic/playback cancellation and
restoration without auto-arm. It also checks narrow sleeping layouts and custom
presentation precedence. Existing voice, disclosure, stop and settings
regressions continue to exercise their original owners. Backend
contract tests cover authentication, bounded/redacted declarations, revision
identity and absence of mutation/provider calls. Synthetic checks do not
establish human voice quality, physical audible latency or mobile calling.
