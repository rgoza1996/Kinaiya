import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/voice_options.dart';

Future<void> _pumpSheet(
  WidgetTester tester, {
  required bool windowMode,
  double scale = 1,
  double keyboard = 0,
  required int endSilenceMs,
  required ValueChanged<int> onSilenceChanged,
}) async {
  tester.view.physicalSize = const Size(320, 480);
  tester.view.devicePixelRatio = 1;
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  tester.view.viewInsets = FakeViewPadding(bottom: keyboard);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
  addTearDown(tester.view.resetViewInsets);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: VoiceOptionsSheet(
          endSilenceMs: endSilenceMs,
          silenceChoices: const [800, 1500, 2500],
          onSilenceChanged: onSilenceChanged,
          windowMode: windowMode,
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  addTearDown(() async => tester.pumpWidget(const SizedBox.shrink()));
}

void main() {
  for (final outputSupported in [false, true]) {
    testWidgets(
      'Capture Mode is independent of output support=$outputSupported and unknown timing',
      (tester) async {
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: VoiceOptionsSheet(
                endSilenceMs: 1500,
                silenceChoices: const [800, 1500, 2500],
                onSilenceChanged: (_) => fail('No implicit silence selection'),
                windowMode: false,
                legacyCapture: false,
                captureModeControl: const Text('PUBLIC-CAPTURE-MODE-CONTROL'),
                onRefreshAudioOutputs: outputSupported ? () {} : null,
              ),
            ),
          ),
        );
        expect(find.text('PUBLIC-CAPTURE-MODE-CONTROL'), findsOneWidget);
        expect(
          find.byKey(const ValueKey('voice-group-output')),
          outputSupported ? findsOneWidget : findsNothing,
        );
        expect(find.byType(DropdownButton<int>), findsNothing);
        expect(find.byKey(const ValueKey('voice-group-timing')), findsNothing);
        expect(
          find.text('Call for live listening · Tell for one message'),
          findsOneWidget,
        );
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      },
    );
  }

  for (final windowMode in [true, false]) {
    testWidgets(
      'timing mode $windowMode stays readable at 320px2x with keyboard',
      (tester) async {
        var calls = 0;
        await _pumpSheet(
          tester,
          windowMode: windowMode,
          scale: 2,
          keyboard: 320,
          endSilenceMs: 1500,
          onSilenceChanged: (_) => calls++,
        );
        expect(
          find.byTooltip('Close voice options').hitTestable(),
          findsOneWidget,
        );
        final explanation = windowMode
            ? 'Live audio streams continuously. Decisions chooses when to wait, respond, or interrupt; no silence timer ends the turn.'
            : 'Legacy Silence: silence ends the clip. Not live model timing.';
        await tester.ensureVisible(find.text(explanation));
        await tester.pumpAndSettle();
        expect(calls, 0);
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets(
    'window mode hides silence control and describes Decisions timing',
    (tester) async {
      var calls = 0;
      await _pumpSheet(
        tester,
        windowMode: true,
        endSilenceMs: 1500,
        onSilenceChanged: (_) => calls++,
      );
      expect(find.text('Live'), findsOneWidget);
      expect(find.text('Conversation timing'), findsOneWidget);
      expect(find.text('Silence'), findsNothing);
      expect(find.byType(DropdownButton<int>), findsNothing);
      expect(
        find.text(
          'Live audio streams continuously. Decisions chooses when to wait, respond, or interrupt; no silence timer ends the turn.',
        ),
        findsOneWidget,
      );
      expect(
        find.text('Call for live listening · Tell for one message'),
        findsOneWidget,
      );
      expect(find.byTooltip('Close voice options'), findsOneWidget);
      expect(calls, 0);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'legacy mode keeps silence dropdown and forwards explicit choice',
    (tester) async {
      final seen = <int>[];
      await _pumpSheet(
        tester,
        windowMode: false,
        endSilenceMs: 1500,
        onSilenceChanged: seen.add,
      );
      expect(find.text('Legacy Silence'), findsOneWidget);
      expect(find.text('Live'), findsNothing);
      expect(find.text('Silence'), findsOneWidget);
      expect(find.text('Conversation timing'), findsNothing);
      expect(
        find.text(
          'Legacy Silence: silence ends the clip. Not live model timing.',
        ),
        findsOneWidget,
      );
      expect(
        find.text('Call for live listening · Tell for one message'),
        findsOneWidget,
      );
      await tester.ensureVisible(find.byType(DropdownButton<int>));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(DropdownButton<int>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('2.5s').last);
      await tester.pumpAndSettle();
      expect(seen, [2500]);
      expect(tester.takeException(), isNull);
    },
  );
}
