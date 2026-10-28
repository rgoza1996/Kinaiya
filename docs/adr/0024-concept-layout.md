# ADR 0024: Concept-led Converse and Agent workspace

Status: experimental implementation for [#8](https://github.com/rgoza1996/Kinaiya/issues/8), Roadmap 9, 10, 17 and 18. M5's broader settings, expression library, physical voice and device acceptance remain open.

## Presentation and authority

The owner-supplied [revised Converse concept](../../assets/UIUX%20Concept/kinaiya-converse-moonlit-concept.png) supplies the oversized moon, orbits, reflected water, left navigation and distinct Call/Tell actions. The [Agent concept](../../assets/UIUX%20Concept/kinaiya-agent-workspace-concept.png) supplies the navy workspace, task list and details panel. Their illustrative times, weather, harnesses, timelines and project status are not runtime facts.

`MoonlitScene` paints bounded deterministic decoration behind the existing versioned mascot and interactive controls. It contains no credentials, network, microphone, timers or model state. The existing sprite renderer still supplies idle/listening/thinking/speaking frames and reduced-motion behavior. The palette retains the specified indigo and white; separate light Converse and dark Agent themes provide readable controls. Pane-local themes prevent a hidden pane from inheriting the other view's ink/background combination.

The workspace shows real task/session/revision, evidence and approval state. Selecting a task only selects inspection; it does not change the recipient/session for new instructions. Pending approvals retain their existing digest/revision-bound actions. Registry and MCP controls exist only in Agent Chat. Plain Converse input still has no command parser.

## Navigation and local voice

Both panes stay mounted under `Offstage`, `ExcludeFocus` and `TickerMode`. This preserves drafts, service sessions and user-armed capture while excluding hidden controls from navigation/accessibility and pausing hidden visual tickers. Backend resource/connection invalidation still stops local activity. An `ObjectKey` recreates the shell when its `AppState` instance changes.

Character View publishes coalesced post-frame local activity to the shell. A global strip exposes microphone and speech stops from either view. Call arms the existing continuous browser listener; Tell records one message. No phone signaling or native/mobile calling is added.

The microphone stop invalidates capture/voice ownership before awaiting the adapter, clears queued clips/windows and cancels an unfinished recording without sending it. Window listening also invalidates its queued speech. It never invokes Agent task interruption. The strip shows pending acknowledgment and disables duplicate presses. A three-second timeout/error remains unconfirmed and retryable; an older completion cannot clear a newer stop's state. A local adapter acknowledgment is not proof of physical microphone or speaker behavior.

## Responsive layout and verification

The desktop rail becomes bottom navigation based on width adjusted for text scaling. Wide Agent layouts add search and an inspection panel; narrow layouts retain task cards and approvals. Scrollable natural-height composers preserve focus/drafts through keyboard insets and avoid fixed-height overflow at enlarged text sizes.

`concept_layout_test.dart` uses synthetic transport/audio only. It checks retained capture and separate drafts/sessions, global stop without transcription or Agent cancellation, failed and delayed stop recovery, stale stop completion, inspection without routing changes, phone/desktop layouts at normal and 2× text, keyboard focus/drafts and 4.5:1 control-text contrast in both schemes. Existing resource tests retain their disabled/ownership assertions using stable labeled controls after the layout change.

Run the pinned Flutter analyzer, full Flutter suite, browser audio bridge tests and release web build. Browser screenshots supplement these checks; they do not establish physical microphone quality, echo cancellation, audible latency, complete pack expressions, native voice or M8 calling acceptance.
