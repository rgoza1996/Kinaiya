import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/state/converse_handoff.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

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

  testWidgets(
    'causal handoff status renders beside originating message across states, revisions and stale speech windows',
    (tester) async {
      final semantics = tester.ensureSemantics();
      final api = ReceiptApi();
      final state = wired(api);

      // Add a typed user turn bound to eventId 'source'.
      state.appendHistory(
        AppView.character,
        ChatMsg(role: 'user', text: 'Public request'),
      );
      state.acceptedConverseSource(scope, 'source', 0);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: CharacterView(
              state: state,
              surface: CharacterSurface.interaction,
              showCharacter: false,
              richArtifactsEnabled: false,
            ),
          ),
        ),
      );
      await tester.pump();

      // 1. Decisions route suggestion alone never displays queued or accepted.
      api.entries = [source(), decision()];
      await state.refreshNotices();
      await tester.pump();
      expect(
        find.byKey(const ValueKey('converse-handoff-source')),
        findsNothing,
      );

      // 2. Native queued receipt shows Queued beside the originating message.
      api.entries.add(receipt('queued'));
      await state.refreshNotices();
      await tester.pump();
      expect(
        find.byKey(const ValueKey('converse-handoff-source')),
        findsOneWidget,
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-source')),
            )
            .data,
        'Queued',
      );
      expect(find.byIcon(Icons.schedule_rounded), findsOneWidget);
      expect(find.text('Waiting for Agent acceptance.'), findsOneWidget);
      expect(
        tester
            .getSemantics(find.byKey(const ValueKey('converse-handoff-source')))
            .label,
        contains('Agent handoff Queued: Waiting for Agent acceptance.'),
      );
      // Positioned directly beneath the originating user bubble.
      final userRect = tester.getRect(find.byType(MessageBubble).first);
      final handoffRect = tester.getRect(
        find.byKey(const ValueKey('converse-handoff-source')),
      );
      expect(handoffRect.top, greaterThanOrEqualTo(userRect.bottom - 4));

      // 3. Rejection, not-applied, and unknown states BEFORE terminal acceptance.
      api.entries.add(receipt('rejected', id: 'r-rej', sequence: 4));
      await state.refreshNotices();
      await tester.pump();
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-source')),
            )
            .data,
        'Rejected',
      );
      expect(find.byIcon(Icons.block_rounded), findsOneWidget);
      expect(find.text('Agent did not accept this request.'), findsOneWidget);

      api.entries.add(receipt('not_applied', id: 'r-na', sequence: 5));
      await state.refreshNotices();
      await tester.pump();
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-source')),
            )
            .data,
        'Not applied',
      );
      expect(find.byIcon(Icons.do_not_disturb_on_outlined), findsOneWidget);

      api.entries.add(receipt('unknown', id: 'r-unk', sequence: 6));
      await state.refreshNotices();
      await tester.pump();
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-source')),
            )
            .data,
        'Unconfirmed',
      );
      expect(find.byIcon(Icons.help_outline_rounded), findsOneWidget);

      // 4. Native accepted receipt updates status to Accepted.
      api.entries.add(receipt('accepted', id: 'r-acc', sequence: 7));
      await state.refreshNotices();
      await tester.pump();
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-source')),
            )
            .data,
        'Accepted',
      );
      expect(find.byIcon(Icons.task_alt_rounded), findsOneWidget);
      expect(
        find.text('Agent accepted the request · Task task-1'),
        findsOneWidget,
      );

      // 5. Typed revision invalidates prior revision receipt until new revision receipt arrives.
      api.entries.add(
        event('conversation.revision', 8, {
          'target_event_id': 'source',
          'revision': 2,
          'text': 'Revised request',
        }, id: 'rev-2'),
      );
      await state.refreshNotices();
      await tester.pump();
      expect(
        find.byKey(const ValueKey('converse-handoff-source')),
        findsNothing,
      );

      // 6. Speech window receipt attaches to live window and drops when window text changes.
      api.entries.addAll([
        source(
          id: 'win-0',
          sequence: 9,
          utterance: 'utt-1',
          index: 0,
          text: 'Draft a checklist',
        ),
        event(
          'routing.window_status',
          10,
          {
            'source_event_id': 'win-0',
            'source_sequence': 9,
            'source_revision': 1,
            'phase': 'agent_unavailable',
            'reason': 'agent_status_unavailable',
          },
          id: 'win-unavail',
          causes: ['win-0'],
        ),
      ]);
      await state.refreshNotices();
      await tester.pump();
      expect(
        find.byKey(const ValueKey('converse-handoff-win-0')),
        findsOneWidget,
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-win-0')),
            )
            .data,
        'Unavailable',
      );
      expect(find.byIcon(Icons.cloud_off_rounded), findsOneWidget);

      api.entries.addAll([
        decision(
          window: true,
          id: 'win-dec',
          decisionId: 'win-d1',
          sourceId: 'win-0',
          sourceSequence: 9,
          sequence: 11,
          cause: 'win-0',
        ),
        receipt(
          'applied',
          window: true,
          id: 'win-app',
          sequence: 12,
          sourceId: 'win-0',
          sourceSequence: 9,
          decisionId: 'win-d1',
          cause: 'win-dec',
        ),
      ]);
      await state.refreshNotices();
      await tester.pump();
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-win-0')),
            )
            .data,
        'Applied',
      );
      expect(find.byIcon(Icons.published_with_changes_rounded), findsOneWidget);

      // A newer speech window in the same utterance with changed text invalidates the stale window receipt.
      api.entries.add(
        source(
          id: 'win-1',
          sequence: 13,
          utterance: 'utt-1',
          index: 1,
          text: 'Never mind, just chatting',
        ),
      );
      await state.refreshNotices();
      await tester.pump();
      expect(
        find.byKey(const ValueKey('converse-handoff-win-0')),
        findsNothing,
      );
      semantics.dispose();
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      await tester.pump(const Duration(seconds: 1));
    },
  );

  testWidgets(
    'confirmed handoff links to exact known task, reports missing/stale/unauthorized destinations without selecting unrelated task, and preserves drafts/audio/back navigation',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1440, 900));
      addTearDown(() => tester.binding.setSurfaceSize(null));

      final api = ReceiptApi();
      final state = wired(api);
      state.setDraft(AppView.character, 'keep converse draft');
      state.setDraft(AppView.agent, 'keep agent draft');

      state.converseHistory.add(
        ChatMsg(role: 'user', text: 'Run the build', eventId: 'source'),
      );
      api.entries = [
        source(id: 'source', sequence: 1, text: 'Run the build'),
        decision(id: 'dec-1', sequence: 2),
        receipt('accepted', id: 'r-acc', sequence: 3),
      ];
      await state.refreshNotices();

      final unrelatedTask = AgentTask(
        taskId: 'task-unrelated',
        sessionId: 'agent-s1',
        threadId: 'agent-t1',
        epoch: 2,
        objective: 'Unrelated background objective',
        objectiveRevision: 1,
        state: 'running',
        idempotencyKey: 'k-unrelated',
      );
      final exactTask = AgentTask(
        taskId: 'task-1',
        sessionId: 'agent-s1',
        threadId: 'agent-t1',
        epoch: 2,
        objective: 'Exact handoff build objective',
        objectiveRevision: 1,
        state: 'running',
        idempotencyKey: 'k-1',
      );

      // 1. Missing destination: task-1 is not in state.tasks (only unrelatedTask is).
      state.setTasks([unrelatedTask]);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ListenableBuilder(
              listenable: state,
              builder: (_, _) => IndexedStack(
                index: state.view == AppView.agent ? 1 : 0,
                children: [
                  CharacterView(state: state, showCharacter: false),
                  AgentChat(state: state),
                ],
              ),
            ),
          ),
        ),
      );
      await tester.pump();

      expect(
        find.byKey(const ValueKey('converse-handoff-open-task-source')),
        findsNothing,
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(
                const ValueKey('converse-handoff-destination-status-source'),
              ),
            )
            .data,
        contains('Task task-1 is unavailable or retired'),
      );

      // Attempting to open a missing destination reports the missing task and never selects unrelatedTask.
      final notice = state.handoffNotice('source')!;
      state.agentSession = SessionInfo(
        sessionId: 'agent-s1',
        threadId: 'agent-t1',
        epoch: 2,
        channel: 'agent',
      );
      state.openHandoffTask(notice);
      await tester.pump();
      expect(state.view, AppView.agent);
      expect(state.agentNotice, contains('Task task-1 is missing or retired'));
      expect(
        find.text('Choose a task to inspect its status and evidence.'),
        findsOneWidget,
      );

      // Return to Converse via Back to Converse button.
      await tester.tap(find.byKey(const ValueKey('agent-return-to-converse')));
      await tester.pump();
      expect(state.view, AppView.character);

      // 2. Stale destination: task-1 belongs to epoch 1 while active session is epoch 2.
      state.setTasks([
        unrelatedTask,
        AgentTask(
          taskId: 'task-1',
          sessionId: 'agent-s1',
          threadId: 'agent-t1',
          epoch: 1,
          objective: 'Stale epoch objective',
          objectiveRevision: 1,
          state: 'running',
          idempotencyKey: 'k-1',
        ),
      ]);
      await tester.pump();

      expect(
        find.byKey(const ValueKey('converse-handoff-open-task-source')),
        findsNothing,
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(
                const ValueKey('converse-handoff-destination-status-source'),
              ),
            )
            .data,
        contains('Task task-1 belongs to an earlier session or epoch'),
      );

      // 3. Unauthorized destination: unauthenticated session hides link and reports reconnect hint.
      state.setTasks([unrelatedTask, exactTask]);
      state.sessionAuthenticated = false;
      state.touch();
      await tester.pump();

      expect(
        find.byKey(const ValueKey('converse-handoff-open-task-source')),
        findsNothing,
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(
                const ValueKey('converse-handoff-destination-status-source'),
              ),
            )
            .data,
        contains('Reconnect to inspect task task-1'),
      );

      // 4. Known destination: authenticated session + matching task-1 shows Open task link.
      state.sessionAuthenticated = true;
      state.beginTurn(5);
      final liveTurnNonce = state.turnNonce;
      final liveVoiceNonce = state.voiceNonce;
      final liveSpeechEpoch = state.speechEpoch;
      state.touch();
      await tester.pump();

      final openTaskBtn = find.byKey(
        const ValueKey('converse-handoff-open-task-source'),
      );
      expect(openTaskBtn, findsOneWidget);

      await tester.tap(openTaskBtn);
      await tester.pump();

      expect(state.view, AppView.agent);
      expect(state.requestedAgentTaskId, 'task-1');
      expect(state.agentSession?.sessionId, 'agent-s1');
      expect(
        find.descendant(
          of: find.byKey(const ValueKey('agent-details-panel')),
          matching: find.text('Exact handoff build objective'),
        ),
        findsOneWidget,
      );

      // Drafts and active audio nonces/epochs are preserved across navigation and return.
      expect(state.converseDraft, 'keep converse draft');
      expect(state.agentDraft, 'keep agent draft');
      expect(state.turnNonce, liveTurnNonce);
      expect(state.voiceNonce, liveVoiceNonce);
      expect(state.speechEpoch, liveSpeechEpoch);

      await tester.tap(find.byKey(const ValueKey('agent-return-to-converse')));
      await tester.pump();
      expect(state.view, AppView.character);

      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      await tester.pump(const Duration(seconds: 1));
    },
  );
}
