import 'dart:async';

import 'package:flutter/material.dart';

import 'audio/speech_audio.dart';
import 'config/options_catalog.dart';
import 'state/app_state.dart';
import 'presentation/controller.dart';
import 'ui/presentation_settings.dart';
import 'ui/agent_chat.dart';
import 'ui/character_view.dart';
import 'ui/setup_dialog.dart';
import 'ui/settings_controls.dart';
import 'ui/settings_hub.dart';
import 'ui/converse_memory.dart';
import 'ui/mcp_settings.dart';
import 'ui/visual_theme.dart';
import 'ui/design_tokens.dart';

export 'ui/visual_theme.dart' show kinaiyaIndigo, kinaiyaWhite;

Color? _presentationColor(String? hex) => hex == null
    ? null
    : Color(0xFF000000 | int.parse(hex.substring(1), radix: 16));

class KinaiyaApp extends StatefulWidget {
  const KinaiyaApp({
    super.key,
    required this.state,
    this.audio,
    this.presentation,
  });
  final AppState state;
  final SpeechAudio? audio;
  final PresentationController? presentation;

  @override
  State<KinaiyaApp> createState() => _KinaiyaAppState();
}

class _KinaiyaAppState extends State<KinaiyaApp> {
  late PresentationController _presentation;
  late bool _ownsPresentation;
  Color? _themePrimary, _themeBackground;
  ThemeData? _lightTheme, _darkTheme;

  void _adoptPresentation() {
    _ownsPresentation = widget.presentation == null;
    _presentation = widget.presentation ?? PresentationController();
    unawaited(_presentation.initialize());
  }

  @override
  void initState() {
    super.initState();
    _adoptPresentation();
  }

  @override
  void didUpdateWidget(covariant KinaiyaApp oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.presentation, widget.presentation)) {
      if (_ownsPresentation) _presentation.dispose();
      _adoptPresentation();
    }
  }

  @override
  void dispose() {
    if (_ownsPresentation) _presentation.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([widget.state, _presentation]),
    builder: (context, _) {
      final preferences = _presentation.preferences;
      final pack = preferences.usePackPalette
          ? _presentation.selected?.pack
          : null;
      final primary = _presentationColor(
        preferences.primaryOverride ??
            (pack?.themePrimary.isNotEmpty == true ? pack!.themePrimary : null),
      );
      final background = _presentationColor(
        preferences.backgroundOverride ??
            (pack?.themeBackground.isNotEmpty == true
                ? pack!.themeBackground
                : null),
      );
      if (_lightTheme == null ||
          primary != _themePrimary ||
          background != _themeBackground) {
        _themePrimary = primary;
        _themeBackground = background;
        _lightTheme = kinaiyaTheme(
          primary: primary,
          background: background,
          designSystem: true,
        );
        _darkTheme = kinaiyaTheme(
          designSystem: true,
          brightness: Brightness.dark,
          primary: primary,
          background: background,
        );
      }
      final light = _lightTheme!;
      final dark = _darkTheme!;
      return MaterialApp(
        title: 'Kinaiya',
        theme: light,
        darkTheme: dark,
        themeAnimationDuration: Duration.zero,
        themeMode: widget.state.view == AppView.agent
            ? ThemeMode.dark
            : ThemeMode.light,
        builder: (context, child) {
          final media = MediaQuery.of(context);
          return MediaQuery(
            data: media.copyWith(
              disableAnimations:
                  media.disableAnimations || preferences.reducedMotion,
            ),
            child: child!,
          );
        },
        home: HomeShell(
          key: ObjectKey(widget.state),
          state: widget.state,
          audio: widget.audio,
          presentation: _presentation,
          lightTheme: light,
          darkTheme: dark,
          accent: primary ?? KinaiyaDesign.blue,
          stageBackground: background ?? kinaiyaWhite,
        ),
      );
    },
  );
}

/// Keep voice/task panes mounted across navigation. Hide only their decorative
/// tickers, focus and semantics; changing views is not a runtime stop request.
class HomeShell extends StatefulWidget {
  const HomeShell({
    super.key,
    required this.state,
    this.audio,
    required this.presentation,
    required this.lightTheme,
    required this.darkTheme,
    required this.accent,
    required this.stageBackground,
  });
  final AppState state;
  final SpeechAudio? audio;
  final PresentationController presentation;
  final ThemeData lightTheme, darkTheme;
  final Color accent, stageBackground;
  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  final _voice = ValueNotifier(const VoiceActivity());
  final _converseControls = ConverseViewControls();
  final _agentControls = AgentViewControls();
  bool _settingsOpen = false;

  SettingsAvailability get _settingsAvailability => SettingsAvailability(
    voiceOptions: _converseControls.openVoiceOptions != null,
    agentControls:
        widget.state.commandsAvailable && _agentControls.openCommands != null,
    mcpConnections: widget.state.client != null,
  );

  Future<void> _settings() async {
    if (_settingsOpen || _closing) return;
    final owner = widget.state;
    _settingsOpen = true;
    SettingsAction? action;
    try {
      action = await showDialog<SettingsAction>(
        context: context,
        builder: (_) => ListenableBuilder(
          listenable: owner,
          builder: (_, _) => Theme(
            data: widget.darkTheme,
            child: SettingsHub(availability: _settingsAvailability),
          ),
        ),
      );
    } finally {
      _settingsOpen = false;
    }
    if (!mounted ||
        _closing ||
        !identical(widget.state, owner) ||
        action == null) {
      return;
    }
    // The hub is dismissed before any real destination is opened. Availability
    // is read now, never taken from the snapshot that rendered a selected row.
    if (!_settingsAvailability.allows(action)) return;
    switch (action) {
      case SettingsAction.connection:
        _setup();
      case SettingsAction.appearance:
        _appearance();
      case SettingsAction.memory:
        await showConverseMemoryDialog(context: context, state: owner);
      case SettingsAction.voice:
        await _converseControls.openVoiceOptions?.call();
      case SettingsAction.agentControls:
      case SettingsAction.mcp:
        owner.setView(AppView.agent);
        await WidgetsBinding.instance.endOfFrame;
        if (!mounted ||
            _closing ||
            !identical(widget.state, owner) ||
            !_settingsAvailability.allows(action)) {
          return;
        }
        if (action == SettingsAction.agentControls) {
          _agentControls.openCommands?.call();
        } else {
          await showDialog<void>(
            context: context,
            builder: (_) => McpSettings(state: owner),
          );
        }
    }
  }

  bool _closing = false;
  @override
  void dispose() {
    _closing = true;
    _voice.dispose();
    super.dispose();
  }

  void _setup() => showDialog<void>(
    context: context,
    builder: (_) => SetupDialog(state: widget.state),
  );

  void _appearance() => showDialog<void>(
    context: context,
    builder: (_) => PresentationSettings(controller: widget.presentation),
  );

  @override
  Widget build(BuildContext context) {
    final state = widget.state;
    final isAgent = state.view == AppView.agent;
    return LayoutBuilder(
      builder: (context, box) {
        final scale = MediaQuery.textScalerOf(context).scale(1);
        final rail = box.maxWidth / scale.clamp(1, 2) >= 900;
        final keyboardOpen = MediaQuery.viewInsetsOf(context).bottom > 0;
        final compressedHeader = keyboardOpen || box.maxHeight < 360;
        return Scaffold(
          body: SafeArea(
            bottom: false,
            child: Row(
              children: [
                if (rail)
                  NavigationRail(
                    backgroundColor:
                        widget.darkTheme.colorScheme.surfaceContainerLow,
                    indicatorColor: widget.darkTheme.colorScheme.primary,
                    selectedIconTheme: IconThemeData(
                      color: widget.darkTheme.colorScheme.onPrimary,
                    ),
                    unselectedIconTheme: IconThemeData(
                      color: widget.darkTheme.colorScheme.onSurfaceVariant,
                    ),
                    minWidth: 76,
                    groupAlignment: -.65,
                    leading: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 20),
                      child: ExcludeSemantics(
                        child: Image.asset(
                          'assets/mascot/kinaiya-mascot.png',
                          width: 48,
                          height: 48,
                        ),
                      ),
                    ),
                    selectedIndex: isAgent ? 1 : 0,
                    onDestinationSelected: (i) => state.setView(
                      i == 0 ? AppView.character : AppView.agent,
                    ),
                    destinations: const [
                      NavigationRailDestination(
                        icon: Icon(Icons.graphic_eq_rounded),
                        label: Text('Converse'),
                      ),
                      NavigationRailDestination(
                        icon: Icon(Icons.chat_bubble_outline_rounded),
                        label: Text('Agent Chat'),
                      ),
                    ],
                  ),
                Expanded(
                  child: Column(
                    children: [
                      Theme(
                        data: widget.darkTheme,
                        child: Material(
                          color: widget.darkTheme.colorScheme.surface,
                          child: Padding(
                            padding: const EdgeInsets.fromLTRB(20, 8, 12, 8),
                            child: LayoutBuilder(
                              builder: (context, header) {
                                final compact = header.maxWidth / scale >= 420;
                                final title = Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    Text(
                                      isAgent
                                          ? 'Kinaiya Agent'
                                          : 'Kinaiya Converse',
                                      style: Theme.of(context)
                                          .textTheme
                                          .titleLarge
                                          ?.copyWith(
                                            fontWeight: FontWeight.w700,
                                          ),
                                    ),
                                    Text(
                                      isAgent
                                          ? 'Your workspace'
                                          : 'Your voice, your companion',
                                      style: Theme.of(context)
                                          .textTheme
                                          .bodySmall
                                          ?.copyWith(
                                            color: Theme.of(
                                              context,
                                            ).colorScheme.onSurfaceVariant,
                                          ),
                                    ),
                                  ],
                                );
                                final actions = Row(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    ConnectionDot(state: state),
                                    IconButton(
                                      key: const ValueKey('settings-open'),
                                      tooltip: 'Settings',
                                      icon: const Icon(Icons.settings_outlined),
                                      onPressed: _settings,
                                    ),
                                    IconButton(
                                      tooltip: 'Appearance',
                                      icon: const Icon(Icons.palette_outlined),
                                      onPressed: _appearance,
                                    ),
                                    IconButton(
                                      tooltip: 'Connection setup',
                                      icon: const Icon(Icons.tune_rounded),
                                      onPressed: _setup,
                                    ),
                                  ],
                                );
                                if (compressedHeader) {
                                  return Row(
                                    children: [
                                      Expanded(
                                        child: Text(
                                          isAgent ? 'Agent' : 'Converse',
                                          maxLines: 1,
                                          overflow: TextOverflow.ellipsis,
                                          style: Theme.of(
                                            context,
                                          ).textTheme.titleMedium,
                                        ),
                                      ),
                                      actions,
                                    ],
                                  );
                                }
                                return compact
                                    ? Row(
                                        children: [
                                          Expanded(child: title),
                                          actions,
                                        ],
                                      )
                                    : Column(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                          title,
                                          Align(
                                            alignment: Alignment.centerRight,
                                            child: actions,
                                          ),
                                        ],
                                      );
                              },
                            ),
                          ),
                        ),
                      ),
                      if (!state.connected && state.connectionError != null)
                        MaterialBanner(
                          content: Text(
                            state.connectionError!,
                            maxLines: 3,
                            overflow: TextOverflow.ellipsis,
                          ),
                          actions: [
                            TextButton(
                              onPressed: _setup,
                              child: const Text('SETUP'),
                            ),
                          ],
                        ),
                      ValueListenableBuilder<VoiceActivity>(
                        valueListenable: _voice,
                        builder: (context, voice, _) => voice.active
                            ? _voiceBar(voice)
                            : const SizedBox.shrink(),
                      ),
                      Expanded(
                        child: Stack(
                          fit: StackFit.expand,
                          children: [
                            Offstage(
                              offstage: isAgent,
                              child: ExcludeFocus(
                                excluding: isAgent,
                                child: TickerMode(
                                  enabled: !isAgent,
                                  child: Theme(
                                    data: widget.lightTheme,
                                    child: CharacterView(
                                      useDesignScene:
                                          widget.presentation.selected ==
                                              null &&
                                          widget
                                                  .presentation
                                                  .preferences
                                                  .primaryOverride ==
                                              null &&
                                          widget
                                                  .presentation
                                                  .preferences
                                                  .backgroundOverride ==
                                              null,
                                      state: state,
                                      audio: widget.audio,
                                      controls: _converseControls,
                                      character: widget.presentation.selected,
                                      showCharacter: widget
                                          .presentation
                                          .preferences
                                          .showCharacter,
                                      showCaptions: widget
                                          .presentation
                                          .preferences
                                          .showCaptions,
                                      scenePrimary: widget.accent,
                                      sceneBackground: widget.stageBackground,
                                      onActivityChanged: (activity) {
                                        if (!_closing) _voice.value = activity;
                                      },
                                    ),
                                  ),
                                ),
                              ),
                            ),
                            Offstage(
                              offstage: !isAgent,
                              child: ExcludeFocus(
                                excluding: !isAgent,
                                child: TickerMode(
                                  enabled: isAgent,
                                  child: Theme(
                                    data: widget.darkTheme,
                                    child: AgentChat(
                                      state: state,
                                      controls: _agentControls,
                                    ),
                                  ),
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          bottomNavigationBar: rail || keyboardOpen
              ? null
              : NavigationBar(
                  height: 80 + 32 * (scale - 1).clamp(0, 3),
                  selectedIndex: isAgent ? 1 : 0,
                  onDestinationSelected: (i) =>
                      state.setView(i == 0 ? AppView.character : AppView.agent),
                  destinations: const [
                    NavigationDestination(
                      icon: Icon(Icons.graphic_eq_rounded),
                      label: 'Converse',
                    ),
                    NavigationDestination(
                      icon: Icon(Icons.chat_bubble_outline_rounded),
                      label: 'Agent Chat',
                    ),
                  ],
                ),
        );
      },
    );
  }

  Widget _voiceBar(VoiceActivity voice) => Material(
    color: Theme.of(context).colorScheme.surfaceContainerHigh,
    child: Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 2),
      child: Wrap(
        spacing: 12,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          Text(
            voice.stopPending
                ? 'Stopping microphone…'
                : voice.stopUnconfirmed
                ? 'Microphone stop unconfirmed'
                : voice.recording
                ? 'Recording a voice message'
                : voice.listening
                ? 'Live microphone on'
                : 'Converse is speaking',
          ),
          if (voice.listening || voice.recording || voice.stopUnconfirmed)
            TextButton.icon(
              onPressed: voice.stopPending ? null : voice.stopMicrophone,
              icon: const Icon(Icons.mic_off_rounded),
              label: const Text('Stop microphone'),
            ),
          if (voice.speaking)
            TextButton.icon(
              onPressed: voice.stopSpeech,
              icon: const Icon(Icons.volume_off_rounded),
              label: const Text('Stop speech'),
            ),
        ],
      ),
    ),
  );
}

class ConnectionDot extends StatelessWidget {
  const ConnectionDot({super.key, required this.state});
  final AppState state;
  @override
  Widget build(BuildContext context) {
    final label = state.connected ? 'Connection checked' : 'Not connected';
    final detail =
        state.connectionError ??
        (state.connected
            ? 'Services checked.'
            : 'Open Connection setup to enter service URLs and the owner token.');
    return Tooltip(
      message: detail,
      child: Semantics(
        label: 'Connection status: $label. $detail',
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Icon(
            Icons.circle,
            size: 12,
            color: state.connected
                ? const Color(0xFF63E6B2)
                : const Color(0xFFDAD8FF),
            semanticLabel: label,
          ),
        ),
      ),
    );
  }
}
