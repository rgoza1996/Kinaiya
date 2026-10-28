import 'package:flutter/material.dart';

import 'audio/speech_audio.dart';
import 'state/app_state.dart';
import 'ui/agent_chat.dart';
import 'ui/character_view.dart';
import 'ui/setup_dialog.dart';
import 'ui/visual_theme.dart';

export 'ui/visual_theme.dart' show kinaiyaIndigo, kinaiyaWhite;

final _lightTheme = kinaiyaTheme();
final _darkTheme = kinaiyaTheme(brightness: Brightness.dark);

class KinaiyaApp extends StatelessWidget {
  const KinaiyaApp({super.key, required this.state, this.audio});
  final AppState state;
  final SpeechAudio? audio;

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: state,
    builder: (context, _) => MaterialApp(
      title: 'Kinaiya',
      theme: _lightTheme,
      darkTheme: _darkTheme,
      themeMode: state.view == AppView.agent ? ThemeMode.dark : ThemeMode.light,
      home: HomeShell(key: ObjectKey(state), state: state, audio: audio),
    ),
  );
}

/// Keep voice/task panes mounted across navigation. Hide only their decorative
/// tickers, focus and semantics; changing views is not a runtime stop request.
class HomeShell extends StatefulWidget {
  const HomeShell({super.key, required this.state, this.audio});
  final AppState state;
  final SpeechAudio? audio;
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
                        color: isAgent ? kinaiyaNavy : kinaiyaIndigo,
                        child: Padding(
                          padding: const EdgeInsets.fromLTRB(20, 8, 12, 8),
                          child: Row(
                            children: [
                              Expanded(
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    const Text(
                                      'Kinaiya',
                                      style: TextStyle(
                                        color: kinaiyaWhite,
                                        fontSize: 22,
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                                    Text(
                                      isAgent ? 'AGENT WORKSPACE' : 'CONVERSE',
                                      style: const TextStyle(
                                        color: Color(0xFFDAD8FF),
                                        fontSize: 12,
                                        letterSpacing: 1.5,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                              ConnectionDot(state: state),
                              IconButton(
                                tooltip: 'Connection setup',
                                color: kinaiyaWhite,
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
                                    data: _lightTheme,
                                    child: CharacterView(
                                      state: state,
                                      audio: widget.audio,
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
                                    data: _darkTheme,
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
