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
JournalEntry native(
  String kind,
  int sequence,
  Map<String, Object?> payload, {
  String id = 'source',
  String? origin,
  String session = 's',
  String thread = 't',
  int epoch = 3,
}) => JournalEntry(
  eventId: id,
  sequence: sequence,
  sessionId: session,
  threadId: thread,
  epoch: epoch,
  kind: kind,
  origin:
      origin ??
      (kind.startsWith('conversation.') ? 'authenticated-user' : 'decisions'),
  payload: payload,
);
Map<String, Object?> receipt(
  String state, {
  int revision = 1,
  String source = 'source',
  int sequence = 1,
}) => {
  'source_event_id': source,
  'source_sequence': sequence,
  'source_revision': revision,
  'decision_id': 'decision',
  'route': 'queue',
  'agent_route': 'QUEUE_UNTIL_TASK_DONE',
  'state': state,
  'reason': 'bounded_reason',
  'target_task_id': null,
  if (state == 'accepted' || state == 'applied') ...{
    'native_task_id': 'task-1',
    'native_task_state': 'running',
  },
};

class ReceiptApi extends KinaiyaClient {
  ReceiptApi()
    : super(
        sessionBase: 'http://synthetic',
        agentBase: 'http://synthetic',
        converseBase: 'http://synthetic',
        ownerToken: 'synthetic-owner',
      );
  List<JournalEntry> entries = [];
  Completer<List<JournalEntry>>? pending;
  int reads = 0;
  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) {
    reads++;
    return pending?.future ??
        Future.value(entries.where((entry) => entry.sequence > after).toList());
  }
}

void main() {
  test(
    'authenticated receipt polling updates one notice and discards old connection reads',
    () async {
      final first = ReceiptApi()
        ..entries = [
          native('conversation.user', 1, {'revision': 1}),
          native('routing.dispatch', 2, receipt('queued'), id: 'queued'),
        ];
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: first);
      state.converseSession = scope;
      await state.refreshNotices();
      expect(state.converseHistory.single.note, 'Agent handoff');
      expect(
        state.converseHistory.single.text,
        'Waiting for Agent acceptance.',
      );
      first.pending = Completer<List<JournalEntry>>();
      final stale = state.refreshNotices();
      state.disconnect();
      final next = ReceiptApi()
        ..entries = [
          native('routing.dispatch', 3, receipt('accepted'), id: 'accepted'),
        ];
      state.connect(state.connection!, injected: next);
      state.converseSession = scope;
      first.pending!.complete([
        native('routing.dispatch', 4, receipt('rejected'), id: 'stale'),
      ]);
      await stale;
      expect(
        state.converseHistory.single.text,
        'Waiting for Agent acceptance.',
      );
      await state.refreshNotices();
      expect(state.converseHistory, hasLength(1));
      expect(state.converseHistory.single.text, contains('Task task-1'));
      expect(next.reads, 1);
      // A different backend must never reuse a previous receipt source or history.
      state.connect(
        ConnectionProfile(
          sessionUrl: 'http://localhost:8870',
          agentUrl: 'http://localhost:8871',
          converseUrl: 'http://localhost:8872',
          ownerToken: 'different-synthetic',
        ),
        injected: ReceiptApi(),
      );
      expect(state.converseHistory, isEmpty);
      expect(state.converseSession, isNull);
      state.dispose();
    },
  );

  test(
    'native source and acknowledgment establish handoff, never dialogue/classification',
    () {
      final tracker = ConverseHandoffTracker()..acceptedSource(scope, 'source');
      expect(
        tracker.observe(
          native('routing.dispatch', 2, receipt('accepted')),
          scope,
        ),
        isNull,
      );
      tracker.observe(native('conversation.user', 1, {'revision': 1}), scope);
      expect(
        tracker.observe(
          native('routing.decided', 2, receipt('accepted')),
          scope,
        ),
        isNull,
      );
      final queued = tracker.observe(
        native('routing.dispatch', 3, receipt('queued')),
        scope,
      )!;
      expect(queued.text, 'Waiting for Agent acceptance.');
      final accepted = tracker.observe(
        native('routing.dispatch', 4, receipt('accepted')),
        scope,
      )!;
      expect(accepted.text, 'Agent accepted the request · Task task-1');
      expect(accepted.text, isNot(contains('succeeded')));
      expect(
        tracker.observe(
          native('routing.dispatch', 5, receipt('unknown')),
          scope,
        ),
        isNull,
      );
      expect(
        tracker.observe(
          native('routing.dispatch', 3, receipt('queued')),
          scope,
        ),
        isNull,
      );
    },
  );

  test(
    'wrong producer, session, thread, epoch, source and native acknowledgment fail closed',
    () {
      for (final invalid in [
        native('routing.dispatch', 2, receipt('accepted'), origin: 'converse'),
        native('routing.dispatch', 2, receipt('accepted'), thread: 'other'),
        native('routing.dispatch', 2, receipt('accepted'), session: 'other'),
        native('routing.dispatch', 2, receipt('accepted'), epoch: 4),
        native('routing.dispatch', 2, receipt('accepted', revision: 2)),
        native('routing.dispatch', 2, receipt('accepted', source: 'foreign')),
        native('routing.dispatch', 2, receipt('accepted', sequence: 2)),
        native('routing.dispatch', 2, {
          ...receipt('accepted'),
          'native_task_id': null,
        }),
        native('routing.dispatch', 2, {
          ...receipt('accepted'),
          'route': 'steer',
          'target_task_id': 'other',
        }),
        native('routing.dispatch', 2, {
          ...receipt('accepted'),
          'native_task_state': 'imaginary',
        }),
      ]) {
        final tracker = ConverseHandoffTracker();
        tracker.observe(native('conversation.user', 1, {'revision': 1}), scope);
        expect(tracker.observe(invalid, scope), isNull);
      }
    },
  );

  test(
    'correction invalidates stale revision and rolling newest source binds receipt',
    () {
      final tracker = ConverseHandoffTracker();
      tracker.observe(native('conversation.user', 1, {'revision': 1}), scope);
      tracker.observe(
        native('conversation.revision', 3, {
          'target_event_id': 'source',
          'revision': 2,
        }),
        scope,
      );
      expect(
        tracker.observe(
          native('routing.dispatch', 4, receipt('queued')),
          scope,
        ),
        isNull,
      );
      expect(
        tracker.observe(
          native('routing.dispatch', 5, receipt('queued', revision: 2)),
          scope,
        ),
        isNotNull,
      );
      tracker.observe(
        native('conversation.window', 10, {
          'utterance_id': 'u',
          'window_index': 0,
          'revision': 1,
        }, id: 'w0'),
        scope,
      );
      tracker.observe(
        native('conversation.window', 12, {
          'utterance_id': 'u',
          'window_index': 1,
          'revision': 1,
        }, id: 'w1'),
        scope,
      );
      expect(
        tracker.observe(
          native(
            'routing.window_dispatch',
            14,
            receipt('applied', source: 'w0', sequence: 10),
          ),
          scope,
        ),
        isNull,
      );
      expect(
        tracker
            .observe(
              native('routing.window_dispatch', 15, {
                ...receipt('applied', source: 'w1', sequence: 12),
                'target_task_id': 'prior-task',
              }),
              scope,
            )!
            .text,
        'Agent accepted the request · Task task-1',
      );
    },
  );

  test(
    'rejection is generic, uncertainty does not justify retry, unavailable is distinct',
    () {
      final tracker = ConverseHandoffTracker();
      tracker.observe(native('conversation.user', 1, {'revision': 1}), scope);
      expect(
        tracker
            .observe(
              native('routing.dispatch', 2, {
                ...receipt('rejected'),
                'http_status': 403,
                'error': 'private details',
              }),
              scope,
            )!
            .text,
        'Agent did not accept this request.',
      );
      expect(
        tracker
            .observe(native('routing.dispatch', 3, receipt('unknown')), scope)!
            .text,
        contains('unconfirmed'),
      );
      tracker.observe(
        native('conversation.window', 4, {
          'utterance_id': 'unavailable-window',
          'window_index': 0,
          'revision': 1,
        }, id: 'window-source'),
        scope,
      );
      expect(
        tracker
            .observe(
              native('routing.window_status', 5, {
                ...receipt('queued', source: 'window-source', sequence: 4),
                'phase': 'agent_unavailable',
                'reason': 'agent_status_unavailable',
              }),
              scope,
            )!
            .text,
        contains('unavailable'),
      );
      tracker.clear();
      expect(
        tracker.observe(
          native('routing.dispatch', 5, receipt('accepted')),
          scope,
        ),
        isNull,
      );
    },
  );
}
