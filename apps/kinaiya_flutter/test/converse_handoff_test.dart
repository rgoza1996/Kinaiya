import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/converse_handoff.dart';

final scope = SessionInfo(
  sessionId: 's',
  threadId: 't',
  epoch: 3,
  channel: 'converse',
);
JournalEntry event(
  String kind,
  int sequence,
  Map<String, Object?> payload, {
  String id = 'source',
  String? origin,
  String thread = 't',
  int epoch = 3,
  List<String> causes = const [],
}) => JournalEntry(
  eventId: id,
  sequence: sequence,
  sessionId: 's',
  threadId: thread,
  epoch: epoch,
  kind: kind,
  origin:
      origin ??
      (kind.startsWith('conversation.') ? 'authenticated-user' : 'decisions'),
  payload: payload,
  causes: causes,
);
JournalEntry source({
  String id = 'source',
  int sequence = 1,
  String? utterance,
  int index = 0,
  int revision = 1,
  String text = 'Public request',
}) => event(
  utterance == null ? 'conversation.user' : 'conversation.window',
  sequence,
  {
    'revision': revision,
    'text': text,
    'final': true,
    if (utterance != null) ...{
      'utterance_id': utterance,
      'window_index': index,
    },
  },
  id: id,
);
JournalEntry decision({
  bool window = false,
  String id = 'native-decision',
  String decisionId = 'decision',
  String sourceId = 'source',
  int sourceSequence = 1,
  int revision = 1,
  int sequence = 2,
  String cause = 'source',
  String route = 'queue',
  String? target,
  String origin = 'decisions',
  String thread = 't',
}) => event(
  window ? 'routing.window_decided' : 'routing.decided',
  sequence,
  {
    'decision_id': decisionId,
    'source_revision': revision,
    'target_task_id': target,
    if (window) ...{
      'source_event_id': sourceId,
      'source_sequence': sourceSequence,
      'agent_route': route == 'queue'
          ? 'QUEUE_UNTIL_TASK_DONE'
          : 'STEER_CURRENT_TASK',
    } else ...{
      'source_event_ids': [sourceId],
      'route': route,
    },
  },
  id: id,
  causes: [cause],
  origin: origin,
  thread: thread,
);
JournalEntry receipt(
  String state, {
  bool window = false,
  String id = 'receipt',
  int sequence = 3,
  String sourceId = 'source',
  int sourceSequence = 1,
  int revision = 1,
  String decisionId = 'decision',
  String? cause,
  String route = 'queue',
  String? target,
  String origin = 'decisions',
  String thread = 't',
  int epoch = 3,
  Map<String, Object?> extra = const {},
}) => event(
  window ? 'routing.window_dispatch' : 'routing.dispatch',
  sequence,
  {
    'source_event_id': sourceId,
    'source_sequence': sourceSequence,
    'source_revision': revision,
    'decision_id': decisionId,
    'target_task_id': target,
    'state': state,
    if (window)
      'agent_route': route == 'queue'
          ? 'QUEUE_UNTIL_TASK_DONE'
          : 'STEER_CURRENT_TASK'
    else
      'route': route,
    if (state == 'accepted' || state == 'applied') ...{
      'native_task_id': 'task-1',
      'native_task_state': 'running',
    },
    ...extra,
  },
  id: id,
  causes: [cause ?? (window ? 'native-decision' : 'source')],
  origin: origin,
  thread: thread,
  epoch: epoch,
);

class ReceiptApi extends KinaiyaClient {
  ReceiptApi()
    : super(
        sessionBase: 'http://synthetic',
        agentBase: 'http://synthetic',
        converseBase: 'http://synthetic',
        ownerToken: 'synthetic-owner',
      );
  List<JournalEntry> entries = [], decisions = [];
  Map<String, JournalEntry> references = {};
  JournalEntry? latest;
  int reads = 0, causalReads = 0, pageSize = 200;
  bool failPages = false;
  final decisionCursors = <int>[];
  Completer<JournalEntry>? pendingSource;
  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async {
    reads++;
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
    causalReads++;
    if (resolve && pendingSource != null) return pendingSource!.future;
    return references[eventId]!;
  }

  @override
  Future<JournalEntry?> handoffLatestWindow({
    required String sessionId,
    required int epoch,
    required String utteranceId,
  }) async {
    causalReads++;
    return latest;
  }

  @override
  Future<List<JournalEntry>> handoffDecisions({
    required String sessionId,
    required String threadId,
    required int after,
  }) async {
    causalReads++;
    decisionCursors.add(after);
    if (failPages) throw StateError('synthetic unavailable lookup');
    return decisions
        .where((entry) => entry.sequence > after)
        .take(pageSize)
        .toList();
  }
}

AppState wired(ReceiptApi api) => AppState()
  ..connect(ConnectionProfile.defaults(), injected: api)
  ..converseSession = scope;

void main() {
  test(
    'source and actual native decision establish waiting then acknowledgment, never classifier output alone',
    () {
      final tracker = ConverseHandoffTracker()..acceptedSource(scope, 'source');
      tracker.observe(source(), scope);
      expect(tracker.observe(receipt('accepted'), scope), isNull);
      expect(tracker.pendingReceipts, hasLength(1));
      tracker.observe(decision(), scope);
      expect(
        tracker.drainPending(scope).single.text,
        'Agent accepted the request · Task task-1',
      );
      expect(tracker.observe(receipt('queued', sequence: 4), scope), isNull);
      expect(tracker.observe(receipt('unknown', sequence: 5), scope), isNull);
      final fresh = ConverseHandoffTracker();
      fresh.observe(source(), scope);
      fresh.observe(decision(), scope);
      expect(
        fresh.observe(receipt('queued'), scope)!.text,
        'Waiting for Agent acceptance.',
      );
      expect(
        fresh.observe(receipt('accepted', sequence: 4), scope)!.text,
        contains('Task task-1'),
      );
    },
  );

  test(
    'unrelated causes, native decisions, source bindings and receipt claims cannot announce acceptance',
    () {
      for (final invalid in [
        receipt('accepted', cause: 'unrelated'),
        receipt('accepted', origin: 'converse'),
        receipt('accepted', thread: 'other'),
        receipt('accepted', epoch: 4),
        receipt('accepted', revision: 2),
        receipt('accepted', sourceSequence: 2),
        receipt('accepted', route: 'steer', target: 'task-1'),
        receipt('accepted', target: 'unrelated-target'),
        receipt('accepted', extra: {'native_task_id': null}),
        receipt('accepted', extra: {'native_task_state': 'imaginary'}),
      ]) {
        final tracker = ConverseHandoffTracker();
        tracker.observe(source(), scope);
        tracker.observe(decision(), scope);
        expect(tracker.observe(invalid, scope), isNull);
        expect(tracker.drainPending(scope), isEmpty);
      }
      for (final invalid in [
        decision(cause: 'other'),
        decision(sourceId: 'other'),
        decision(revision: 2),
        decision(route: 'steer', target: 'task-1'),
        decision(origin: 'converse'),
        decision(thread: 'other'),
        decision(decisionId: 'other'),
      ]) {
        final tracker = ConverseHandoffTracker();
        tracker.observe(source(), scope);
        tracker.observe(invalid, scope);
        expect(tracker.observe(receipt('accepted'), scope), isNull);
        expect(tracker.drainPending(scope), isEmpty);
      }
    },
  );

  test(
    'window receipts bind their actual native decision event and queue may create a different task',
    () {
      final tracker = ConverseHandoffTracker();
      tracker.observe(source(utterance: 'u'), scope);
      tracker.observe(decision(window: true, target: 'prior-task'), scope);
      expect(
        tracker.observe(
          receipt(
            'applied',
            window: true,
            cause: 'source',
            target: 'prior-task',
          ),
          scope,
        ),
        isNull,
      );
      expect(
        tracker
            .observe(
              receipt('applied', window: true, target: 'prior-task'),
              scope,
            )!
            .text,
        contains('task-1'),
      );
      final unrelated = ConverseHandoffTracker();
      unrelated.observe(source(utterance: 'u'), scope);
      unrelated.observe(decision(window: true, sourceSequence: 99), scope);
      expect(
        unrelated.observe(receipt('applied', window: true), scope),
        isNull,
      );
    },
  );

  test(
    'an authenticated newer window with unchanged text does not hide a valid ACK',
    () {
      final tracker = ConverseHandoffTracker();
      tracker.observe(source(utterance: 'u'), scope);
      tracker.observe(decision(window: true), scope);
      tracker.observe(
        source(id: 'same-text', sequence: 4, utterance: 'u', index: 1),
        scope,
      );
      expect(
        tracker
            .observe(receipt('applied', window: true, sequence: 5), scope)!
            .text,
        contains('task-1'),
      );
    },
  );

  test(
    'typed corrections bind the current revision and actual correcting cause',
    () {
      final tracker = ConverseHandoffTracker();
      tracker.observe(source(), scope);
      tracker.observe(decision(), scope);
      expect(tracker.observe(receipt('accepted'), scope), isNotNull);
      // Native resolve after cache/reload returns the original ID/sequence
      // carrying current revision; the separate correction supplies its cause.
      tracker.observe(source(revision: 2), scope);
      tracker.observe(
        event('conversation.revision', 4, {
          'target_event_id': 'source',
          'revision': 2,
        }, id: 'correction'),
        scope,
      );
      tracker.observe(
        decision(
          sequence: 5,
          id: 'corrected-decision',
          decisionId: 'corrected',
          cause: 'correction',
          revision: 2,
        ),
        scope,
      );
      expect(tracker.observe(receipt('accepted', sequence: 6), scope), isNull);
      expect(
        tracker.observe(
          receipt(
            'accepted',
            sequence: 6,
            revision: 2,
            decisionId: 'corrected',
          ),
          scope,
        ),
        isNull,
      );
      expect(
        tracker
            .observe(
              receipt(
                'queued',
                sequence: 6,
                revision: 2,
                decisionId: 'corrected',
                cause: 'correction',
              ),
              scope,
            )!
            .text,
        'Waiting for Agent acceptance.',
      );
      expect(
        tracker
            .observe(
              receipt(
                'accepted',
                sequence: 7,
                revision: 2,
                decisionId: 'corrected',
                cause: 'correction',
              ),
              scope,
            )!
            .text,
        contains('task-1'),
      );
    },
  );

  test(
    'status requires exact window cause; rejection and uncertainty remain generic',
    () {
      final tracker = ConverseHandoffTracker();
      tracker.observe(source(), scope);
      tracker.observe(decision(), scope);
      expect(
        tracker
            .observe(
              receipt(
                'rejected',
                extra: {'http_status': 403, 'error': 'private body'},
              ),
              scope,
            )!
            .text,
        'Agent did not accept this request.',
      );
      expect(
        tracker.observe(receipt('unknown', sequence: 4), scope)!.text,
        contains('unconfirmed'),
      );
      tracker.observe(source(id: 'window', sequence: 5, utterance: 'u'), scope);
      final payload = {
        'source_event_id': 'window',
        'source_sequence': 5,
        'source_revision': 1,
        'phase': 'agent_unavailable',
        'reason': 'agent_status_unavailable',
      };
      expect(
        tracker.observe(
          event('routing.window_status', 6, payload, causes: ['other']),
          scope,
        ),
        isNull,
      );
      expect(
        tracker
            .observe(
              event('routing.window_status', 6, payload, causes: ['window']),
              scope,
            )!
            .text,
        contains('no confirmed acceptance'),
      );
    },
  );

  test(
    'native polling projects one waiting/accepted notice without causal lookups when fully loaded',
    () async {
      final api = ReceiptApi()
        ..entries = [source(), decision(), receipt('queued')];
      final state = wired(api);
      await state.refreshNotices();
      expect(
        state.converseHistory.single.text,
        'Waiting for Agent acceptance.',
      );
      api.entries.add(receipt('accepted', id: 'accepted', sequence: 4));
      await state.refreshNotices();
      expect(state.converseHistory, hasLength(1));
      expect(state.converseHistory.single.text, contains('task-1'));
      expect(api.causalReads, 0);
      state.dispose();
    },
  );

  test(
    'fresh reload recovers skipped native source and decision through authenticated reads',
    () async {
      final api = ReceiptApi()
        ..entries = [receipt('accepted', sequence: 20)]
        ..references = {'source': source()}
        ..decisions = [decision()];
      final state = wired(api);
      await state.refreshNotices();
      expect(state.converseHistory.single.text, contains('task-1'));
      expect(api.causalReads, 3);
      state.dispose();
    },
  );

  test(
    'a newer native receipt recovers a correction skipped by a resumed journal cursor',
    () async {
      final api = ReceiptApi()
        ..entries = [source(), decision(), receipt('queued')];
      final state = wired(api);
      await state.refreshNotices();
      final correction = event('conversation.revision', 4, {
        'target_event_id': 'source',
        'revision': 2,
      }, id: 'correction');
      api.references = {
        'source': source(revision: 2),
        'correction': correction,
      };
      api.decisions = [
        decision(
          sequence: 5,
          decisionId: 'corrected',
          revision: 2,
          cause: 'correction',
        ),
      ];
      api.entries.add(
        receipt(
          'accepted',
          id: 'new-revision-receipt',
          sequence: 20,
          revision: 2,
          decisionId: 'corrected',
          cause: 'correction',
        ),
      );
      await state.refreshNotices();
      expect(state.converseHistory, hasLength(1));
      expect(state.converseHistory.single.text, contains('task-1'));
      expect(api.causalReads, 3);
      state.dispose();
    },
  );

  test(
    'typed causal lookup preserves pagination across short pages and transient failures with eight-read budget',
    () async {
      final api = ReceiptApi()
        ..entries = [receipt('accepted', sequence: 20)]
        ..references = {'source': source()}
        ..pageSize = 1
        ..decisions = [
          for (var n = 2; n < 10; n++)
            decision(sequence: n, id: 'native-$n', decisionId: 'other-$n'),
          decision(sequence: 10),
        ];
      final state = wired(api);
      await state.refreshNotices();
      expect(state.converseHistory, isEmpty);
      expect(api.causalReads, 8);
      api.failPages = true;
      await state.refreshNotices();
      expect(state.converseHistory, isEmpty);
      final failedCursor = api.decisionCursors.last;
      api.failPages = false;
      await state.refreshNotices();
      expect(api.decisionCursors[api.decisionCursors.length - 3], failedCursor);
      expect(state.converseHistory.single.text, contains('task-1'));
      state.dispose();
    },
  );

  test(
    'delayed window ACK recovers an evicted source after 256 windows in another utterance',
    () async {
      final original = source(utterance: 'utterance-A');
      final nativeDecision = decision(window: true);
      final api = ReceiptApi()
        ..entries = [
          original,
          nativeDecision,
          for (var n = 0; n < 256; n++)
            source(
              id: 'other-$n',
              sequence: n + 3,
              utterance: 'utterance-B',
              index: n,
            ),
        ]
        ..references = {'source': original, 'native-decision': nativeDecision}
        ..latest = original;
      final state = wired(api);
      await state.refreshNotices();
      await state.refreshNotices();
      expect(
        state.converseHistory.where(
          (message) => message.note == 'Agent handoff',
        ),
        isEmpty,
      );
      api.entries.add(receipt('applied', window: true, sequence: 400));
      await state.refreshNotices();
      expect(state.converseHistory.last.text, contains('task-1'));
      expect(api.causalReads, 3);
      state.dispose();
    },
  );

  test(
    'malformed latest-window lookup cannot prime a later unverified acceptance',
    () async {
      final original = source(utterance: 'u');
      final api = ReceiptApi()
        ..entries = [receipt('applied', window: true, sequence: 20)]
        ..references = {
          'source': original,
          'native-decision': decision(window: true),
        }
        ..latest = event(
          'conversation.window',
          1,
          original.payload,
          origin: 'converse',
        );
      final state = wired(api);
      await state.refreshNotices();
      expect(state.converseHistory, isEmpty);
      // An unrelated normal journal event must not release staged dependencies
      // when the authoritative latest-window provenance check failed.
      api.entries.add(source(id: 'unrelated', sequence: 21));
      await state.refreshNotices();
      expect(state.converseHistory, isEmpty);
      api.latest = original;
      await state.refreshNotices();
      expect(state.converseHistory.single.text, contains('task-1'));
      state.dispose();
    },
  );

  test(
    'lookup cannot revive a superseded window or a disconnected owner',
    () async {
      final old = source(utterance: 'u');
      final api = ReceiptApi()
        ..entries = [receipt('applied', window: true, sequence: 20)]
        ..references = {
          'source': old,
          'native-decision': decision(window: true),
        }
        ..latest = source(
          id: 'new-source',
          sequence: 5,
          utterance: 'u',
          index: 1,
          text: 'Corrected request',
        );
      final state = wired(api);
      await state.refreshNotices();
      expect(state.converseHistory, isEmpty);
      state.dispose();
      final delayed = ReceiptApi()
        ..entries = [receipt('accepted', sequence: 20)]
        ..pendingSource = Completer<JournalEntry>();
      final owner = wired(delayed);
      final pending = owner.refreshNotices();
      await Future<void>.delayed(Duration.zero);
      owner.disconnect();
      owner.connect(owner.connection!, injected: ReceiptApi());
      owner.converseSession = scope;
      delayed.pendingSource!.complete(source());
      await pending;
      expect(owner.converseHistory, isEmpty);
      owner.dispose();
    },
  );
}
