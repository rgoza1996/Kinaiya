import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/character/package.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/converse_interaction.dart';
import 'package:kinaiya_flutter/ui/design_tokens.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

import 'converse_stage_test.dart' show StageApi, StageAudio;

class _CharacterProbe extends StatefulWidget {
  const _CharacterProbe({
    required this.constraints,
    required this.onMount,
    required this.onDispose,
  });

  final ValueChanged<BoxConstraints> constraints;
  final VoidCallback onMount, onDispose;

  @override
  State<_CharacterProbe> createState() => _CharacterProbeState();
}

class _CharacterProbeState extends State<_CharacterProbe> {
  @override
  void initState() {
    super.initState();
    widget.onMount();
  }

  @override
  void dispose() {
    widget.onDispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      widget.constraints(constraints);
      return const ColoredBox(color: Color(0xFF253359));
    },
  );
}

Widget _screen({
  Widget character = const ColoredBox(color: Color(0xFF253359)),
  Widget? composer,
  double textScale = 1,
  double hostHeader = 0,
  double hostRailWidth = 0,
  ThemeData? theme,
  bool activeVoice = false,
  ConverseStageState? stageState,
  String status = 'Synthetic owner status',
  String callLabel = 'Talk with Kinaiya',
  String microphoneLabel = 'Mute',
  VoidCallback? onCall,
  VoidCallback? onMicrophone,
  VoidCallback? onModelSettings,
  VoidCallback? onHistory,
  VoidCallback? onVoiceOptions,
  VoidCallback? onMemory,
}) => MaterialApp(
  theme: theme ?? kinaiyaTheme(brightness: Brightness.dark, designSystem: true),
  builder: (context, child) => MediaQuery(
    data: MediaQuery.of(
      context,
    ).copyWith(textScaler: TextScaler.linear(textScale)),
    child: child!,
  ),
  home: Scaffold(
    body: Padding(
      padding: EdgeInsets.only(left: hostRailWidth),
      child: Column(
        children: [
          SizedBox(height: hostHeader),
          Expanded(
            child: ConverseInteraction(
              character: character,
              transcript: ListView.builder(
                key: const ValueKey('synthetic-transcript'),
                itemCount: 80,
                itemBuilder: (_, index) => Padding(
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  child: Text('Synthetic line $index'),
                ),
              ),
              composer:
                  composer ??
                  const TextField(
                    key: ValueKey('synthetic-composer'),
                    decoration: InputDecoration(hintText: 'Synthetic composer'),
                  ),
              modelSummary: const Text('Synthetic supplied model summary'),
              status: status,
              stageState: stageState,
              callLabel: callLabel,
              microphoneLabel: microphoneLabel,
              onCall: onCall,
              onMicrophone: onMicrophone,
              onModelSettings: onModelSettings ?? () {},
              onHistory: onHistory ?? () {},
              onVoiceOptions: onVoiceOptions ?? () {},
              onMemory: onMemory ?? () {},
              activeVoice: activeVoice,
            ),
          ),
        ],
      ),
    ),
  ),
);

void _viewport(WidgetTester tester, Size size) {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = size;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

void main() {
  for (final activeVoice in [false, true]) {
    testWidgets(
      '720px desktop keeps call and composer visible (voice $activeVoice)',
      (tester) async {
        _viewport(tester, const Size(1280, 720));
        BoxConstraints? characterConstraints;
        await tester.pumpWidget(
          _screen(
            hostHeader: 80,
            hostRailWidth: 76,
            activeVoice: activeVoice,
            onCall: () {},
            onMicrophone: () {},
            character: _CharacterProbe(
              constraints: (value) => characterConstraints = value,
              onMount: () {},
              onDispose: () {},
            ),
            composer: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const TextField(
                  key: ValueKey('synthetic-composer'),
                  decoration: InputDecoration(labelText: 'Synthetic message'),
                ),
                const SizedBox(height: 8),
                FilledButton(
                  onPressed: () {},
                  child: const Text('Synthetic send'),
                ),
                const Text('Synthetic composer help'),
              ],
            ),
          ),
        );
        expect(tester.takeException(), isNull);
        expect(characterConstraints!.hasBoundedHeight, isTrue);
        expect(characterConstraints!.hasBoundedWidth, isTrue);
        final canvas = tester.getSize(
          find.byKey(const ValueKey('converse-character-canvas')),
        );
        expect(canvas.height, lessThan(320));
        expect(canvas.width, greaterThan(canvas.height));
        final microphone = tester.getRect(
          find.byKey(const ValueKey('converse-microphone')),
        );
        for (final action in [
          'microphone',
          'camera',
          'call',
          'model-settings',
        ]) {
          final target = find.byKey(ValueKey('converse-$action'));
          final rect = tester.getRect(target);
          expect(rect.height, greaterThanOrEqualTo(48));
          expect(rect.bottom, lessThanOrEqualTo(720));
          expect(rect.top, closeTo(microphone.top, .01));
        }
        expect(
          find.byKey(const ValueKey('converse-call')).hitTestable(),
          findsOneWidget,
        );
        expect(
          tester.getBottomRight(find.text('Synthetic composer help')).dy,
          lessThanOrEqualTo(720),
        );
        expect(
          tester
              .getSize(
                find.byKey(const ValueKey('converse-transcript-viewport')),
              )
              .height,
          inInclusiveRange(100, 180),
        );
        await tester.drag(
          find.byKey(const ValueKey('converse-transcript-viewport')),
          const Offset(0, -180),
        );
        await tester.pumpAndSettle();
        expect(find.text('Synthetic line 0'), findsNothing);
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets(
    'desktop keeps the supplied character bounded in a landscape canvas',
    (tester) async {
      _viewport(tester, const Size(1536, 1024));
      BoxConstraints? characterConstraints;
      var mounts = 0;
      var disposals = 0;
      await tester.pumpWidget(
        _screen(
          character: _CharacterProbe(
            constraints: (value) => characterConstraints = value,
            onMount: () => mounts++,
            onDispose: () => disposals++,
          ),
        ),
      );
      expect(tester.takeException(), isNull);
      expect(characterConstraints!.hasBoundedWidth, isTrue);
      expect(characterConstraints!.hasBoundedHeight, isTrue);
      final canvas = tester.getSize(
        find.byKey(const ValueKey('converse-character-canvas')),
      );
      expect(canvas.width / canvas.height, closeTo(832 / 446, .001));
      final stage = tester.getRect(
        find.byKey(const ValueKey('converse-character-panel')),
      );
      final conversation = tester.getRect(
        find.byKey(const ValueKey('converse-conversation-panel')),
      );
      expect(conversation.left, greaterThan(stage.right));
      expect(
        stage.width / (stage.width + conversation.width),
        closeTo(.646, .01),
      );
      expect(find.text('Synthetic owner status'), findsOneWidget);
      expect(find.text('Synthetic supplied model summary'), findsOneWidget);
      expect(
        find.byKey(const ValueKey('converse-active-waveform')),
        findsNothing,
      );

      tester.view.physicalSize = const Size(320, 568);
      await tester.pump();
      expect(tester.takeException(), isNull);
      expect(mounts, 1);
      expect(disposals, 0);
      expect(characterConstraints!.hasBoundedWidth, isTrue);
      expect(characterConstraints!.hasBoundedHeight, isTrue);
      expect(
        tester
            .getTopLeft(
              find.byKey(const ValueKey('converse-conversation-panel')),
            )
            .dy,
        greaterThan(
          tester
              .getBottomLeft(
                find.byKey(const ValueKey('converse-character-panel')),
              )
              .dy,
        ),
      );
      await tester.pumpWidget(const SizedBox.shrink());
      expect(disposals, 1);
    },
  );

  testWidgets(
    'actions invoke only their supplied callbacks and camera stays disabled',
    (tester) async {
      _viewport(tester, const Size(1536, 1024));
      final calls = <String>[];
      await tester.pumpWidget(
        _screen(
          callLabel: 'End conversation',
          microphoneLabel: 'Resume microphone',
          onCall: () => calls.add('call'),
          onMicrophone: () => calls.add('microphone'),
          onModelSettings: () => calls.add('model'),
          onHistory: () => calls.add('history'),
          onVoiceOptions: () => calls.add('voice'),
          onMemory: () => calls.add('memory'),
        ),
      );
      expect(calls, isEmpty);
      for (final action in [
        'history',
        'voice-options',
        'memory',
        'microphone',
        'call',
        'model-settings',
      ]) {
        final target = find.byKey(ValueKey('converse-$action'));
        await tester.ensureVisible(target);
        await tester.tap(target);
        await tester.pump();
      }
      expect(calls, [
        'history',
        'voice',
        'memory',
        'microphone',
        'call',
        'model',
      ]);
      expect(find.text('End conversation'), findsOneWidget);
      expect(find.text('Resume microphone'), findsOneWidget);
      final camera = tester.widget<OutlinedButton>(
        find.byKey(const ValueKey('converse-camera')),
      );
      expect(camera.onPressed, isNull);
      expect(
        find.byTooltip('Camera input is not supported by this host'),
        findsOneWidget,
      );
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'voice activity decoration depends only on the supplied active flag',
    (tester) async {
      _viewport(tester, const Size(1280, 900));
      await tester.pumpWidget(
        _screen(activeVoice: true, status: 'Actual supplied state'),
      );
      expect(
        find.byKey(const ValueKey('converse-active-waveform')),
        findsOneWidget,
      );
      expect(find.text('Actual supplied state'), findsOneWidget);
      expect(find.textContaining('Listening'), findsNothing);
      await tester.pumpWidget(_screen(status: 'Actual supplied state'));
      expect(
        find.byKey(const ValueKey('converse-active-waveform')),
        findsNothing,
      );
      expect(
        tester
            .widget<FilledButton>(find.byKey(const ValueKey('converse-call')))
            .onPressed,
        isNull,
      );
      expect(
        tester
            .widget<OutlinedButton>(
              find.byKey(const ValueKey('converse-microphone')),
            )
            .onPressed,
        isNull,
      );
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'transcript scrolls independently while its composer stays in place',
    (tester) async {
      _viewport(tester, const Size(1536, 1024));
      await tester.pumpWidget(_screen());
      final composer = find.byKey(const ValueKey('synthetic-composer'));
      final original = tester.getTopLeft(composer);
      await tester.drag(
        find.byKey(const ValueKey('converse-transcript-viewport')),
        const Offset(0, -180),
      );
      await tester.pumpAndSettle();
      expect(tester.getTopLeft(composer), original);
      expect(find.text('Synthetic line 0'), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  for (final viewport in [
    const Size(320, 568),
    const Size(320, 240),
    const Size(480, 320),
  ]) {
    testWidgets('enlarged text scrolls without overflow at $viewport', (
      tester,
    ) async {
      _viewport(tester, viewport);
      await tester.pumpWidget(_screen(textScale: 2, activeVoice: true));
      expect(tester.takeException(), isNull);
      final model = find.byKey(const ValueKey('converse-model-summary'));
      await tester.ensureVisible(model);
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(model.hitTestable(), findsOneWidget);
      await tester.ensureVisible(
        find.byKey(const ValueKey('synthetic-composer')),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.byType(ConverseInteraction), findsOneWidget);
    });
  }

  testWidgets('surfaces follow the active palette instead of hard-coded navy', (
    tester,
  ) async {
    _viewport(tester, const Size(1280, 900));
    final theme = kinaiyaTheme(
      brightness: Brightness.light,
      designSystem: true,
      primary: const Color(0xFF12693C),
      background: const Color(0xFFF4FFF5),
    );
    await tester.pumpWidget(_screen(theme: theme));
    final background = tester.widget<ColoredBox>(
      find
          .descendant(
            of: find.byType(ConverseInteraction),
            matching: find.byType(ColoredBox),
          )
          .first,
    );
    expect(background.color, theme.colorScheme.surface);
    final panel = tester.widget<Container>(
      find
          .descendant(
            of: find.byKey(const ValueKey('converse-character-panel')),
            matching: find.byType(Container),
          )
          .first,
    );
    expect(
      (panel.decoration! as BoxDecoration).color,
      theme.colorScheme.surfaceContainerLow,
    );
    expect(find.byType(DesignPanel), findsNWidgets(3));
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'stage badge distinguishes listening, recording, muted, thinking, speaking, and unavailable states across desktop and 320px',
    (tester) async {
      final semantics = tester.ensureSemantics();
      for (final viewport in [const Size(1280, 900), const Size(320, 568)]) {
        _viewport(tester, viewport);
        for (final state in ConverseStageState.values) {
          await tester.pumpWidget(
            _screen(
              stageState: state,
              status: 'Owner detail for ${state.name}',
              activeVoice:
                  state == ConverseStageState.listening ||
                  state == ConverseStageState.recording ||
                  state == ConverseStageState.speaking,
            ),
          );
          expect(
            find.byKey(const ValueKey('converse-stage-state-badge')),
            findsOneWidget,
          );
          expect(
            tester
                .widget<Text>(
                  find.byKey(const ValueKey('converse-stage-state-label')),
                )
                .data,
            state.label,
          );
          expect(
            find.bySemanticsLabel('Stage state: ${state.label}'),
            findsOneWidget,
          );
          expect(tester.takeException(), isNull);
        }
      }
      semantics.dispose();
    },
  );

  testWidgets(
    'CharacterView interaction stage reflects confirmed owner states without masquerading text or armed mic as playback',
    (tester) async {
      _viewport(tester, const Size(1280, 900));
      final pack = (await tester.runAsync(
        () => loadDefaultCharacterSelection(rootBundle),
      ))!;
      final api = StageApi();
      final audio = StageAudio();
      final state = AppState()
        ..client = api
        ..connected = false
        ..sessionAuthenticated = false
        ..converseEnabled = true
        ..converseOnline = true
        ..converseInferenceAllowed = true
        ..converseSession = SessionInfo(
          sessionId: 's-stage',
          threadId: 't-stage',
          epoch: 1,
          channel: 'converse',
          name: 'Converse',
        );

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ListenableBuilder(
              listenable: state,
              builder: (_, _) => CharacterView(
                state: state,
                audio: audio,
                character: pack,
                surface: CharacterSurface.interaction,
                showCharacter: false,
              ),
            ),
          ),
        ),
      );
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));

      String currentBadge() => tester
          .widget<Text>(
            find.byKey(const ValueKey('converse-stage-state-label')),
          )
          .data!;

      // 1. Offline / unauthenticated shows Unavailable.
      expect(currentBadge(), 'Unavailable');
      expect(find.text('Connect to start a conversation'), findsOneWidget);

      // 2. Authenticated idle with model-generated message stays Microphone muted (never Playing response).
      state
        ..connected = true
        ..sessionAuthenticated = true
        ..appendHistory(
          AppView.character,
          ChatMsg(role: 'kinaiya', text: 'Generated text only'),
        );
      state.touch();
      await tester.pump();
      expect(currentBadge(), 'Microphone muted');
      expect(find.text('Ready to talk'), findsOneWidget);

      // 3. Starting Call arms mic -> Listening (never Playing response).
      await tester.tap(find.byKey(const ValueKey('converse-call')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(currentBadge(), 'Listening');
      expect(find.text('Live microphone on'), findsOneWidget);

      // 4. Busy Converse while armed transitions badge to Thinking.
      state.busyConverse = true;
      state.touch();
      await tester.pump();
      expect(currentBadge(), 'Thinking');
      state.busyConverse = false;
      state.touch();
      await tester.pump();
      expect(currentBadge(), 'Listening');

      // 5. Muting microphone returns to Microphone muted.
      await tester.tap(find.byKey(const ValueKey('converse-microphone')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(currentBadge(), 'Microphone muted');

      // 6. Starting clip recording via Tell shows Recording clip.
      await tester.tap(find.byKey(const ValueKey('converse-tell-inline')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(currentBadge(), 'Recording clip');
      expect(find.text('Recording a voice message'), findsOneWidget);

      // 7. Stale resource status on reconnect transitions to Unavailable.
      state.resourceStatusStale = true;
      state.touch();
      await tester.pump();
      expect(currentBadge(), 'Unavailable');
      expect(find.text('Voice availability unconfirmed'), findsOneWidget);

      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'ConverseModelSummary stays compact by default, discloses local/cloud/missing states truthfully, and expands details without mutating state',
    (tester) async {
      _viewport(tester, const Size(1280, 900));
      var openedModelSettings = 0;
      var openedPrivacy = 0;
      final state = AppState();

      Widget buildSummary({double textScale = 1.0}) => MaterialApp(
        theme: kinaiyaTheme(brightness: Brightness.dark, designSystem: true),
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(
            context,
          ).copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home: Scaffold(
          body: ListenableBuilder(
            listenable: state,
            builder: (_, _) => ConverseInteraction(
              character: const ColoredBox(color: Color(0xFF253359)),
              transcript: ListView(
                children: const [Text('Transcript line one')],
              ),
              composer: const TextField(
                decoration: InputDecoration(hintText: 'Composer'),
              ),
              modelSummary: ConverseModelSummary(
                state: state,
                onModelSettings: () => openedModelSettings++,
                onConnectionSetup: () => openedPrivacy++,
              ),
              status: 'Idle',
              callLabel: 'Talk with Kinaiya',
              microphoneLabel: 'Unmute',
              onModelSettings: () => openedModelSettings++,
              onHistory: () {},
              onVoiceOptions: () {},
              onMemory: () {},
            ),
          ),
        ),
      );

      await tester.pumpWidget(buildSummary());
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-model-summary-identity')),
            )
            .data,
        'Not connected · Conversation model unknown',
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(
                const ValueKey('converse-model-summary-qualification'),
              ),
            )
            .data,
        'Disconnected · Open Connection & privacy to connect',
      );
      expect(
        find.byKey(const ValueKey('converse-model-summary-details')),
        findsNothing,
      );

      // Connected with verified local provider.
      state
        ..connected = true
        ..sessionAuthenticated = true
        ..converseOnline = true
        ..converseInferenceAllowed = true
        ..converseProvider = {
          'provider': 'ollama',
          'model': 'llama3.2:3b',
          'local': true,
        };
      state.touch();
      await tester.pump();
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-model-summary-identity')),
            )
            .data,
        'llama3.2:3b',
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(
                const ValueKey('converse-model-summary-qualification'),
              ),
            )
            .data,
        'Local inference (ollama)',
      );
      expect(
        find.byKey(const ValueKey('converse-model-summary-privacy')),
        findsNothing,
      );

      // Cloud provider without disclosure consent shows privacy warning and Connection & privacy shortcut.
      state
        ..connection = ConnectionProfile.defaults()
        ..converseInferenceAllowed = false
        ..converseProvider = {
          'provider': 'meta',
          'model': 'muse-spark-1.3-contributor',
          'local': false,
        };
      state.touch();
      await tester.pump();
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-model-summary-identity')),
            )
            .data,
        'muse-spark-1.3-contributor',
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(
                const ValueKey('converse-model-summary-qualification'),
              ),
            )
            .data,
        'Cloud provider (meta) · Privacy disclosure required',
      );
      final privacyButton = find.byKey(
        const ValueKey('converse-model-summary-privacy'),
      );
      expect(privacyButton, findsOneWidget);
      await tester.tap(privacyButton);
      await tester.pump();
      expect(openedPrivacy, 1);

      // Expanding technical details does not change model or disclosure state.
      await tester.tap(
        find.byKey(const ValueKey('converse-model-summary-toggle')),
      );
      await tester.pump();
      expect(
        find.byKey(const ValueKey('converse-model-summary-details')),
        findsOneWidget,
      );
      expect(find.text('Provider: meta'), findsOneWidget);
      expect(
        find.text('Disclosure profile: Local inference only'),
        findsOneWidget,
      );
      expect(state.converseInferenceAllowed, isFalse);

      // The quick picker keeps full model settings available.
      await tester.tap(
        find.byKey(const ValueKey('converse-model-summary-settings')),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('Edit models…'));
      await tester.pumpAndSettle();
      expect(openedModelSettings, 1);

      // Narrow 320px at 2x text scaling remains readable without overflow.
      _viewport(tester, const Size(320, 568));
      await tester.pumpWidget(buildSummary(textScale: 2.0));
      await tester.pump();
      expect(tester.takeException(), isNull);
      expect(find.text('Transcript line one'), findsOneWidget);

      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );
}
