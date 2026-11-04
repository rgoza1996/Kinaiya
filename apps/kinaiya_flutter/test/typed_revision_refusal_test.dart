// Frozen issue450 revision-refusal contract; public synthetic journal fixtures.
// No production repair inspected, no inference or native authentication proof.
import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/converse_handoff.dart';

import 'converse_handoff_test.dart' as old;

const _unavailable =
    'Routing unavailable. No Agent acceptance is confirmed for this input.';
final _scope = old.scope;

JournalEntry _entry(
  String kind,
  int sequence,
  Map<String, Object?> payload, {
  required String id,
  required String origin,
  String session = 's',
  String thread = 't',
  int epoch = 3,
  List<String> causes = const [],
}) => JournalEntry(
  eventId: id,
  sequence: sequence,
  sessionId: session,
  threadId: thread,
  epoch: epoch,
  kind: kind,
  origin: origin,
  payload: payload,
  causes: causes,
);

JournalEntry _revision({
  String id = 'revision-a',
  String target = 'source',
  Object? revision = 2,
  int sequence = 2,
  String origin = 'authenticated-user',
  String session = 's',
  String thread = 't',
  int epoch = 3,
  List<String>? causes,
}) => _entry(
  'conversation.revision',
  sequence,
  {
    'target_event_id': target,
    'revision': revision,
    'text': 'Corrected public request',
  },
  id: id,
  origin: origin,
  session: session,
  thread: thread,
  epoch: epoch,
  causes: causes ?? [target],
);

JournalEntry _refusal({
  String id = 'refusal',
  String source = 'source',
  int revision = 2,
  int sequence = 20,
  String cause = 'revision-a',
}) => _entry(
  'routing.classification_refused',
  sequence,
  {
    'source_event_id': source,
    'source_revision': revision,
    'reason': 'provider_refused',
    'error_type': 'ProviderRefusal',
  },
  id: id,
  origin: 'decisions',
  causes: [cause],
);

ConverseHandoffTracker _seed({List<String>? correctionCauses}) =>
    ConverseHandoffTracker()
      ..observe(old.source(), _scope)
      ..observe(_revision(causes: correctionCauses), _scope);

void _expectUnavailable(HandoffNotice? notice, {int revision = 2}) {
  expect(notice, isNotNull);
  final actual = notice!;
  expect(actual.sourceEventId, 'source');
  expect(actual.sourceRevision, revision);
  expect(actual.text, _unavailable);
  expect(
    actual.status,
    isIn([HandoffStatus.unknown, HandoffStatus.unavailable]),
  );
  expect(actual.hasConfirmedTaskIdentifier, isFalse);
  expect(actual.nativeTaskId, isNull);
}

http.Client _transport() => MockClient((_) async => http.Response('{}', 503));

class _Api extends KinaiyaClient {
  _Api()
    : super.mock(
        sessionBase: 'http://synthetic',
        agentBase: 'http://synthetic',
        converseBase: 'http://synthetic',
        mockTransport: _transport(),
        memoryClientFactory: _transport,
        artifactClientFactory: _transport,
      );
  List<JournalEntry> entries = [];
  Map<String, JournalEntry> references = {};
  final reads = <(String, bool)>[];
  String? heldId;
  final entered = Completer<void>();
  final released = Completer<JournalEntry>();

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async =>
      entries.where((entry) => entry.sequence > after).take(limit).toList();

  @override
  Future<JournalEntry> handoffEntry(
    String eventId, {
    bool resolve = false,
  }) async {
    reads.add((eventId, resolve));
    if (eventId == heldId) {
      if (!entered.isCompleted) entered.complete();
      return released.future;
    }
    final result = references[eventId];
    if (result == null) throw StateError('Synthetic native reference absent');
    return result;
  }

  @override
  Future<List<JournalEntry>> handoffDecisions({
    required String sessionId,
    required String threadId,
    required int after,
  }) async {
    reads.add(('decision-page', false));
    return [];
  }

  @override
  Future<JournalEntry?> handoffLatestWindow({
    required String sessionId,
    required int epoch,
    required String utteranceId,
  }) async {
    reads.add(('latest-window', false));
    return null;
  }
}

AppState _owner(_Api api) {
  final state = AppState()
    ..connect(ConnectionProfile.defaults(), injected: api)
    ..converseSession = _scope;
  addTearDown(state.dispose);
  return state;
}

void main() {
  test(
    'canonical original-user refusal binds the actual authenticated revision cause',
    () {
      final tracker = _seed(correctionCauses: const []);
      _expectUnavailable(tracker.observe(_refusal(), _scope));
      _expectUnavailable(tracker.noticeForSource('source', _scope));
      expect(tracker.noticeForSource('revision-a', _scope), isNull);
    },
  );

  test(
    'historical raw revision ID refusal projects only onto its exact original user',
    () {
      final tracker = _seed(correctionCauses: const []);
      final refusal = _refusal(source: 'revision-a');
      expect(refusal.payload.containsKey('source_sequence'), isFalse);
      _expectUnavailable(tracker.observe(refusal, _scope));
      _expectUnavailable(tracker.noticeForSource('source', _scope));
      expect(tracker.noticeForSource('revision-a', _scope), isNull);
    },
  );

  test(
    'historical alias A cannot borrow current revision B payload or cause',
    () {
      final invalid = [
        _refusal(source: 'revision-a', revision: 3, cause: 'revision-b'),
        _refusal(source: 'revision-a', revision: 3, cause: 'revision-a'),
        _refusal(source: 'revision-a', revision: 2, cause: 'revision-b'),
        _refusal(source: 'revision-a', revision: 2, cause: 'revision-a'),
        _refusal(source: 'source', revision: 3, cause: 'revision-a'),
        _refusal(source: 'revision-b', revision: 3, cause: 'revision-a'),
      ];
      for (final invalidRefusal in invalid) {
        final tracker = _seed()
          ..observe(
            _revision(id: 'revision-b', revision: 3, sequence: 4),
            _scope,
          );
        expect(tracker.observe(invalidRefusal, _scope), isNull);
        expect(tracker.noticeForSource('source', _scope), isNull);
        _expectUnavailable(
          tracker.observe(
            _refusal(
              id: 'valid-current',
              revision: 3,
              cause: 'revision-b',
              sequence: 21,
            ),
            _scope,
          ),
          revision: 3,
        );
      }
    },
  );

  test(
    'exact revision proof requires authenticated scope target scalar order and cause',
    () {
      final invalid = [
        _revision(origin: 'decisions'),
        _revision(origin: 'unauthenticated'),
        _revision(session: 'foreign'),
        _revision(thread: 'foreign'),
        _revision(epoch: 4),
        _revision(id: ''),
        _revision(target: 'unrelated'),
        _revision(revision: true),
        _revision(revision: '2'),
        _revision(revision: 0),
        _revision(revision: null),
        _revision(sequence: 0),
        _revision(sequence: 1),
      ];
      for (final invalidRevision in invalid) {
        for (final sourceId in ['source', 'revision-a']) {
          final tracker = ConverseHandoffTracker()
            ..observe(old.source(revision: 2), _scope)
            ..observe(invalidRevision, _scope);
          expect(tracker.observe(_refusal(source: sourceId), _scope), isNull);
          expect(tracker.noticeForSource('source', _scope), isNull);
        }
      }
    },
  );

  test(
    'new authentic revision invalidates prior refusal without reviving stale alias',
    () {
      final tracker = _seed();
      _expectUnavailable(
        tracker.observe(_refusal(source: 'revision-a'), _scope),
      );
      tracker.observe(
        _revision(id: 'revision-b', revision: 3, sequence: 21),
        _scope,
      );
      expect(tracker.noticeForSource('source', _scope), isNull);
      expect(
        tracker.observe(
          _refusal(id: 'stale', source: 'revision-a', sequence: 22),
          _scope,
        ),
        isNull,
      );
      expect(tracker.noticeForSource('source', _scope), isNull);
      _expectUnavailable(
        tracker.observe(
          _refusal(
            id: 'current',
            revision: 3,
            cause: 'revision-b',
            sequence: 23,
          ),
          _scope,
        ),
        revision: 3,
      );
    },
  );

  for (final acceptanceFirst in [true, false]) {
    test(
      'authentic corrected acceptance outranks raw refusal acceptanceFirst=$acceptanceFirst',
      () {
        final tracker = _seed()
          ..observe(
            old.decision(
              id: 'corrected-native-decision',
              decisionId: 'corrected',
              sequence: 5,
              revision: 2,
              cause: 'revision-a',
            ),
            _scope,
          );
        final accepted = old.receipt(
          'accepted',
          id: 'corrected-receipt',
          sequence: 6,
          revision: 2,
          decisionId: 'corrected',
          cause: 'revision-a',
        );
        final refusal = _refusal(source: 'revision-a', sequence: 20);
        expect(accepted.sequence, lessThan(refusal.sequence));
        if (acceptanceFirst) {
          expect(
            tracker.observe(accepted, _scope)!.hasConfirmedTaskIdentifier,
            isTrue,
          );
          expect(tracker.observe(refusal, _scope), isNull);
        } else {
          _expectUnavailable(tracker.observe(refusal, _scope));
          expect(
            tracker.observe(accepted, _scope)!.hasConfirmedTaskIdentifier,
            isTrue,
          );
        }
        final notice = tracker.noticeForSource('source', _scope)!;
        expect(notice.sourceRevision, 2);
        expect(notice.nativeTaskId, 'task-1');
        expect(notice.status, HandoffStatus.accepted);
        expect(notice.text, 'Agent accepted the request · Task task-1');
      },
    );
  }

  for (final rawAlias in [false, true]) {
    test(
      'fresh owner recovers exact revision lineage without cached aliases raw=$rawAlias',
      () async {
        final api = _Api()
          ..entries = [_refusal(source: rawAlias ? 'revision-a' : 'source')]
          ..references = {
            'source': old.source(revision: 2, text: 'Corrected public request'),
            'revision-a': _revision(causes: []),
          };
        final state = _owner(api);
        await state.refreshNotices();
        _expectUnavailable(state.handoffNotice('source'));
        expect(api.reads.any((read) => read.$1 == 'source'), isTrue);
        expect(api.reads.any((read) => read.$1 == 'revision-a'), isTrue);
        expect(api.reads.length, lessThanOrEqualTo(8));
        expect(
          state.converseHistory.where(
            (message) => message.text == _unavailable,
          ),
          hasLength(1),
        );
        expect(state.tasks, isEmpty);
      },
    );
  }

  test(
    'fresh lookup cannot substitute a foreign or mismatched actual revision',
    () async {
      final invalid = [
        _revision(id: 'different-revision'),
        _revision(target: 'different-user'),
        _revision(revision: 3),
        _revision(session: 'foreign'),
        _revision(thread: 'foreign'),
        _revision(epoch: 4),
        _revision(origin: 'decisions'),
      ];
      for (final invalidRevision in invalid) {
        final api = _Api()
          ..entries = [_refusal(source: 'revision-a')]
          ..references = {
            'source': old.source(revision: 2),
            'revision-a': invalidRevision,
          };
        final state = _owner(api);
        await state.refreshNotices();
        expect(state.handoffNotice('source'), isNull);
        expect(
          state.converseHistory.where(
            (message) => message.text == _unavailable,
          ),
          isEmpty,
        );
        expect(api.reads.length, lessThanOrEqualTo(8));
      }
    },
  );

  test(
    'missing actual revision stays unconfirmed until bounded native recovery',
    () async {
      final api = _Api()
        ..entries = [_refusal()]
        ..references = {'source': old.source(revision: 2)};
      final state = _owner(api);
      await state.refreshNotices();
      expect(state.handoffNotice('source'), isNull);
      expect(api.reads.length, lessThanOrEqualTo(8));
      api.references['revision-a'] = _revision();
      final before = api.reads.length;
      await state.refreshNotices();
      _expectUnavailable(state.handoffNotice('source'));
      expect(api.reads.length - before, inInclusiveRange(1, 8));
    },
  );

  test(
    'new alias-target await cannot publish into a different current epoch',
    () async {
      final api = _Api()
        ..entries = [_refusal(source: 'revision-a')]
        ..references = {'revision-a': _revision()}
        ..heldId = 'source';
      final state = _owner(api);
      final originalRefresh = state.refreshNotices();
      try {
        await api.entered.future.timeout(const Duration(seconds: 2));
        expect(state.handoffNotice('source'), isNull);
        state.converseSession = SessionInfo(
          sessionId: 's',
          threadId: 't',
          epoch: 4,
          channel: 'converse',
        );
      } finally {
        if (!api.released.isCompleted) {
          api.released.complete(old.source(revision: 2));
        }
        await originalRefresh;
      }
      expect(state.handoffNotice('source'), isNull);
      expect(
        state.converseHistory.where((message) => message.text == _unavailable),
        isEmpty,
      );
      expect(api.reads.length, lessThanOrEqualTo(8));
    },
  );

  test(
    'old raw alias remains recoverable under existing bounded source-history pressure',
    () async {
      final api = _Api()
        ..entries = [old.source(), _revision()]
        ..references = {
          'source': old.source(revision: 2, text: 'Corrected public request'),
          'revision-a': _revision(),
        };
      for (var i = 0; i < 300; i++) {
        final id = 'history-user-$i';
        api.entries.add(old.source(id: id, sequence: 3 + 2 * i));
        api.entries.add(
          _revision(id: 'history-revision-$i', target: id, sequence: 4 + 2 * i),
        );
      }
      final state = _owner(api);
      // Seven ordinary <=100-entry pages cover all 602 original history entries.
      for (var page = 0; page < 7; page++) {
        final before = api.reads.length;
        await state.refreshNotices();
        expect(api.reads.length - before, lessThanOrEqualTo(8));
      }
      api.entries.add(_refusal(source: 'revision-a', sequence: 1000));
      final before = api.reads.length;
      await state.refreshNotices();
      _expectUnavailable(state.handoffNotice('source'));
      final recovery = api.reads.skip(before).toList();
      expect(recovery.length, inInclusiveRange(1, 8));
      expect(recovery.any((read) => read.$1 == 'source'), isTrue);
      expect(state.tasks, isEmpty);
    },
  );
}
