import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

import 'live_capture_test.dart' show CaptureAudio;

class ResourceApi extends KinaiyaClient {
  ResourceApi()
    : super(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic',
      );
  String controller = 'first';
  String phase = 'ready';
  int revision = 1;
  Object? ownerRevision = 0;
  bool desiredConverse = true;
  bool fail = false;
  bool malformed = false;

  @override
  Future<Map<String, Object?>> resourceStatus() async {
    if (fail) throw StateError('Synthetic offline resources');
    if (malformed) return {'policy': {}};
    return {
      'controller_id': controller,
      'policy': {
        'revision': revision,
        'phase': phase,
        'desired_converse': desiredConverse,
        if (ownerRevision != null) 'owner_intent_revision': ownerRevision,
      },
    };
  }

  @override
  Future<Map<String, Object?>> converseHealth() async => {'enabled': true};
}

class PendingMicAudio extends CaptureAudio {
  final requests = <Completer<String>>[];
  int cancellations = 0;

  @override
  Future<String> startRecording() {
    final request = Completer<String>();
    requests.add(request);
    return request.future;
  }

  @override
  Future<void> cancelRecording() async {
    cancellations++;
  }
}

void main() {
  test(
    'owner-off revision stops voice while worker revision is unchanged',
    () async {
      final api = ResourceApi();
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api);
      await state.refreshResources();
      expect(state.converseEnabled, isTrue);
      state.turnNonce = 10;
      api.desiredConverse = false;
      api.ownerRevision = 1;
      await state.refreshResources();
      expect(state.converseEnabled, isFalse);
      expect(state.turnNonce, 11);
      await state.refreshResources();
      expect(state.turnNonce, 11);
      api.desiredConverse = true;
      api.ownerRevision = 2;
      api.phase = 'off';
      await state.refreshResources();
      expect(state.converseEnabled, isFalse);
      api.revision++;
      api.phase = 'ready';
      await state.refreshResources();
      expect(state.converseEnabled, isTrue);
      api.ownerRevision = 1;
      await state.refreshResources();
      expect(state.resourceStatusStale, isTrue);
      expect(state.converseEnabled, isFalse);
      api.ownerRevision = 'invalid';
      await state.refreshResources();
      expect(state.resourceStatusStale, isTrue);
      api.controller = 'new';
      api.revision = 0;
      api.ownerRevision = 0;
      await state.refreshResources();
      expect(state.resourceStatusStale, isFalse);
      expect(state.converseEnabled, isTrue);
      state.dispose();
    },
  );

  testWidgets(
    'late mic completion cannot rearm paused voice or cancel a newer recording',
    (tester) async {
      final api = ResourceApi();
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api);
      final audio = PendingMicAudio();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AnimatedBuilder(
              animation: state,
              builder: (context, _) =>
                  CharacterView(state: state, audio: audio),
            ),
          ),
        ),
      );
      await tester.pump(const Duration(milliseconds: 100));
      await tester.tap(find.byTooltip('Record voice'));
      await tester.pump();
      expect(audio.requests.length, 1);
      api.phase = 'local_work';
      api.revision++;
      await state.refreshResources();
      await tester.pump();
      expect(audio.cancellations, greaterThan(0));
      expect(
        tester
            .widget<OutlinedButton>(find.byKey(const ValueKey('tell-kinaiya')))
            .onPressed,
        isNull,
      );
      api.phase = 'ready';
      api.revision++;
      await state.refreshResources();
      await tester.pump();
      await tester.tap(find.byTooltip('Record voice'));
      await tester.pump();
      expect(audio.requests.length, 2);
      final cancellations = audio.cancellations;
      audio.requests[0].complete('recording');
      await tester.pump();
      expect(audio.cancellations, cancellations);
      expect(find.byTooltip('Stop recording'), findsNothing);
      audio.requests[1].complete('recording');
      await tester.pump();
      expect(find.byTooltip('Stop recording'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  test(
    'resource revisions cancel once, restore honestly, and recover controller identity',
    () async {
      final api = ResourceApi();
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api);
      var stops = 0;
      final subscription = state.resourceStops.listen((_) {
        stops++;
      });
      await state.refreshResources();
      expect(state.converseEnabled, isTrue);
      state.turnNonce = 7;
      api.phase = 'local_work';
      api.revision = 2;
      await state.refreshResources();
      await Future<void>.delayed(Duration.zero);
      expect(state.converseEnabled, isFalse);
      expect(state.turnNonce, 8);
      expect(stops, 1);
      await state.refreshResources();
      expect(state.turnNonce, 8);
      api.phase = 'ready';
      api.revision = 3;
      await state.refreshResources();
      expect(state.converseEnabled, isTrue);
      api.controller = 'restarted';
      api.phase = 'off';
      api.revision = 1;
      await state.refreshResources();
      await Future<void>.delayed(Duration.zero);
      expect(stops, 2);
      expect(state.converseEnabled, isFalse);
      api.fail = true;
      await state.refreshResources();
      expect(state.resourceStatusStale, isTrue);
      expect(state.converseEnabled, isFalse);
      api.fail = false;
      api.phase = 'ready';
      api.revision++;
      await state.refreshResources();
      expect(state.converseEnabled, isTrue);
      api.malformed = true;
      await state.refreshResources();
      expect(state.resourceStatusStale, isTrue);
      expect(state.converseEnabled, isFalse);
      api.malformed = false;
      await state.refreshResources();
      expect(state.resourceStatusStale, isFalse);
      expect(state.converseEnabled, isTrue);
      await subscription.cancel();
      state.dispose();
    },
  );

  testWidgets(
    'resource pause stops local playback and capture without rearming on restore',
    (tester) async {
      final api = ResourceApi();
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api);
      final audio = CaptureAudio();
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
      expect(audio.clips, 1);
      final priorStops = audio.stops;
      api.phase = 'local_work';
      api.revision = 2;
      await state.refreshResources();
      await tester.pump();
      expect(audio.stops, greaterThan(priorStops));
      expect(
        find.textContaining('Voice is paused for local work'),
        findsOneWidget,
      );
      for (final control in <ButtonStyleButton>[
        tester.widget<FilledButton>(find.byKey(const ValueKey('call-kinaiya'))),
        tester.widget<OutlinedButton>(
          find.byKey(const ValueKey('tell-kinaiya')),
        ),
      ]) {
        expect(control.onPressed, isNull);
      }
      expect(
        tester
            .widget<IconButton>(
              find.byWidgetPredicate(
                (w) => w is IconButton && w.tooltip == 'Use audio recording',
              ),
            )
            .onPressed,
        isNull,
      );
      final priorClips = audio.clips;
      api.phase = 'ready';
      api.revision = 3;
      await state.refreshResources();
      await tester.pump(const Duration(milliseconds: 200));
      expect(audio.clips, priorClips);
      api.fail = true;
      await state.refreshResources();
      await tester.pump();
      expect(
        find.textContaining('Voice availability is unconfirmed'),
        findsOneWidget,
      );
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );
}
