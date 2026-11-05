/// Design-independent navigation for settings that already have an owner.
///
/// This catalog describes destinations, not a configuration store. Opening or
/// searching it changes no runtime setting, credential, permission or session.
library;

enum SettingsAction {
  models,
  connection,
  appearance,
  voice,
  memory,
  agentControls,
  mcp,
}

enum SettingsSection {
  connection('Connection & privacy'),
  appearance('Appearance & accessibility'),
  converse('Converse'),
  agent('Agent & integrations');

  const SettingsSection(this.label);
  final String label;
}

class SettingsEntry {
  const SettingsEntry({
    required this.action,
    required this.section,
    required this.title,
    required this.description,
    required this.keywords,
  });
  final SettingsAction action;
  final SettingsSection section;
  final String title, description, keywords;

  bool matches(String query) {
    final words = query.toLowerCase().trim().split(RegExp(r'\s+'));
    final haystack = '${section.label} $title $description $keywords'
        .toLowerCase();
    return words.every(haystack.contains);
  }
}

const settingsEntries = <SettingsEntry>[
  SettingsEntry(
    action: SettingsAction.models,
    section: SettingsSection.converse,
    title: 'Models & conversation',
    description: 'Inspect host models and choose the optional Converse view.',
    keywords: 'conversation stt tts decisions reasoning modalities setup llm model provider',
  ),
  SettingsEntry(
    action: SettingsAction.connection,
    section: SettingsSection.connection,
    title: 'Service connection & privacy',
    description: 'Connect to your services and choose where input may be sent.',
    keywords: 'host url owner token credentials local cloud disclosure api key endpoint connect setup privacy',
  ),
  SettingsEntry(
    action: SettingsAction.appearance,
    section: SettingsSection.appearance,
    title: 'Appearance & character',
    description: 'Colors, character packs, captions, interface transitions and reduced motion.',
    keywords: 'mascot sprites animation accessibility palette import export reset character avatar theme font captions',
  ),
  SettingsEntry(
    action: SettingsAction.voice,
    section: SettingsSection.converse,
    title: 'Voice options',
    description: 'Conversation timing for the current view: live Decisions timing or fallback clip silence.',
    keywords: 'speech microphone vad live call tell audio window decisions clip fallback timing silence mic speaker tts stt',
  ),
  SettingsEntry(
    action: SettingsAction.memory,
    section: SettingsSection.converse,
    title: 'Saved Converse notes',
    description: 'Inspect existing notes and choose whether to use them.',
    keywords:
        'memory dag recall retained storage selected access notes privacy',
  ),
  SettingsEntry(
    action: SettingsAction.agentControls,
    section: SettingsSection.agent,
    title: 'Agent controls',
    description:
        'Open Agent controls for models, permissions, skills and tools.',
    keywords: 'provider profile yolo approvals sandbox workspace resources plugins commands permissions harnesses',
  ),
  SettingsEntry(
    action: SettingsAction.mcp,
    section: SettingsSection.agent,
    title: 'MCP connections',
    description:
        'Manage Agent tool servers and inspect their connection state.',
    keywords: 'integrations extensions tools authentication enable disable mcp servers',
  ),
];

class SettingsAvailability {
  const SettingsAvailability({
    this.voiceOptions = false,
    this.agentControls = false,
    this.mcpConnections = false,
  });
  final bool voiceOptions, agentControls, mcpConnections;

  bool allows(SettingsAction action) => switch (action) {
    SettingsAction.connection ||
    SettingsAction.models ||
    SettingsAction.appearance ||
    SettingsAction.memory => true,
    SettingsAction.voice => voiceOptions,
    SettingsAction.agentControls => agentControls,
    SettingsAction.mcp => mcpConnections,
  };

  String? unavailableReason(SettingsAction action) {
    if (allows(action)) return null;
    return switch (action) {
      SettingsAction.voice => 'Voice options are unavailable in this view.',
      SettingsAction.agentControls =>
        'Connect and load the Agent controls first.',
      SettingsAction.mcp => 'Connect to the owner service first.',
      _ => null,
    };
  }
}

// Release builds pass the same identity defines used by BuildIdentity; an
// explicit KINAIYA_BUILD_LABEL/REVISION still takes priority for local builds.
// This is display text only; update checks use the validated BuildIdentity.
const _releaseChannel = String.fromEnvironment('KINAIYA_CHANNEL');
const _releaseVersion = String.fromEnvironment('KINAIYA_VERSION');
const _releaseBuild = String.fromEnvironment('KINAIYA_BUILD_NUMBER');
const _releaseLabel =
    (_releaseChannel == 'nightly' || _releaseChannel == 'stable') &&
        _releaseVersion != '' &&
        _releaseBuild != ''
    ? 'Kinaiya $_releaseVersion ($_releaseChannel build $_releaseBuild)'
    : 'Development build';

class SettingsBuildInfo {
  const SettingsBuildInfo({required this.label, this.revision = ''});
  const SettingsBuildInfo.fromEnvironment()
    : label = const String.fromEnvironment(
        'KINAIYA_BUILD_LABEL',
        defaultValue: _releaseLabel,
      ),
      revision = const String.fromEnvironment(
        'KINAIYA_BUILD_REVISION',
        defaultValue: String.fromEnvironment('KINAIYA_SOURCE_SHA'),
      );

  final String label, revision;
}
