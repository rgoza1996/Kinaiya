import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/voice_options.dart';

void main() {
  for (final windowMode in [true, false]) {
    testWidgets(
      'actual timing modal $windowMode keeps Close above keyboard at320px2x',
      (tester) async {
        tester.view.physicalSize = const Size(320, 480);
        tester.view.devicePixelRatio = 1;
        tester.platformDispatcher.textScaleFactorTestValue = 2;
        tester.view.viewInsets = const FakeViewPadding(bottom: 320);
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
        addTearDown(tester.view.resetViewInsets);
        var changes = 0;
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: Builder(
                builder: (context) => TextButton(
                  onPressed: () => showVoiceOptions(
                    context: context,
                    endSilenceMs: 1500,
                    silenceChoices: const [800, 1500, 2500],
                    onSilenceChanged: (_) => changes++,
                    windowMode: windowMode,
                  ),
                  child: const Text('Open'),
                ),
              ),
            ),
          ),
        );
        await tester.tap(find.text('Open'));
        await tester.pumpAndSettle();
        final close = find.byTooltip('Close voice options');
        expect(tester.getRect(close).bottom, lessThanOrEqualTo(160));
        expect(close.hitTestable(), findsOneWidget);
        expect(changes, 0);
        expect(tester.takeException(), isNull);
        await tester.tap(close);
        await tester.pumpAndSettle();
        expect(find.byType(VoiceOptionsSheet), findsNothing);
        expect(changes, 0);
        await tester.pumpWidget(const SizedBox.shrink());
      },
    );
  }
}
