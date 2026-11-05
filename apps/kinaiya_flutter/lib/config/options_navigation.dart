import 'package:flutter/material.dart';

enum OptionsPage {
  overview(
    'Options',
    description: 'Browse settings categories and workspace configuration.',
    keywords: 'overview home settings options',
  ),
  models(
    'Models',
    description: 'Select Converse, Agent, Decisions, STT, and TTS model roles.',
    keywords: 'llm model role defaults reasoning converse agent decisions stt tts speech recognition synthesis',
  ),
  providers(
    'Providers',
    description:
        'Configure model providers, service endpoints, and API credentials.',
    keywords: 'api key token secret credential endpoint ollama openai anthropic gemini cloud local provider catalog',
  ),
  harnesses(
    'Harness Customization',
    description: 'Inspect registered Agent harnesses and eligibility.',
    keywords: 'harness version adaptive baseline seed custom workflow',
  ),
  voice(
    'Voice',
    description: 'Configure speech recognition, synthesis, microphone silence, and live timing.',
    keywords: 'mic microphone speech stt tts call tell live listen silence vad voice turn timing',
  ),
  audio(
    'Audio',
    description: 'Inspect microphone capture and audio playback support.',
    keywords: 'mic microphone speaker input output recording playback sound volume device',
  ),
  agent(
    'Agent',
    description: 'Configure Agent operating mode and default task behavior.',
    keywords: 'operating mode on-demand off autonomous worker assistant chat',
  ),
  permissions(
    'Permissions',
    description: 'Review action approval policies and execution authorization.',
    keywords:
        'approval allow deny authorization security policy prompt confirm',
  ),
  workspace(
    'Workspace',
    description: 'Inspect worker workspace isolation and directory boundaries.',
    keywords: 'directory folder files path root boundary',
  ),
  execution(
    'Execution',
    description: 'Inspect sandboxed command execution and runtime constraints.',
    keywords: 'sandbox bubblewrap seccomp shell command runtime process',
  ),
  tools(
    'Tools',
    description: 'Inspect built-in Agent file, read, search, and shell tools.',
    keywords: 'file write read search shell command builtin',
  ),
  skills(
    'Skills',
    description: 'Inspect available slash commands and reusable Agent skills.',
    keywords: 'slash command registry skill workflow',
  ),
  plugins(
    'Plugins',
    description: 'Inspect extension packages and plugin status.',
    keywords: 'plugin extension package addon',
  ),
  integrations(
    'Integrations',
    description: 'Manage MCP tool servers and external integrations.',
    keywords: 'mcp server external integration connection hub',
  ),
  memory(
    'Memory',
    description: 'Manage retained Converse notes and memory namespace access.',
    keywords: 'notes recall retained storage dag history quarantine',
  ),
  context(
    'Context',
    description: 'Inspect conversation context and session window behavior.',
    keywords: 'context window prompt session token history',
  ),
  data(
    'Data',
    description: 'Inspect local storage and recorded session journals.',
    keywords: 'journal database local storage export persistence',
  ),
  tasks(
    'Tasks',
    description: 'Inspect active and recorded Agent tasks and evidence.',
    keywords: 'task queue running objective evidence receipt stop steer',
  ),
  resources(
    'Resources',
    description: 'Inspect host resource health, GPU/CPU admission, and voice restoration.',
    keywords: 'gpu cpu vram ram memory health admission phase restore',
  ),
  appearance(
    'Appearance',
    description: 'Customize interface theme colors, stage background, and Converse view visibility.',
    keywords: 'theme color palette dark light background stage layout visual converse hide unhide show page view navigation',
  ),
  character(
    'Character',
    description: 'Change character pack, mascot visibility, or import and export character packages.',
    keywords: 'change character mascot avatar sprite pack bundle import export hide show portrait converse page view',
  ),
  connection(
    'Connection',
    description: 'Connect to Session, Agent, and Converse host URLs with your owner token.',
    keywords: 'host url endpoint port owner token api key auth credential connect reconnect',
  ),
  privacy(
    'Privacy',
    description: 'Control local-only versus cloud provider data disclosure.',
    keywords: 'disclosure consent cloud local privacy data sharing scope',
  ),
  accessibility(
    'Accessibility',
    description: 'Configure captions, subtitles, reduced motion, and speech mouth animation.',
    keywords: 'captions subtitles reduced motion animation transitions mouth viseme screen reader a11y text scale',
  ),
  about(
    'About',
    description: 'View build version, channel, and software information.',
    keywords: 'version build channel license info release',
  );

  const OptionsPage(
    this.label, {
    required this.description,
    required this.keywords,
  });
  final String label;
  final String description;
  final String keywords;
}

class OptionsSearchResult {
  const OptionsSearchResult({
    required this.page,
    required this.category,
    required this.score,
    this.isPartial = false,
  });

  final OptionsPage page;
  final OptionsCategory category;
  final int score;
  final bool isPartial;
}

class OptionsCategory {
  const OptionsCategory(this.label, this.icon, this.pages);
  final String label;
  final IconData icon;
  final List<OptionsPage> pages;
}

const optionsCategories = [
  OptionsCategory('Models & providers', Icons.diamond_outlined, [
    OptionsPage.models,
    OptionsPage.providers,
    OptionsPage.harnesses,
  ]),
  OptionsCategory('Voice & audio', Icons.graphic_eq, [
    OptionsPage.voice,
    OptionsPage.audio,
  ]),
  OptionsCategory('Agent & permissions', Icons.smart_toy_outlined, [
    OptionsPage.agent,
    OptionsPage.permissions,
    OptionsPage.workspace,
    OptionsPage.execution,
  ]),
  OptionsCategory('Tools & integrations', Icons.extension_outlined, [
    OptionsPage.tools,
    OptionsPage.skills,
    OptionsPage.plugins,
    OptionsPage.integrations,
  ]),
  OptionsCategory('Memory & data', Icons.storage_outlined, [
    OptionsPage.context,
    OptionsPage.memory,
    OptionsPage.data,
  ]),
  OptionsCategory('Tasks & resources', Icons.speed_outlined, [
    OptionsPage.tasks,
    OptionsPage.resources,
  ]),
  OptionsCategory('Appearance & character', Icons.auto_awesome_outlined, [
    OptionsPage.appearance,
    OptionsPage.character,
  ]),
  OptionsCategory('Connection & privacy', Icons.link_outlined, [
    OptionsPage.connection,
    OptionsPage.privacy,
  ]),
  OptionsCategory('Accessibility', Icons.accessibility_new, [
    OptionsPage.accessibility,
  ]),
  OptionsCategory('About', Icons.info_outline, [OptionsPage.about]),
];

List<OptionsSearchResult> searchOptionsPages(String query) {
  final normalized = query.trim().toLowerCase();
  if (normalized.isEmpty) return const <OptionsSearchResult>[];
  final words = normalized
      .split(RegExp(r'\s+'))
      .where((word) => word.isNotEmpty)
      .toList();
  if (words.isEmpty) return const <OptionsSearchResult>[];

  final exact = <OptionsSearchResult>[];
  final partial = <OptionsSearchResult>[];
  final seen = <OptionsPage>{};

  for (final category in optionsCategories) {
    final categoryLower = category.label.toLowerCase();
    for (final page in category.pages) {
      if (!seen.add(page)) continue;
      final titleLower = page.label.toLowerCase();
      final descLower = page.description.toLowerCase();
      final keywordsLower = page.keywords.toLowerCase();
      final keywordTokens = keywordsLower.split(RegExp(r'\s+')).toSet();
      final haystack = '$titleLower $categoryLower $descLower $keywordsLower';

      var matchedWords = 0;
      var score = 0;
      if (titleLower == normalized) {
        score += 120;
      } else if (titleLower.startsWith(normalized)) {
        score += 90;
      } else if (titleLower.contains(normalized)) {
        score += 70;
      }
      if (categoryLower.contains(normalized)) score += 40;
      if (keywordsLower.contains(normalized)) score += 35;
      if (descLower.contains(normalized)) score += 25;

      for (final word in words) {
        if (!haystack.contains(word)) continue;
        matchedWords++;
        if (titleLower == word) {
          score += 35;
        } else if (titleLower.contains(word)) {
          score += 25;
        }
        if (keywordTokens.contains(word)) {
          score += 20;
        } else if (keywordsLower.contains(word)) {
          score += 12;
        }
        if (descLower.contains(word)) score += 8;
        if (categoryLower.contains(word)) score += 10;
      }

      if (matchedWords == words.length) {
        exact.add(
          OptionsSearchResult(page: page, category: category, score: score),
        );
      } else if (words.length > 1 &&
          matchedWords > 0 &&
          words.any((w) => w.length >= 2 && haystack.contains(w))) {
        partial.add(
          OptionsSearchResult(
            page: page,
            category: category,
            score: matchedWords * 50 + score,
            isPartial: true,
          ),
        );
      }
    }
  }

  int compareResults(OptionsSearchResult a, OptionsSearchResult b) =>
      b.score.compareTo(a.score);

  if (exact.isNotEmpty) {
    exact.sort(compareResults);
    return exact;
  }
  partial.sort(compareResults);
  return partial;
}
