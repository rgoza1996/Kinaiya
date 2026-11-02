import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/character_view.dart';

final _converseScope = SessionInfo(
  sessionId: 'conv-s1',
  threadId: 'conv-t1',
  epoch: 3,
  channel: 'converse',
);

final _agentScope = SessionInfo(
  sessionId: 'agent-s1',
  threadId: 'agent-t1',
  epoch: 2,
  channel: 'agent',
);

JournalEntry _entry(
  String kind,
  int sequence,
  Map<String, Object?> payload, {
  required String id,
  String? origin,
  String thread = 'conv-t1',
  int epoch = 3,
  List<String> causes = const [],
}) => JournalEntry(
  eventId: id,
  sequence: sequence,
  sessionId: _converseScope.sessionId,
  threadId: thread,
  epoch: epoch,
  kind: kind,
  origin:
      origin ??
      (kind.startsWith('conversation.') ? 'authenticated-user' : 'decisions'),
  payload: payload,
  causes: causes,
);

JournalEntry _source({
  String id = 'source',
  int sequence = 1,
  int revision = 1,
  String text = 'Run the unit tests and summarize failures in report.md',
}) => _entry(
  'conversation.user',
  sequence,
  {'revision': revision, 'text': text, 'final': true},
  id: id,
);

JournalEntry _decision({
  String id = 'dec-1',
  String decisionId = 'decision-1',
  String sourceId = 'source',
  int revision = 1,
  int sequence = 2,
  String route = 'queue',
}) => _entry(
  'routing.decided',
  sequence,
  {
    'decision_id': decisionId,
    'source_revision': revision,
    'target_task_id': null,
    'source_event_ids': [sourceId],
    'route': route,
  },
  id: id,
  causes: [sourceId],
);

JournalEntry _receipt(
  String state, {
  String id = 'r-1',
  int sequence = 3,
  String sourceId = 'source',
  int sourceSequence = 1,
  int revision = 1,
  String decisionId = 'decision-1',
  String route = 'queue',
  String? nativeTaskId = 'task-1',
  Map<String, Object?> extra = const {},
}) => _entry(
  'routing.dispatch',
  sequence,
  {
    'source_event_id': sourceId,
    'source_sequence': sourceSequence,
    'source_revision': revision,
    'decision_id': decisionId,
    'target_task_id': null,
    'state': state,
    'route': route,
    if ((state == 'accepted' || state == 'applied') &&
        nativeTaskId != null) ...{
      'native_task_id': nativeTaskId,
      'native_task_state': 'running',
    },
    ...extra,
  },
  id: id,
  causes: [sourceId],
);

class _JourneyFakeClient extends KinaiyaClient {
  _JourneyFakeClient()
    : super(
        sessionBase: 'http://synthetic',
        agentBase: 'http://synthetic',
        converseBase: 'http://synthetic',
        ownerToken: 'synthetic-owner',
      );

  List<JournalEntry> entries = [];
  List<JournalEntry> decisions = [];

  @override
  Future<List<JournalEntry>> transcript({
    required String sessionId,
    int after = 0,
    int limit = 200,
  }) async => entries.where((e) => e.sequence > after).take(limit).toList();

  @override
  Future<List<JournalEntry>> handoffDecisions({
    required String sessionId,
    required String threadId,
    required int after,
  }) async => decisions.where((e) => e.sequence > after).take(200).toList();
}

AgentTask _buildTask({
  String taskId = 'task-1',
  String sessionId = 'agent-s1',
  String threadId = 'agent-t1',
  int epoch = 2,
  String objective = 'Run the unit tests and summarize failures in report.md',
  int objectiveRevision = 1,
  required String taskState,
  List<Evidence> evidence = const [],
  String summary = '',
  String? error,
}) => AgentTask(
  taskId: taskId,
  sessionId: sessionId,
  threadId: threadId,
  epoch: epoch,
  objective: objective,
  objectiveRevision: objectiveRevision,
  state: taskState,
  idempotencyKey: 'idem-$taskId',
  evidence: evidence,
  summary: summary,
  error: error,
);

Widget _buildJourneyHarness(
  AppState state, {
  bool animate = true,
  bool disableAnimations = false,
}) => MaterialApp(
  builder: (context, child) => MediaQuery(
    data: MediaQuery.of(context).copyWith(disableAnimations: disableAnimations),
    child: child!,
  ),
  home: Scaffold(
    body: ListenableBuilder(
      listenable: state,
      builder: (_, _) => IndexedStack(
        index: state.view == AppView.agent ? 1 : 0,
        children: [
          CharacterView(
            state: state,
            showCharacter: false,
            animateTransitions: animate,
          ),
          AgentChat(state: state),
        ],
      ),
    ),
  ),
);

void main() {
  setUp(() {
    ConverseHandoffStatusBanner.resetMicrofeedbackForTesting();
  });

  testWidgets(
    'synthetic Converse->Agent handoff and evidence inspection journey covers all states without fabricating readiness or completion (#310)',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1440, 900));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final semantics = tester.ensureSemantics();

      final api = _JourneyFakeClient();
      final state = AppState()
        ..connect(ConnectionProfile.defaults(), injected: api)
        ..converseSession = _converseScope
        ..agentSession = _agentScope
        ..setDraft(AppView.character, 'keep my converse draft intact')
        ..setDraft(AppView.agent, 'keep my agent draft intact');

      state.converseHistory.addAll([
        ChatMsg(
          role: 'user',
          text: 'Run the unit tests and summarize failures in report.md',
          eventId: 'source',
          windowRevision: 1,
        ),
        // Assistant prose claiming acceptance/completion must NEVER fabricate handoff or task completion.
        ChatMsg(
          role: 'kinaiya',
          text:
              'I accepted task-1, ran the tests, and verified report.md is ready!',
          eventId: 'assistant-claim',
        ),
      ]);

      await tester.pumpWidget(_buildJourneyHarness(state));
      await tester.pump();

      state.beginTurn(5);
      final initialTurnNonce = state.turnNonce;
      final initialVoiceNonce = state.voiceNonce;
      final initialSpeechEpoch = state.speechEpoch;

      // 1. Classifier route (routing.decided) + assistant prose + published artifact alone
      // must NOT produce any accepted or succeeded handoff banner.
      api.entries = [
        _source(),
        _decision(),
        _entry(
          'artifact.published',
          3,
          {
            'artifact_id': 'art-unverified',
            'kind': 'markdown',
            'title': 'Premature report',
            'summary': 'Unverified artifact summary',
            'content': '# Report',
            'task_id': 'task-1',
            'objective_revision': 1,
          },
          id: 'art-event-1',
          origin: 'agent',
        ),
      ];
      await state.refreshNotices();
      await tester.pump();

      expect(
        find.byKey(const ValueKey('converse-handoff-source')),
        findsNothing,
      );

      // 2. Waiting (queued) state recorded via authoritative dispatch receipt.
      api.entries.add(_receipt('queued', id: 'r-queued', sequence: 4));
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
      expect(find.text('Waiting for Agent acceptance.'), findsOneWidget);

      // 3. Uncertain (unknown) and Rejected states BEFORE terminal acceptance.
      api.entries.add(_receipt('unknown', id: 'r-uncertain', sequence: 5));
      await state.refreshNotices();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 250));

      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-source')),
            )
            .data,
        'Unconfirmed',
      );

      api.entries.add(_receipt('rejected', id: 'r-rejected', sequence: 6));
      await state.refreshNotices();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 250));

      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-status-source')),
            )
            .data,
        'Rejected',
      );

      // 4. Primary request transitions to Accepted with confirmed task-1.
      api.entries.add(
        _receipt(
          'accepted',
          id: 'r-accepted',
          sequence: 7,
          nativeTaskId: 'task-1',
        ),
      );
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
      // Fresh transition to Accepted triggers restrained microfeedback once.
      expect(
        find.byKey(
          const ValueKey('converse-handoff-microfeedback-active-source'),
        ),
        findsOneWidget,
      );
      await tester.pump(const Duration(milliseconds: 250));
      expect(
        find.byKey(
          const ValueKey('converse-handoff-microfeedback-active-source'),
        ),
        findsNothing,
      );

      // Before Agent tasks are loaded, task progress is Unknown (never inferred from assistant prose or artifacts).
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-task-state-source')),
            )
            .data,
        'Task status: Unknown',
      );

      // 5. Stale/unknown task IDs never navigate to an unrelated active task (#296/#310).
      state.setTasks([
        _buildTask(
          taskId: 'task-other',
          objective: 'Unrelated background cleanup',
          taskState: 'running',
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
        contains('Task task-1 is unavailable or retired'),
      );

      final notice = state.handoffNotice('source')!;
      state.openHandoffTask(notice);
      await tester.pump();

      expect(state.view, AppView.agent);
      expect(state.agentNotice, contains('Task task-1 is missing or retired'));
      // Unrelated task-other is NOT selected; details pane remains collapsed (#291).
      expect(find.byKey(const ValueKey('agent-details-panel')), findsNothing);
      expect(
        find.byKey(const ValueKey('agent-details-collapsed')),
        findsOneWidget,
      );

      await tester.tap(find.byKey(const ValueKey('agent-return-to-converse')));
      await tester.pump();
      expect(state.view, AppView.character);

      // 6. Known task in approval-required state shows Approval needed + Review approval.
      state.recordApprovals([
        ApprovalRequest(
          approvalId: 'app-1',
          taskId: 'task-1',
          tool: 'write_file',
          actionDigest:
              'sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
          objectiveRevision: 1,
          arguments: const {'path': 'report.md'},
          reason: 'Write failure summary to report.md',
          sessionId: _agentScope.sessionId,
          threadId: _agentScope.threadId,
          epoch: _agentScope.epoch,
          status: 'pending',
          reissueSupported: true,
        ),
      ]);
      state.setTasks([
        _buildTask(taskId: 'task-1', taskState: 'awaiting_approval'),
        _buildTask(
          taskId: 'task-other',
          objective: 'Unrelated background cleanup',
          taskState: 'running',
        ),
      ]);
      await tester.pump();

      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-task-state-source')),
            )
            .data,
        'Approval needed',
      );
      final taskActionFinder = find.byKey(
        const ValueKey('converse-handoff-task-action-source'),
      );
      expect(
        find.descendant(
          of: taskActionFinder,
          matching: find.text('Review approval'),
        ),
        findsOneWidget,
      );

      // 7. Artifact receipt alone without native work receipt is labeled Finished (unverified).
      state.recordApprovals(const []);
      state.setTasks([
        _buildTask(
          taskId: 'task-1',
          taskState: 'succeeded',
          evidence: [
            Evidence(
              kind: 'artifact',
              summary: 'Published preview card',
              objectiveRevision: 1,
              path: 'artifact://art-1',
              sha256:
                  '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
              output:
                  '{"artifact_id":"art-1","kind":"markdown","title":"Preview"}',
            ),
          ],
        ),
      ]);
      await tester.pump();

      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-task-state-source')),
            )
            .data,
        'Finished (unverified)',
      );

      // 8. Succeeded-with-evidence shows Task succeeded + Inspect evidence and links to exact Agent task.
      state.setTasks([
        _buildTask(
          taskId: 'task-1',
          taskState: 'succeeded',
          summary: 'Ran pytest and wrote failure summary to report.md',
          evidence: [
            Evidence(
              kind: 'test',
              summary: 'pytest -q (12 passed)',
              objectiveRevision: 1,
              exitCode: 0,
              output: '12 passed in 0.42s',
            ),
            Evidence(
              kind: 'file',
              summary: 'wrote report.md',
              objectiveRevision: 1,
              path: 'report.md',
              sha256:
                  '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
            ),
          ],
        ),
        _buildTask(
          taskId: 'task-other',
          objective: 'Unrelated background cleanup',
          taskState: 'running',
        ),
      ]);
      await tester.pump();

      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-task-state-source')),
            )
            .data,
        'Task succeeded',
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-task-detail-source')),
            )
            .data,
        contains('2 verified receipts'),
      );
      expect(
        find.descendant(
          of: taskActionFinder,
          matching: find.text('Inspect evidence'),
        ),
        findsOneWidget,
      );

      // Verify screen-reader Semantics labels on handoff notice and task progress.
      expect(
        tester
            .getSemantics(find.byKey(const ValueKey('converse-handoff-source')))
            .label,
        contains('Agent handoff Accepted:'),
      );
      expect(
        tester
            .getSemantics(
              find.byKey(
                const ValueKey('converse-handoff-task-progress-source'),
              ),
            )
            .label,
        contains(
          'Task succeeded: Ran pytest and wrote failure summary to report.md (2 verified receipts)',
        ),
      );

      // Tap Inspect evidence -> opens AgentChat with task-1 selected and details pane open.
      await tester.tap(taskActionFinder);
      await tester.pump();

      expect(state.view, AppView.agent);
      expect(state.requestedAgentTaskId, 'task-1');
      expect(find.byKey(const ValueKey('agent-details-panel')), findsOneWidget);
      expect(
        find.descendant(
          of: find.byKey(const ValueKey('agent-details-panel')),
          matching: find.text(
            'Run the unit tests and summarize failures in report.md',
          ),
        ),
        findsOneWidget,
      );

      // Return to Converse and verify Failed state.
      await tester.tap(find.byKey(const ValueKey('agent-return-to-converse')));
      await tester.pump();
      expect(state.view, AppView.character);

      state.setTasks([
        _buildTask(
          taskId: 'task-1',
          taskState: 'failed',
          error: 'Sandbox command exited with status 2.',
        ),
      ]);
      await tester.pump();

      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-task-state-source')),
            )
            .data,
        'Task failed',
      );
      expect(
        tester
            .widget<Text>(
              find.byKey(const ValueKey('converse-handoff-task-detail-source')),
            )
            .data,
        'Sandbox command exited with status 2.',
      );

      // 9. Verify active audio owner and draft/session routing semantics are preserved throughout.
      expect(state.turnNonce, initialTurnNonce);
      expect(state.voiceNonce, initialVoiceNonce);
      expect(state.speechEpoch, initialSpeechEpoch);
      expect(state.converseDraft, 'keep my converse draft intact');
      expect(state.agentDraft, 'keep my agent draft intact');
      expect(state.converseSession?.sessionId, _converseScope.sessionId);
      expect(state.agentSession?.sessionId, _agentScope.sessionId);

      // 10. Narrow screen (320x800) with reduced motion renders cleanly and navigates to Agent task.
      await tester.binding.setSurfaceSize(const Size(320, 800));
      await tester.pumpWidget(
        _buildJourneyHarness(state, animate: false, disableAnimations: true),
      );
      await tester.pump();
      expect(tester.takeException(), isNull);

      final openTaskNarrow = find.byKey(
        const ValueKey('converse-handoff-open-task-source'),
      );
      await tester.ensureVisible(openTaskNarrow);
      await tester.pump();
      await tester.tap(openTaskNarrow);
      await tester.pump();
      expect(state.view, AppView.agent);
      expect(tester.takeException(), isNull);

      semantics.dispose();
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      await tester.pump(const Duration(seconds: 1));
    },
  );
}
