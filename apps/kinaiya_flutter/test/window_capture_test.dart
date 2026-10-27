import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/audio/speech_audio.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'live_capture_test.dart' show CaptureAudio, ControlledApi;

final session = SessionInfo(
  sessionId: 'converse',
  threadId: 'thread',
  channel: 'converse',
  epoch: 1,
  name: 'Converse',
);

class WindowApi extends ControlledApi {
  final received = <Map<String, Object?>>[];
  final entries = <JournalEntry>[];
  final first = Completer<Map<String, Object?>>();
  @override
  Future<List<SessionInfo>> listSessions() async => [session];
  @override
  Future<List<JournalEntry>> recent({
    required String sessionId,
    int limit = 100,
  }) async => [];
  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async => entries.where((e) => e.sequence > after).toList();
  @override
  Future<Map<String, Object?>> transcribeWindow(Map<String, Object?> window) {
    received.add(window);
    return received.length == 1 ? first.future : Future.value({});
  }

  @override
  Stream<Map<String, Object?>> speech({
    required String eventId,
    required int generationEpoch,
  }) async* {
    yield {'audio': 'AAAAAA==', 'sample_rate': 24000};
  }

  @override
  Future<void> reportDelivery({
    required String eventId,
    required int generationEpoch,
    required String chunkId,
    required String status,
    double playedSeconds = 0,
  }) async {}
}

class WindowAudio extends CaptureAudio implements WindowSpeechAudio {
  bool enabled = false, speaking = true;
  int snapshots = 0, queued = 0;
  @override
  Future<void> configureWindows(bool value) async {
    enabled = value;
  }

  @override
  Future<int> nextUtterance() async => ++epoch;
  @override
  Future<bool> get userSpeaking async => speaking;
  @override
  Future<Map<String, Object?>?> snapshotWindow() async => {
    'base64': 'cHVibGljIHN5bnRoZXRpYw==',
    'capture_epoch': epoch,
    'start_ms': 0,
    'end_ms': 1000 * (++snapshots),
    'user_speaking': speaking,
  };
  @override
  Future<bool> queuePcm(String value, int rate) async {
    queued++;
    return true;
  }
}

Future<AppState> arm(
  WidgetTester tester,
  WindowApi api,
  WindowAudio audio,
) async {
  final state = AppState()
    ..connect(ConnectionProfile.defaults(), injected: api);
  state.streamingWindows = true;
  state.converseSession = session;
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: CharacterView(state: state, audio: audio),
      ),
    ),
  );
  await tester.pump(const Duration(milliseconds: 100));
  await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
  await tester.pump(const Duration(milliseconds: 100));
  return state;
}

void main() {
  testWidgets(
    'rolling capture continues through STT and acoustic overlap cannot interrupt',
    (tester) async {
      final api = WindowApi();
      final audio = WindowAudio();
      final state = await arm(tester, api, audio);
      expect(audio.enabled, isTrue);
      expect(audio.clips, 0);
      expect(find.text('Conversation timing'), findsOneWidget);
      expect(find.text('Silence'), findsNothing);
      for (var n = 0; n < 30; n++) {
        await tester.pump(const Duration(milliseconds: 100));
      }
      expect(audio.snapshots, greaterThanOrEqualTo(3));
      expect(
        api.received.length,
        1,
        reason: 'one STT in flight; only latest raw snapshot waits',
      );
      audio.events.add({'type': 'barge', 'epoch': audio.epoch});
      await tester.pump(const Duration(milliseconds: 100));
      expect(api.interrupts, 0);
      api.first.complete({});
      await tester.pump();
      expect(api.received.length, 2);
      expect(api.received.last['window_index'], greaterThan(1));
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'prepared speech waits for opening and starts next utterance without closing mic',
    (tester) async {
      final api = WindowApi();
      final audio = WindowAudio();
      final state = await arm(tester, api, audio);
      for (var n = 0; n < 11; n++) {
        await tester.pump(const Duration(milliseconds: 100));
      }
      final utterance = api.received.first['utterance_id'];
      final captureEpoch = audio.epoch;
      api.entries.add(
        JournalEntry(
          eventId: 'reply',
          sequence: 1,
          sessionId: 'converse',
          threadId: 'thread',
          epoch: 1,
          kind: 'conversation.converse',
          origin: 'converse',
          payload: {
            'text': 'Hello.',
            'generation_epoch': 2,
            'window_source_id': 'source',
            'window_utterance_id': utterance,
            'window_behavior': 'RESPOND_WITHOUT_INTERRUPTING_USER',
          },
        ),
      );
      await state.refreshNotices();
      await tester.pump();
      expect(audio.queued, 0);
      expect(audio.epoch, captureEpoch);
      audio.speaking = false;
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pump();
      expect(audio.queued, 1);
      expect(audio.epoch, greaterThan(captureEpoch));
      expect(find.text('Listening (live)'), findsOneWidget);
      api.first.complete({});
      await tester.pump();
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );
}
