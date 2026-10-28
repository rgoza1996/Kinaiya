import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/audio/turn_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'client_test.dart' show mockClient;

AppState wiredState() {
  final state = AppState();
  state.connect(
    ConnectionProfile(
      sessionUrl: 'http://127.0.0.1:8760',
      agentUrl: 'http://127.0.0.1:8761',
      converseUrl: 'http://127.0.0.1:8762',
      ownerToken: 'test-token',
    ),
    injected: mockClient(),
  );
  return state;
}

void main() {
  test(
    'speech queue serializes independently produced entries in order',
    () async {
      final q = SpeechQueue();
      final order = <int>[];
      await Future.wait([
        q.enqueue(() async {
          await Future.delayed(const Duration(milliseconds: 30));
          order.add(1);
        }),
        q.enqueue(() async => order.add(2)),
        q.enqueue(() async => order.add(3)),
      ]);
      expect(order, [1, 2, 3]);
    },
  );

  test('stale nonce never owns cleanup; newer turn keeps busy', () {
    expect(turnOwnsCleanup(7, 7), isTrue);
    expect(turnOwnsCleanup(7, 8), isFalse);
    final state = AppState();
    state.turnNonce = 8;
    // Old finally guard: must not clear the new turn's flag.
    var busy = true;
    if (turnOwnsCleanup(7, state.turnNonce)) busy = false;
    expect(busy, isTrue);
    if (turnOwnsCleanup(8, state.turnNonce)) busy = false;
    expect(busy, isFalse);
  });

  test('interrupted estimate clamps to scheduled; unknown stays null', () {
    final start = DateTime(2026, 1, 1);
    expect(
      estimatePlayedSeconds(
        start: start,
        now: start.add(const Duration(seconds: 1)),
        scheduledSeconds: 4,
      ),
      closeTo(1, 0.001),
    );
    expect(
      estimatePlayedSeconds(
        start: start,
        now: start.add(const Duration(seconds: 9)),
        scheduledSeconds: 4,
      ),
      4,
    );
    expect(
      estimatePlayedSeconds(
        start: start,
        now: start.subtract(const Duration(seconds: 1)),
        scheduledSeconds: 4,
      ),
      isNull,
    );
    expect(
      estimatePlayedSeconds(start: start, now: start, scheduledSeconds: 0),
      isNull,
    );
  });

  test('barge needs 220ms sustained voice during playback', () {
    final d = BargeDetector();
    expect(d.feed(voice: true, playing: false, nowMs: 1000), isFalse);
    expect(d.feed(voice: true, playing: true, nowMs: 2000), isFalse);
    expect(d.feed(voice: true, playing: true, nowMs: 2100), isFalse);
    expect(d.feed(voice: true, playing: true, nowMs: 2220), isTrue);
    d.reset();
    expect(d.feed(voice: false, playing: true, nowMs: 3000), isFalse);
  });

  test('voice session generation is separate from text turns', () {
    final state = AppState();
    state.beginTurn(3);
    final v = state.beginVoiceTurn(state.voiceEpoch + 1);
    expect(state.voiceIsLive(v, state.voiceEpoch), isTrue);
    state.beginTurn(4); // text barge does not kill the voice clip.
    expect(state.voiceIsLive(v, state.voiceEpoch), isTrue);
    expect(state.audioIsLive(state.turnNonce, 4), isTrue);
    state.voiceNonce++; // disarm invalidates stragglers.
    expect(state.voiceIsLive(v, state.voiceEpoch), isFalse);
  });

  testWidgets('live toggle degrades honestly where voice is unavailable', (
    tester,
  ) async {
    final state = wiredState();
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));
    expect(find.text('Live'), findsOneWidget);
    expect(find.text('Silence'), findsOneWidget);
    await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(
      find.text('Live listening is unavailable here; text works.'),
      findsOneWidget,
    );
    expect(state.converseHistory, isNotEmpty);
  });
}
