import 'dart:async';

import 'package:flutter/material.dart';

import 'audio/speech_audio.dart';
import 'state/app_state.dart';
import 'presentation/controller.dart';
import 'ui/presentation_settings.dart';
import 'ui/agent_chat.dart';
import 'ui/character_view.dart';
import 'ui/setup_dialog.dart';
import 'ui/visual_theme.dart';

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
        _lightTheme = kinaiyaTheme(primary: primary, background: background);
        _darkTheme = kinaiyaTheme(
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
          accent: primary ?? kinaiyaIndigo,
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
        return Scaffold(
          body: SafeArea(
            bottom: false,
            child: Row(
              children: [
                if (rail)
                  NavigationRail(
                    backgroundColor: kinaiyaNavy,
                    indicatorColor: kinaiyaWhite,
                    selectedIconTheme: const IconThemeData(
                      color: kinaiyaIndigo,
                    ),
                    unselectedIconTheme: const IconThemeData(
                      color: kinaiyaWhite,
                    ),
                    minWidth: 80,
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
                      Material(
                        color: isAgent
                            ? widget.darkTheme.colorScheme.surface
                            : widget.accent,
                        child: Padding(
                          padding: const EdgeInsets.fromLTRB(20, 8, 12, 8),
                          child: Row(
                            children: [
                              Expanded(
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      'Kinaiya',
                                      style: TextStyle(
                                        color: _headerInk(isAgent),
                                        fontSize: 22,
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                                    Text(
                                      isAgent ? 'AGENT WORKSPACE' : 'CONVERSE',
                                      style: TextStyle(
                                        color: _headerInk(isAgent),
                                        fontSize: 12,
                                        letterSpacing: 1.5,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                              ConnectionDot(state: state),
                              IconButton(
                                tooltip: 'Appearance',
                                color: _headerInk(isAgent),
                                icon: const Icon(Icons.palette_outlined),
                                onPressed: _appearance,
                              ),
                              IconButton(
                                tooltip: 'Connection setup',
                                color: _headerInk(isAgent),
                                icon: const Icon(Icons.tune_rounded),
                                onPressed: _setup,
                              ),
                            ],
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
                                      state: state,
                                      audio: widget.audio,
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
                                    child: AgentChat(state: state),
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
          bottomNavigationBar: rail
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

  Color _headerInk(bool isAgent) => isAgent
      ? widget.darkTheme.colorScheme.onSurface
      : widget.accent.computeLuminance() > 0.179
      ? Colors.black
      : Colors.white;

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
