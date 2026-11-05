// Synthetic native-read protocol fixtures; no inference, cleanup or auth proof.
// Authored from issue450's frozen contract and existing U89 APIs, before the
// proposed typed-refusal implementation. The refusal has NO source_sequence.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/converse_handoff.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

import 'converse_handoff_test.dart' as old;
import 'support/intelligent_artifact_fixtures.dart';

const _unavailable =
    'Routing unavailable. No Agent acceptance is confirmed for this input.';
final _scope = old.scope;

JournalEntry _entry(
  String kind,
  int sequence,
  Map<String, Object?> payload, {
  String id = 'refusal',
  String origin = 'decisions',
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

JournalEntry _refusal({
  String id = 'refusal',
  String source = 'source',
  int sequence = 20,
  Object? revision = 1,
  String origin = 'decisions',
  String session = 's',
  String thread = 't',
  int epoch = 3,
  List<String>? causes,
  Map<String, Object?> extra = const {},
}) => _entry(
  'routing.classification_refused',
  sequence,
  {
    'source_event_id': source,
    'source_revision': revision,
    'reason': 'provider_refused',
    'error_type': 'ProviderRefusal',
    ...extra,
  },
  id: id,
  origin: origin,
  session: session,
  thread: thread,
  epoch: epoch,
  causes: causes ?? [source],
);

void _expectConservative(HandoffNotice? notice) {
  expect(notice, isNotNull);
  expect(notice!.text, _unavailable);
  expect(notice.hasConfirmedTaskIdentifier, isFalse);
  expect(notice.nativeTaskId, isNull);
  expect(
    notice.status,
    isIn([HandoffStatus.unknown, HandoffStatus.unavailable]),
  );
}

http.Client _unavailableTransport() =>
    MockClient((_) async => http.Response('{}', 503));

class _Api extends KinaiyaClient {
  _Api({List<Map<String, Object?>> artifacts = const []})
    : super.mock(
        sessionBase: 'http://synthetic',
        agentBase: 'http://synthetic',
        converseBase: 'http://synthetic',
        mockTransport: _unavailableTransport(),
        memoryClientFactory: _unavailableTransport,
        artifactClientFactory: () => MockClient((request) async {
          final after = int.parse(request.url.queryParameters['after'] ?? '0');
          return http.Response(
            jsonEncode({
              'items': after == 0 ? artifacts : <Object?>[],
              'next_after': after == 0 && artifacts.isNotEmpty ? 1 : after,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );

  List<JournalEntry> entries = [], decisions = [];
  Map<String, JournalEntry> references = {};
  final nativeReads = <(String, bool)>[];
  final transcriptLimits = <int>[];
  final sourceEntered = Completer<void>();
  Completer<JournalEntry>? heldSource;
  bool failLookup = false;

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async {
    transcriptLimits.add(limit);
    return entries
        .where((entry) => entry.sequence > after)
        .take(limit)
        .toList();
  }

  @override
  Future<JournalEntry> handoffEntry(
    String eventId, {
    bool resolve = false,
  }) async {
    nativeReads.add((eventId, resolve));
    if (resolve && !sourceEntered.isCompleted) sourceEntered.complete();
    if (failLookup) throw StateError('Synthetic native lookup unavailable');
    if (resolve && heldSource != null) return heldSource!.future;
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
    nativeReads.add(('decision-page', false));
    return decisions
        .where((entry) => entry.sequence > after)
        .take(200)
        .toList();
  }

  @override
  Future<JournalEntry?> handoffLatestWindow({
    required String sessionId,
    required int epoch,
    required String utteranceId,
  }) async {
    nativeReads.add(('latest-window', false));
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
    'known authenticated typed source gets conservative refusal, never acceptance',
    () {
      final tracker = ConverseHandoffTracker();
      tracker.observe(old.source(), _scope);
      final refusal = _refusal();
      expect(refusal.payload.containsKey('source_sequence'), isFalse);
      _expectConservative(tracker.observe(refusal, _scope));
      _expectConservative(tracker.noticeForSource('source', _scope));
      expect(
        tracker.noticeForTurn(
          current: _scope,
          role: 'kinaiya',
          eventId: 'source',
        ),
        isNull,
      );
      expect(tracker.observe(refusal, _scope), isNull);
      expect(tracker.notices, hasLength(1));
    },
  );

  test(
    'optimistic accepted-source ID alone is not authenticated source provenance',
    () {
      final tracker = ConverseHandoffTracker()
        ..acceptedSource(_scope, 'source');
      expect(tracker.observe(_refusal(), _scope), isNull);
      expect(tracker.noticeForSource('source', _scope), isNull);
      expect(tracker.pendingReceipts, isNotEmpty);
      tracker.observe(old.source(), _scope);
      final recovered = tracker.drainPending(_scope);
      expect(recovered, hasLength(1));
      _expectConservative(recovered.single);
    },
  );

  test(
    'malformed foreign and unrelated refusal lineage cannot publish a notice',
    () {
      final invalid = [
        _refusal(origin: 'converse'),
        _refusal(origin: 'authenticated-user'),
        _refusal(session: 'other'),
        _refusal(thread: 'other'),
        _refusal(epoch: 4),
        _refusal(source: ''),
        _refusal(source: 'unrelated', causes: ['source']),
        _refusal(causes: []),
        _refusal(causes: ['source', 'unrelated']),
        _refusal(causes: ['unrelated']),
        _refusal(revision: true),
        _refusal(revision: 0),
        _refusal(revision: -1),
        _refusal(revision: '1'),
        _refusal(revision: null),
        _refusal(sequence: 1),
        _refusal(sequence: 0),
        _refusal(extra: {'source_event_id': 123}),
      ];
      for (final refusal in invalid) {
        final tracker = ConverseHandoffTracker()..observe(old.source(), _scope);
        expect(tracker.observe(refusal, _scope), isNull);
        expect(tracker.drainPending(_scope), isEmpty);
        expect(tracker.noticeForSource('source', _scope), isNull);
      }
    },
  );

  test(
    'assistant/window sources cannot acquire ordinary typed refusal authority',
    () {
      for (final source in [
        _entry(
          'conversation.converse',
          1,
          {'revision': 1, 'text': 'Synthetic prose'},
          id: 'source',
          origin: 'converse',
        ),
        old.source(utterance: 'window'),
      ]) {
        final tracker = ConverseHandoffTracker()..observe(source, _scope);
        expect(tracker.observe(_refusal(), _scope), isNull);
        expect(tracker.drainPending(_scope), isEmpty);
        expect(tracker.noticeForSource('source', _scope), isNull);
      }
    },
  );

  test(
    'new original revision supersedes old refusal and cannot be rolled back',
    () {
      final tracker = ConverseHandoffTracker()..observe(old.source(), _scope);
      _expectConservative(tracker.observe(_refusal(), _scope));
      tracker.observe(old.source(revision: 2), _scope);
      tracker.observe(
        old.event(
          'conversation.revision',
          21,
          {'target_event_id': 'source', 'revision': 2},
          id: 'correction',
          causes: ['source'],
        ),
        _scope,
      );
      expect(tracker.noticeForSource('source', _scope), isNull);
      expect(
        tracker.observe(_refusal(id: 'stale-refusal', sequence: 22), _scope),
        isNull,
      );
      expect(tracker.drainPending(_scope), isEmpty);
    },
  );

  test('later refusal cannot erase exact prior native Agent acceptance', () {
    final tracker = ConverseHandoffTracker()
      ..observe(old.source(), _scope)
      ..observe(old.decision(), _scope);
    final accepted = tracker.observe(old.receipt('accepted'), _scope)!;
    expect(accepted.hasConfirmedTaskIdentifier, isTrue);
    expect(tracker.observe(_refusal(), _scope), isNull);
    final retained = tracker.noticeForSource('source', _scope)!;
    expect(retained.status, accepted.status);
    expect(retained.nativeTaskId, 'task-1');
    expect(retained.text, accepted.text);
  });

  test(
    'older authentic acceptance observed after refusal supersedes unconfirmed notice',
    () {
      final tracker = ConverseHandoffTracker()
        ..observe(old.source(), _scope)
        ..observe(old.decision(), _scope);
      final refusal = _refusal(sequence: 20);
      final acceptedReceipt = old.receipt('accepted', sequence: 3);
      expect(acceptedReceipt.sequence, lessThan(refusal.sequence));
      _expectConservative(tracker.observe(refusal, _scope));
      final accepted = tracker.observe(acceptedReceipt, _scope);
      expect(accepted, isNotNull);
      final confirmed = accepted!;
      expect(confirmed.hasConfirmedTaskIdentifier, isTrue);
      expect(confirmed.status, HandoffStatus.accepted);
      expect(confirmed.nativeTaskId, 'task-1');
      expect(confirmed.text, 'Agent accepted the request · Task task-1');
      final retained = tracker.noticeForSource('source', _scope)!;
      expect(retained.hasConfirmedTaskIdentifier, isTrue);
      expect(retained.nativeTaskId, 'task-1');
      expect(retained.text, confirmed.text);
    },
  );

  test(
    'fresh poll recovers absent source through bounded native reads without decision',
    () async {
      final api = _Api()
        ..entries = [_refusal()]
        ..references = {'source': old.source()};
      final state = _owner(api);
      await state.refreshNotices();
      _expectConservative(state.handoffNotice('source'));
      expect(api.nativeReads, contains(('source', true)));
      expect(api.nativeReads.length, lessThanOrEqualTo(8));
      expect(api.transcriptLimits, everyElement(lessThanOrEqualTo(200)));
      expect(
        state.converseHistory.where((m) => m.text == _unavailable),
        hasLength(1),
      );
      await state.refreshNotices();
      expect(
        state.converseHistory.where((m) => m.text == _unavailable),
        hasLength(1),
      );
      expect(state.tasks, isEmpty);
    },
  );

  test(
    'failed source lookup stays unconfirmed and later GET recovery uses original refusal',
    () async {
      final api = _Api()
        ..entries = [_refusal()]
        ..references = {'source': old.source()}
        ..failLookup = true;
      final state = _owner(api);
      await state.refreshNotices();
      expect(state.handoffNotice('source'), isNull);
      expect(
        state.converseHistory.where((m) => m.text == _unavailable),
        isEmpty,
      );
      expect(api.nativeReads.length, inInclusiveRange(1, 8));
      api.failLookup = false;
      final before = api.nativeReads.length;
      await state.refreshNotices();
      _expectConservative(state.handoffNotice('source'));
      expect(api.nativeReads.length - before, inInclusiveRange(1, 8));
      expect(state.tasks, isEmpty);
    },
  );

  test(
    'resolved source must still be exact original authenticated USER and current revision',
    () async {
      for (final source in [
        old.source(id: 'wrong-id'),
        old.event(
          'conversation.user',
          1,
          {'revision': 1, 'text': 'Synthetic'},
          id: 'source',
          origin: 'converse',
        ),
        old.event(
          'conversation.user',
          1,
          {'revision': 1, 'text': 'Synthetic'},
          id: 'source',
          thread: 'foreign',
        ),
        old.event(
          'conversation.user',
          1,
          {'revision': 1, 'text': 'Synthetic'},
          id: 'source',
          epoch: 4,
        ),
        old.event(
          'conversation.converse',
          1,
          {'revision': 1, 'text': 'Synthetic'},
          id: 'source',
          origin: 'converse',
        ),
        old.source(sequence: 20),
        old.source(revision: 2),
      ]) {
        final api = _Api()
          ..entries = [_refusal()]
          ..references = {'source': source};
        final state = _owner(api);
        await state.refreshNotices();
        expect(state.handoffNotice('source'), isNull);
        expect(
          state.converseHistory.where((m) => m.text == _unavailable),
          isEmpty,
        );
        expect(api.nativeReads.length, lessThanOrEqualTo(8));
      }
    },
  );

  for (final change in ['connection', 'session']) {
    test(
      'held source lookup cannot publish into newer $change owner',
      () async {
        final api = _Api()
          ..entries = [_refusal()]
          ..heldSource = Completer<JournalEntry>();
        final state = _owner(api);
        final pending = state.refreshNotices();
        try {
          await api.sourceEntered.future.timeout(const Duration(seconds: 2));
          expect(state.handoffNotice('source'), isNull);
          if (change == 'connection') {
            state.disconnect();
            state.connect(ConnectionProfile.defaults(), injected: _Api());
            state.converseSession = _scope;
          } else {
            state.converseSession = SessionInfo(
              sessionId: 's',
              threadId: 't',
              epoch: 4,
              channel: 'converse',
            );
          }
        } finally {
          api.heldSource!.complete(old.source());
          await pending;
        }
        expect(state.handoffNotice('source'), isNull);
        expect(
          state.converseHistory.where((m) => m.text == _unavailable),
          isEmpty,
        );
      },
    );
  }

  test(
    'recovery stays within the existing eight-read budget across several refusals',
    () async {
      final api = _Api();
      for (var i = 0; i < 12; i++) {
        final id = 'source-$i';
        api.references[id] = old.source(id: id, sequence: i + 1);
        api.entries.add(
          _refusal(id: 'refusal-$i', source: id, sequence: 20 + i),
        );
      }
      final state = _owner(api);
      for (var poll = 0; poll < 12; poll++) {
        final before = api.nativeReads.length;
        await state.refreshNotices();
        expect(api.nativeReads.length - before, lessThanOrEqualTo(8));
      }
      for (var i = 0; i < 12; i++) {
        _expectConservative(state.handoffNotice('source-$i'));
      }
      expect(
        state.converseHistory.where((m) => m.text == _unavailable),
        hasLength(12),
      );
    },
  );

  test(
    'refusal preserves accepted tasks and independently published artifact metadata',
    () async {
      final published = artifactFixture(
        sessionId: 's',
        threadId: 't',
        taskId: 'task-1',
      )..['epoch'] = 3;
      final api = _Api(artifacts: [published])
        ..entries = [
          old.source(),
          old.decision(),
          old.receipt('accepted'),
          old.source(id: 'other-source', sequence: 4),
        ];
      final state = _owner(api)..sessionAuthenticated = true;
      final task = AgentTask(
        taskId: 'task-1',
        sessionId: 'agent-session',
        threadId: 'agent-thread',
        epoch: 1,
        objective: 'Synthetic accepted task',
        objectiveRevision: 1,
        state: 'running',
        idempotencyKey: 'synthetic-task',
      );
      state.setTasks([task]);
      await state.refreshArtifacts();
      final retainedArtifact = state.converseArtifacts.single;
      await state.refreshNotices();
      expect(state.handoffNotice('source')!.hasConfirmedTaskIdentifier, isTrue);
      api.entries.addAll([
        _refusal(id: 'late-refusal', sequence: 20),
        _refusal(id: 'other-refusal', source: 'other-source', sequence: 21),
      ]);
      api.references = {
        'source': old.source(),
        'other-source': old.source(id: 'other-source', sequence: 4),
      };
      await state.refreshNotices();
      _expectConservative(state.handoffNotice('other-source'));
      expect(state.handoffNotice('source')!.nativeTaskId, 'task-1');
      expect(state.tasks.single, same(task));
      expect(state.tasks.single.state, 'running');
      expect(
        state.converseArtifacts.single.artifactId,
        retainedArtifact.artifactId,
      );
      expect(
        state.converseArtifacts.single.contentSha256,
        retainedArtifact.contentSha256,
      );
    },
  );

  testWidgets(
    'ordinary typed refusal is visibly labelled beside its original user turn',
    (tester) async {
      final api = _Api()
        ..entries = [old.source(), _refusal()]
        ..references = {'source': old.source()};
      final state = _owner(api);
      state.converseHistory.add(
        ChatMsg(role: 'user', text: 'Public request', eventId: 'source'),
      );
      await state.refreshNotices();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: CharacterView(state: state, showCharacter: false),
          ),
        ),
      );
      await tester.pump();
      expect(find.text(_unavailable), findsWidgets);
      expect(find.textContaining('Agent accepted the request'), findsNothing);
      _expectConservative(state.handoffForMessage(state.converseHistory.first));
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
}
