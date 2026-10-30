import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/converse_interaction.dart';
import 'package:kinaiya_flutter/ui/design_tokens.dart';
import 'package:kinaiya_flutter/ui/visual_theme.dart';

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
  double textScale = 1,
  ThemeData? theme,
  bool activeVoice = false,
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
    body: ConverseInteraction(
      character: character,
      transcript: ListView.builder(
        key: const ValueKey('synthetic-transcript'),
        itemCount: 80,
        itemBuilder: (_, index) => Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Text('Synthetic line $index'),
        ),
      ),
      composer: const TextField(
        key: ValueKey('synthetic-composer'),
        decoration: InputDecoration(hintText: 'Synthetic composer'),
      ),
      modelSummary: const Text('Synthetic supplied model summary'),
      status: status,
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
);

void _viewport(WidgetTester tester, Size size) {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = size;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

void main() {
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
}
