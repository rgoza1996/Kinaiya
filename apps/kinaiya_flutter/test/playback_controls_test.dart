import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/api/playback_controls.dart';

Map<String, Object?> control() => {
  'runtime_id': 'original-runtime',
  'control': 'generation_stop',
  'status': 'interrupted',
  'alignment': 'delivery_unknown',
  'generation_epoch': 3,
  'next_generation_epoch': 4,
};

JournalEntry entry(
  Map<String, Object?> payload, {
  String kind = 'delivery.updated',
  String origin = 'converse',
  int epoch = 1,
  List<String> causes = const [],
}) => JournalEntry(
  eventId: 'event',
  sequence: 1,
  sessionId: 'session',
  threadId: 'thread',
  epoch: epoch,
  kind: kind,
  origin: origin,
  causes: causes,
  payload: payload,
);

void main() {
  test(
    'native negative stop binds exact original runtime and session scope',
    () {
      final stop = NativePlaybackStop.fromEntry(entry(control()));
      expect(stop.previousGeneration, 3);
      expect(stop.nextGeneration, 4);
      expect(
        stop.matches(
          sessionId: 'session',
          threadId: 'thread',
          epoch: 1,
          runtimeId: 'original-runtime',
        ),
        isTrue,
      );
      for (final scope in [
        ('other', 'thread', 1, 'original-runtime'),
        ('session', 'other', 1, 'original-runtime'),
        ('session', 'thread', 2, 'original-runtime'),
        ('session', 'thread', 1, 'replacement-runtime'),
        ('session', 'thread', 1, null),
      ]) {
        expect(
          stop.matches(
            sessionId: scope.$1,
            threadId: scope.$2,
            epoch: scope.$3,
            runtimeId: scope.$4,
          ),
          isFalse,
        );
      }
      final sourced = NativePlaybackStop.fromEntry(
        entry({...control(), 'source_event_id': 'source'}, causes: ['source']),
      );
      expect(sourced.nextGeneration, 4);
    },
  );

  test(
    'delivery reports and malformed controls cannot grant native stop authority',
    () {
      for (final patch in <Map<String, Object?>>[
        {'control': 'model_says_stop'},
        {'status': 'played'},
        {'alignment': 'conservative_estimate'},
        {'runtime_id': ''},
        {'runtime_id': 'x' * 129},
        {'runtime_id': 'wrong\nidentity'},
        {'generation_epoch': true},
        {'generation_epoch': -1},
        {'generation_epoch': 3.5},
        {'next_generation_epoch': null},
        {'next_generation_epoch': 3},
        {'next_generation_epoch': 2},
        {'next_generation_epoch': 9007199254740992},
        {'next_generation_epoch': 4.5},
        {'event_id': 'pretend-delivery'},
        {'chunk_id': 'pretend-chunk'},
        {'played_seconds': 0},
        {'estimated_text_fraction': 0},
        {'source_event_id': 'source'},
      ]) {
        expect(
          () => NativePlaybackStop.fromEntry(entry({...control(), ...patch})),
          throwsFormatException,
        );
      }
      for (final raw in [
        entry(control(), origin: 'authenticated-user'),
        entry(control(), kind: 'conversation.converse'),
        entry(control(), epoch: 0),
        entry(control(), causes: ['unsourced']),
        entry({...control(), 'source_event_id': 'source'}, causes: ['other']),
        entry(
          {...control(), 'source_event_id': 'source'},
          causes: ['source', 'source'],
        ),
      ]) {
        expect(() => NativePlaybackStop.fromEntry(raw), throwsFormatException);
      }
    },
  );

  test(
    'native coalesced stop permits safe monotonic jumps and initial zero',
    () {
      for (final pair in [
        (0, 1),
        (3, 10),
        (9007199254740990, 9007199254740991),
      ]) {
        final stop = NativePlaybackStop.fromEntry(
          entry({
            ...control(),
            'generation_epoch': pair.$1,
            'next_generation_epoch': pair.$2,
          }),
        );
        expect(stop.nextGeneration, pair.$2);
      }
    },
  );
}
