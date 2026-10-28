import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'concept_layout_test.dart'
    show LayoutApi, LayoutAudio, wired, task, settle, close;
import 'mascot_sprites_test.dart' show decoded, currentFrame;

// Synthetic owner transports/audio only. No network, microphone or inference.
class ActivityApi extends LayoutApi {
  final entries = <JournalEntry>[];
  bool failTasks = false;
  int speeches = 0;
  @override
  Future<List<AgentTask>> listTasks() async {
    if (failTasks) throw StateError('Synthetic task read unavailable');
    return fixtures;
  }

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async => entries.where((entry) => entry.sequence > after).toList();

  @override
  Stream<Map<String, Object?>> speech({
    required String eventId,
    required int generationEpoch,
  }) async* {
    speeches++;
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

class ActivityAudio extends LayoutAudio {
  final ended = Completer<void>();
  bool playing = true;
  int queued = 0;
  @override
  Future<bool> queuePcm(String value, int rate) async {
    queued++;
    return true;
  }

  @override
  Future<bool> get isPlaying async => playing;
  @override
  Future<void> awaitPlayback() => ended.future;
}

class DelayedActivityApi extends ActivityApi {
  final reads = <Completer<List<AgentTask>>>[];
  @override
  Future<List<AgentTask>> listTasks() {
    final response = Completer<List<AgentTask>>();
    reads.add(response);
    return response.future;
  }
}

JournalEntry notice() => JournalEntry(
  eventId: 'synthetic-result-notice',
  sequence: 1,
  sessionId: 'converse',
  threadId: 'thread-converse',
  epoch: 1,
  kind: 'conversation.converse',
  origin: 'converse',
  payload: {
    'text': 'Synthetic verified result.',
    'generation_epoch': 2,
    'outcome_event_id': 'synthetic-outcome',
  },
);

MascotWidget mascot(WidgetTester tester) =>
    tester.widget<MascotWidget>(find.byType(MascotWidget));

void main() {
  testWidgets(
    'late task reads cannot replace a newer native activity snapshot',
    (tester) async {
      final api = DelayedActivityApi();
      final state = wired(api);
      final presentation = PresentationController();
      await tester.runAsync(presentation.initialize);
      await tester.pumpWidget(
        KinaiyaApp(
          state: state,
          audio: LayoutAudio(),
          presentation: presentation,
        ),
      );
      await settle(tester);
      expect(api.reads, hasLength(1));
      await tester.pump(const Duration(seconds: 4));
      expect(api.reads, hasLength(2));
      api.reads[1].complete([task('newest', 'running')]);
      await settle(tester);
      expect(state.characterWorkActivity, 'working.generic');
      api.reads[0].complete([task('obsolete', 'awaiting_approval')]);
      await settle(tester);
      expect(state.tasks.single.taskId, 'newest');
      expect(state.characterWorkActivity, 'working.generic');
      await close(tester, state);
      presentation.dispose();
    },
  );

  test(
    'task activity preserves native work facts, including multiple tasks and stale snapshots',
    () {
      final state = AppState()..sessionAuthenticated = true;
      state.setTasks([task('first', 'running'), task('second', 'verifying')]);
      expect(state.characterWorkActivity, 'working.generic');
      state.setTasks([
        task('first', 'running'),
        task('second', 'awaiting_approval'),
      ]);
      expect(state.characterWorkActivity, 'blocked');
      state.setTasks([task('second', 'paused'), task('first', 'running')]);
      expect(state.characterWorkActivity, 'blocked');
      state.markTasksUnavailable();
      expect(state.tasks, hasLength(2));
      expect(state.characterWorkActivity, 'idle');
      state.setTasks([task('first', 'failed'), task('second', 'succeeded')]);
      expect(state.characterWorkActivity, 'idle');
      state.setTasks([task('first', 'running')]);
      state.disconnect();
      expect(state.tasksStatusStale, isTrue);
      expect(state.characterWorkActivity, 'idle');
      state.dispose();
    },
  );

  testWidgets(
    'current work and blocked status paint the actual activity atlas; read failure removes current-activity claim',
    (tester) async {
      final api = ActivityApi()..fixtures = [task('one', 'running')];
      final state = wired(api);
      final presentation = PresentationController();
      await tester.runAsync(presentation.initialize);
      await tester.pumpWidget(
        KinaiyaApp(
          state: state,
          audio: LayoutAudio(),
          presentation: presentation,
        ),
      );
      await settle(tester);
      await decoded(tester);
      expect(mascot(tester).signals!.semantic, 'working.generic');
      expect(currentFrame(tester).src.top, 0);
      final activityImage = currentFrame(tester).image;
      state.setTasks([
        task('one', 'running'),
        task('two', 'awaiting_approval'),
      ]);
      await tester.pump();
      await decoded(tester);
      expect(mascot(tester).signals!.semantic, 'blocked');
      expect(currentFrame(tester).src.top, 313);
      expect(identical(currentFrame(tester).image, activityImage), isTrue);
      api.failTasks = true;
      await tester.pump(const Duration(seconds: 4));
      await tester.pump();
      expect(state.tasksStatusStale, isTrue);
      expect(mascot(tester).signals!.semantic, 'idle');
      expect(state.tasks, hasLength(2));
      expect(api.speeches, 0);
      await close(tester, state);
      presentation.dispose();
    },
  );

  for (final playing in [true, false]) {
    testWidgets(
      'armed microphone paints ${playing ? 'speaking during confirmed playback' : 'listening for queued-only audio'}',
      (tester) async {
        final api = ActivityApi();
        final state = wired(api);
        final audio = ActivityAudio()..playing = playing;
        final presentation = PresentationController();
        await tester.runAsync(presentation.initialize);
        await tester.pumpWidget(
          KinaiyaApp(state: state, audio: audio, presentation: presentation),
        );
        await settle(tester);
        await tester.tap(find.byTooltip('Start Live listen (armed mic)'));
        await settle(tester);
        expect(audio.starts, 1);
        api.entries.add(notice());
        await state.refreshNotices();
        await settle(tester);
        await decoded(tester);
        expect(audio.queued, 1);
        expect(mascot(tester).signals!.listening, isTrue);
        expect(mascot(tester).signals!.speaking, playing);
        expect(
          mascot(tester).signals!.semantic,
          playing ? 'speaking' : 'listening',
        );
        expect(currentFrame(tester).src.top, playing ? 939 : 313);
        expect(api.interrupts, 0);
        audio.ended.complete();
        await settle(tester);
        await close(tester, state);
        presentation.dispose();
      },
    );
  }

  testWidgets(
    'restoration choreography never announces readiness and available speech skips the remaining approach',
    (tester) async {
      final api = ActivityApi();
      final state = wired(api);
      final audio = ActivityAudio();
      final presentation = PresentationController();
      await tester.runAsync(presentation.initialize);
      await tester.pumpWidget(
        KinaiyaApp(state: state, audio: audio, presentation: presentation),
      );
      await settle(tester);
      state.resourcesConfigured = true;
      state.resourceConverseDesired = true;
      state.resourcePhase = 'restoring';
      state.converseEnabled = false;
      state.touch();
      await tester.pump();
      expect(mascot(tester).signals!.warming, isTrue);
      expect(find.byKey(const ValueKey('mascot-approach')), findsOneWidget);
      expect(find.text('Restoring voice…'), findsOneWidget);
      await tester.pump(const Duration(milliseconds: 1400));
      expect(find.byKey(const ValueKey('mascot-approach')), findsNothing);
      expect(find.text('Restoring voice…'), findsOneWidget);
      expect(audio.queued, 0);
      // Begin a fresh restoration, then become available well before its1200ms approach.
      state.resourcePhase = 'failed';
      state.touch();
      await tester.pump();
      expect(find.text('Voice restoration failed'), findsOneWidget);
      state.resourcePhase = 'restoring';
      state.touch();
      await tester.pump();
      expect(find.byKey(const ValueKey('mascot-approach')), findsOneWidget);
      state.resourcePhase = 'ready';
      state.converseEnabled = true;
      state.touch();
      api.entries.add(notice());
      await state.refreshNotices();
      await tester.pump(const Duration(milliseconds: 20));
      await tester.pump();
      expect(audio.queued, 1);
      expect(mascot(tester).signals!.semantic, 'speaking');
      expect(find.byKey(const ValueKey('mascot-approach')), findsNothing);
      audio.ended.complete();
      await settle(tester);
      state.resourcePhase = 'restoring';
      state.resourceConverseDesired = false;
      state.converseEnabled = false;
      state.touch();
      await tester.pump();
      expect(mascot(tester).signals!.warming, isFalse);
      expect(find.text('Converse is off'), findsOneWidget);
      state.resourceConverseDesired = true;
      state.resourceStatusStale = true;
      state.touch();
      await tester.pump();
      expect(mascot(tester).signals!.warming, isFalse);
      expect(find.text('Voice availability unconfirmed'), findsOneWidget);
      await close(tester, state);
      presentation.dispose();
    },
  );

  test(
    'same-backend reconnect does not present retained tasks as fresh work',
    () {
      final api = ActivityApi();
      final state = wired(api);
      state.setTasks([task('one', 'running')]);
      expect(state.characterWorkActivity, 'working.generic');
      state.connect(ConnectionProfile.defaults(), injected: api);
      expect(state.tasks, hasLength(1));
      expect(state.tasksStatusStale, isTrue);
      expect(state.characterWorkActivity, 'idle');
      state.dispose();
    },
  );
}
