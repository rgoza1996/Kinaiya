/// Design-independent navigation for settings that already have an owner.
///
/// This catalog describes destinations, not a configuration store. Opening or
/// searching it changes no runtime setting, credential, permission or session.
library;

enum SettingsAction {
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
    action: SettingsAction.connection,
    section: SettingsSection.connection,
    title: 'Service connection & privacy',
    description: 'Connect to your services and choose where input may be sent.',
    keywords: 'host url owner token credentials local cloud disclosure',
  ),
  SettingsEntry(
    action: SettingsAction.appearance,
    section: SettingsSection.appearance,
    title: 'Appearance & character',
    description: 'Colors, character packs, captions and reduced motion.',
    keywords:
        'mascot sprites animation accessibility palette import export reset',
  ),
  SettingsEntry(
    action: SettingsAction.voice,
    section: SettingsSection.converse,
    title: 'Voice options',
    description:
        'Adjust the current conversation’s silence and timing options.',
    keywords: 'speech microphone vad live call tell audio',
  ),
  SettingsEntry(
    action: SettingsAction.memory,
    section: SettingsSection.converse,
    title: 'Saved Converse notes',
    description: 'Inspect existing notes and choose whether to use them.',
    keywords: 'memory dag recall retained storage selected access',
  ),
  SettingsEntry(
    action: SettingsAction.agentControls,
    section: SettingsSection.agent,
    title: 'Agent controls',
    description:
        'Open Agent controls for models, permissions, skills and tools.',
    keywords:
        'provider profile yolo approvals sandbox workspace resources plugins commands',
  ),
  SettingsEntry(
    action: SettingsAction.mcp,
    section: SettingsSection.agent,
    title: 'MCP connections',
    description:
        'Manage Agent tool servers and inspect their connection state.',
    keywords: 'integrations extensions tools authentication enable disable',
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

class SettingsBuildInfo {
  const SettingsBuildInfo({required this.label, this.revision = ''});
  const SettingsBuildInfo.fromEnvironment()
    : label = const String.fromEnvironment(
        'KINAIYA_BUILD_LABEL',
        defaultValue: 'Development build',
      ),
      revision = const String.fromEnvironment('KINAIYA_BUILD_REVISION');

  final String label, revision;
}
