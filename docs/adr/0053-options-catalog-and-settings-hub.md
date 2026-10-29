# 0053: A settings catalog independent of the visual design

Issue #126 follows M5. The owner requested preparation of the full options
surface using existing Kinaiya controls and common harness patterns. A later
Penpot design may omit controls; it determines visual composition, not the
existence, semantics or authority of options.

## This implementation

Add one searchable owner Settings hub, reusing existing settings destinations.
It returns a `SettingsAction` to HomeShell; it does not execute commands, hold
credentials, write preferences, create sessions or own audio. Closing it before
opening a selected destination avoids stacked settings modals. HomeShell checks
that its owner is still mounted and reads current availability before dispatch.
Keep both Converse and Agent panes mounted across all settings/navigation.

`config/options_catalog.dart` contains pure Dart value objects. SettingsAction
is connection/appearance/voice/memory/agentControls/mcp. SettingsSection provides
connection, appearance, Converse and Agent categories. Six immutable entries
contain titles/descriptions/search keywords only. Availability is supplied by
the current application; it is not an authorization decision. Connection,
appearance and memory are reachable offline; their existing owners explain
missing connections/features. Voice requires the current mounted view handle,
Agent controls require the current registry, MCP requires an owner client.

`ui/settings_hub.dart` exports `SettingsHub({key, required availability,
buildInfo = const SettingsBuildInfo.fromEnvironment()})`. It is a dialog which
returns a SettingsAction through Navigator.pop. Required stable keys are
`settings-search`, `settings-close` and `settings-entry-${action.name}`. Search
uses all query words against existing entry metadata; clearing search restores
all entries. Empty results, unavailable destinations and build information have
truthful text. The widget has no AppState/API/audio dependency or future switches.
It must fit 320px/short screens/2x text, scroll without losing dismissal, label
controls and support Escape and ordinary keyboard focus. Heading, search and
entries share a scrollable body while Close stays outside it; a keyboard inset
or enlarged text cannot push dismissal behind the keyboard. Build information uses
explicit compile definitions KINAIYA_BUILD_LABEL/REVISION; missing definitions
say Development build and omit the revision rather than inventing a release.

HomeShell adds a Settings icon while preserving existing Appearance/Connection
shortcuts. It opens the current SetupDialog, PresentationSettings or Converse
memory dialog unchanged. Voice options reach the existing CharacterView state;
opening/closing settings cannot configure VAD, replace the audio adapter or
change drafts. Only an explicit silence change reaches the current owner.
Agent controls first navigate to Agent and invoke its existing command chooser;
selection only fills the Agent draft, never auto-executes. MCP similarly opens
from Agent scope. No slash-command parser or tool dispatch enters Converse.

Use small view handles `ConverseViewControls.openVoiceOptions` (Future<void>
callback) and `AgentViewControls.openCommands` (void callback), passed from
HomeShell into each mounted pane. Each state registers its own guarded closure,
detaches only its own closure on replacement/disposal, and refuses invocation
after disposal or registry loss. They are presentation handles, not authority.
No changes to AppState connection, API transports or native permission semantics
are required. The caller rebuilds hub availability from current AppState.

## Future option surfaces

The full options specification lives in docs/options.md. It records stable
names, types/defaults, scope, persistence, native owner, change boundary and
support status. A future Penpot layout can reorganize the catalog without
changing those meanings. Current editors remain independent and retain their
existing validation/lifetime rules. Configuration-only and planned capabilities
belong in the specification, not simulated working toggles in the app.

Model/provider selection, inference disclosure, task permissions, approvals,
filesystem/network isolation and billing limits are distinct concepts. A UI
choice cannot broaden native grants, authorize cloud disclosure implicitly or
claim a hard money/VRAM limit from a timeout/token counter. Personality remains
Converse-only; optional DAG storage/use remains Converse-only and separate from
the authoritative transcript. Never copy another harness's unsafe defaults.

## Verification

Focused hub and navigation tests cover search, disabled states, keyboard/layout,
current-state routing, no open-time writes/inference, stale view handles and
preserved audio/drafts/tasks. Existing settings/voice/Agent tests remain intact.
Independent review and exact-head CI precede merge. Browser inspection verifies
the actual served build and real settings entry; synthetic fixtures used for
fault tests are labeled. No model/GPU/human voice or physical-phone qualification
follows from this presentation/navigation slice.
