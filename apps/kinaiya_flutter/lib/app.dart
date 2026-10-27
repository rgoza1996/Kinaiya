import 'package:flutter/material.dart';

import 'state/app_state.dart';
import 'ui/agent_chat.dart';
import 'ui/character_view.dart';
import 'ui/setup_dialog.dart';

const Color kinaiyaIndigo = Color(0xFF141091);
const Color kinaiyaWhite = Color(0xFFFFFFFF);

class KinaiyaApp extends StatelessWidget {
  const KinaiyaApp({super.key, required this.state});
  final AppState state;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: state,
      builder: (context, _) => MaterialApp(
        title: 'Kinaiya',
        theme: ThemeData(
          colorScheme: ColorScheme.fromSeed(
            seedColor: kinaiyaIndigo,
          ).copyWith(primary: kinaiyaIndigo, surface: kinaiyaWhite),
          useMaterial3: true,
        ),
        home: HomeShell(state: state),
      ),
    );
  }
}

class HomeShell extends StatelessWidget {
  const HomeShell({super.key, required this.state});
  final AppState state;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Kinaiya'),
        actions: [
          ConnectionDot(state: state),
          IconButton(
            tooltip: 'Connection setup',
            icon: const Icon(Icons.settings),
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) => SetupDialog(state: state),
            ),
          ),
        ],
      ),
      body: state.view == AppView.character
          ? CharacterView(state: state)
          : AgentChat(state: state),
      bottomNavigationBar: NavigationBar(
        selectedIndex: state.view == AppView.character ? 0 : 1,
        onDestinationSelected: (i) =>
            state.setView(i == 0 ? AppView.character : AppView.agent),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.face), label: 'Converse'),
          NavigationDestination(
            icon: Icon(Icons.construction),
            label: 'Agent Chat',
          ),
        ],
      ),
    );
  }
}

class ConnectionDot extends StatelessWidget {
  const ConnectionDot({super.key, required this.state});
  final AppState state;

  @override
  Widget build(BuildContext context) {
    final label = state.connected ? 'Connected' : 'Not connected';
    final detail =
        state.connectionError ??
        (state.connected
            ? 'Session, Agent and Converse services reachable as configured.'
            : 'Open Connection setup to enter service URLs and the owner token.');
    return Tooltip(
      message: detail,
      child: Semantics(
        label: 'Connection status: $label. $detail',
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8),
          child: Icon(
            Icons.circle,
            size: 14,
            color: state.connected ? Colors.green : Colors.grey,
            semanticLabel: label,
          ),
        ),
      ),
    );
  }
}
