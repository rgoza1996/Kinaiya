import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/audio/vad_settings.dart';
import 'package:kinaiya_flutter/ui/voice_options.dart';

void main() {
  test('microphone profile validates acknowledgment and bounded durations', () {
    const settings = VadSettings();
    expect(settings.valid, isTrue);
    final acknowledgment = {'schemaVersion': 1, ...settings.toBridge()};
    expect(
      VadSettings.fromBridge(acknowledgment).toBridge(),
      settings.toBridge(),
    );
    for (final bad in [
      {...acknowledgment, 'schemaVersion': 2},
      {...acknowledgment, 'adaptive': 'yes'},
      {...acknowledgment, 'sensitivity': 'invented'},
      {...acknowledgment, 'minSpeechMs': 99},
      {...acknowledgment, 'bargeMs': 2001},
      {...acknowledgment, 'endSilenceMs': 5001},
    ]) {
      expect(() => VadSettings.fromBridge(bad), throwsFormatException);
    }
  });

  for (final live in [true, false]) {
    testWidgets('Voice controls stay local and respect timing owner: $live', (
      tester,
    ) async {
      final changes = <VadSettings>[];
      var calibration = 0;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: VoiceOptionsSheet(
              embedded: true,
              endSilenceMs: 1500,
              silenceChoices: const [800, 1500, 2500],
              onSilenceChanged: (_) => fail('No automatic silence change'),
              windowMode: live,
              vadSettings: const VadSettings(),
              onVadChanged: changes.add,
              onRecalibrate: () => calibration++,
            ),
          ),
        ),
      );
      expect(changes, isEmpty);
      expect(calibration, 0);
      expect(
        find.text('Speech confirmation'),
        live ? findsNothing : findsOneWidget,
      );
      expect(
        find.text('Interrupt confirmation'),
        live ? findsNothing : findsOneWidget,
      );
      await tester.tap(find.byKey(const Key('vad-adaptive')));
      expect(changes.single.adaptive, isFalse);
      await tester.ensureVisible(find.byKey(const Key('vad-recalibrate')));
      await tester.tap(find.byKey(const Key('vad-recalibrate')));
      expect(calibration, 1);
      expect(tester.takeException(), isNull);
    });
  }
}
