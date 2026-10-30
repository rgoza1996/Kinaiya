# 0055: Seated idle and content-controlled live timing

Issue #130 follows the owner's correction to #128/#129. Idle Kinaiya sits on
the water, as in the moonlit concept. Add a separate transparent four-frame idle
atlas to the existing bounded pack format and redirect both idle variants;
listening, speaking, thinking and work/attention frames retain their original
sources. The character remains separately animated over the decorative scene.
Import validation, image bounds/hashes, reduced motion, stale decode ownership
and honest unavailable-artwork fallback retain their existing responsibilities.
The prompt record pins actual output geometry and source bytes.

The illustrated water belongs to the actual mascot substage, including its
smaller allotment when history is visible, rather than scaling against the whole
viewport. Captions and status sit outside the image/mascot panel so enlarged labels cannot
push the water below the seated contact. The hero aligns to the panel bottom and
the illustration covers a 1.15-times-panel-height surface, preserving aspect ratio, placing the seated contact ring within its water
region at phone, desktop and tall/ultrawide sizes. The stage can use the available
height on tall screens; the legacy non-illustrated stage retains its prior cap.
This is trusted presentation layout and does not change capture or model state.

## Concept navigation and voice surface

The Converse bottom surface contains only Call and Tell in default and custom
appearances. Header Converse
chat opens text input and supporting tools without arming capture or losing the
draft. Its callbacks are bound to the current owner/route; owner replacement or
disposal invalidates and removes the dialog. Text and actions wrap on narrow,
enlarged-text layouts. Voice options and Memory also remain available in Settings.

The full concept sidebar provides Home, Converse, Agent Chat, People, Files,
Documents, Gallery and Settings, with expansion and a narrow-screen drawer.
Unsupported areas report their availability; they do not fabricate user content
or imply completed product features. Existing Converse and Agent panes retain
state and active local stop controls while the owner navigates.

## Live timing already has no silence turn timer

Continuous audio-window capture bypasses the legacy end-of-silence branch in
`audio_bridge.js`. CharacterView samples bounded windows while speech continues;
the window classifier selects IGNORE, WAIT_FOR_NEXT_WINDOW,
RESPOND_INTERRUPT_USER or RESPOND_WITHOUT_INTERRUPTING_USER from content/context.
The native voice controller applies that decision. A noninterrupting reply waits
for an acoustic opening; the interrupting action can proceed during speech.
Acoustic speaking hints and bounded capture pacing are not a fixed turn-ending
silence threshold and are retained. Tell remains an explicit one-message gesture.

Only the older clip-capture compatibility path uses the adjustable end silence.
Voice options describes that path as a fallback and shows its control only when
the active capture mode uses it; window mode describes model-controlled timing.
Generic settings/tooltip text must not imply silence governs live decisions.
Opening, dismissing or inspecting options does not change capture or configure
the classifier. A health capability flag is not proof of model quality/readiness.
There is no provider, consent, authority, model or voice-runtime change here.

## Verification

Use actual decoded default pack frames for idle and reduced motion; check both
idle aliases and preserve non-idle atlas hashes/mappings. Inspect desktop/narrow
seated placement. Retain audio-bridge/window-capture/native speech-window tests
that distinguish no-silence continuous input, WAIT/IGNORE, mid-speech interruption
and the selected noninterrupting opening hold. UI tests cover window/fallback
wording and an explicit fallback setting callback. This is deterministic behavior
coverage; physical audio, conversation feel and mobile calling remain separate.
