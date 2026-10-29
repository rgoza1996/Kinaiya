# Converse stage and voice options

Status: implements the focused presentation pass in #107; M5 stays open.

The moonlit Call/Tell concept remains the Character View reference. Receiving a
message previously collapsed the character to a 72px avatar below the wide-layout
breakpoint. Retain the real sprite stage beside/before the transcript instead,
with a bounded current-message caption and an explicit full-history dialog.
Captions-off and non-character preferences retain history access. Existing
semantic animation, device reduced motion and pack validation remain unchanged.

The stage and transcript share available height. Their scrollable content and
the existing outer scroll surface keep controls reachable on short screens,
with a keyboard or enlarged text. A caption may truncate; the history dialog
contains complete retained messages and delivery notes. System notices are
labelled status, rather than attributed to Kinaiya's dialogue.

Move the existing silence/timing controls behind Voice options. Capture, VAD
values, draft, session, provider disclosure and playback ownership stay in
CharacterView. The modal owns only its displayed selection; changing it forwards
the existing handler. Opening or dismissing it never starts/stops capture. The
live status stays on the current owner surface instead of becoming a stale copy
inside the settings sheet.

Use the theme's surface/onSurface pair for the composer so imported/custom dark
palettes retain readable text. Gate idle capture controls on microphone
capability separately from playback. A local stop remains available when
backend readiness changes during capture. Text/import/playback and direct Agent
operation retain their existing authority and lifetime rules.

`converse_stage_test.dart` checks actual bundled sprite geometry, complete history
access, narrow/large-text/short-height presentation, dark palette surfaces and
capture/options continuity. Voice/window regression tests open options only when
testing relocated settings. Widget and synthetic browser checks do not establish
microphone quality, audible latency or conversation feel; those require human
device testing. No native phone signaling or new voice model is introduced here.
