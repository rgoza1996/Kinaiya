import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/agent_workspace.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

Map<String, Object?> sessionJson(
  String id, {
  String? harness = 'seed-v1',
  int epoch = 1,
}) => {
  'schema_version': '1.0',
  'session_id': id,
  'thread_id': 'thread-$id',
  'channel': 'agent',
  'epoch': epoch,
  'name': 'Synthetic $id',
  'harness_version': harness,
};
Map<String, Object?> harnessJson(
  String id, {
  bool selectable = true,
  bool active = false,
}) => {
  'version_id': id,
  'name': id,
  'kind': id == 'seed-v1' ? 'seed_runtime' : 'context_selector.v1',
  'description': 'Synthetic registered entry',
  'active': active,
  'selectable': selectable,
  'eligibility': selectable ? 'approved_baseline' : 'unapproved',
  'registered_at': null,
};
AgentHarnessPage catalog() => AgentHarnessPage.fromJson({
  'schema_version': '1.0',
  'configured': false,
  'items': [
    harnessJson('seed-v1', active: true),
    harnessJson('unapproved', selectable: false),
  ],
  'next_cursor': null,
});
AgentSessionPage page(Iterable<SessionInfo> rows, {String? next}) =>
    AgentSessionPage.fromJson({
      'schema_version': '1.0',
      'items': rows
          .map(
            (x) => sessionJson(
              x.sessionId,
              harness: x.harnessVersion,
              epoch: x.epoch,
            ),
          )
          .toList(),
      'next_cursor': next,
    });
JournalEntry entry(
  String session,
  int sequence, {
  int epoch = 1,
  String origin = 'agent-supervisor',
  String text = 'Synthetic objective',
}) => JournalEntry(
  eventId: 'event-$session-$sequence',
  sequence: sequence,
  sessionId: session,
  threadId: 'thread-$session',
  epoch: epoch,
  kind: 'agent.command',
  origin: origin,
  payload: {'op': 'submit', 'objective': text},
);

JournalEntry toolEntry(
  int sequence, {
  String origin = 'agent-supervisor',
  int epoch = 1,
  String taskRef = 'task-a',
  Map<String, Object?>? payload,
}) => JournalEntry(
  eventId: 'tool-event-$sequence',
  sequence: sequence,
  sessionId: 'a',
  threadId: 'thread-a',
  epoch: epoch,
  kind: 'agent.tool',
  origin: origin,
  taskRefs: [taskRef],
  payload:
      payload ??
      {
        'task_id': 'task-a',
        'tool': 'shell',
        'ok': true,
        'evidence': {
          'summary': 'shell exit=0: synthetic command',
          'output': '<literal output>',
          'exit_code': 0,
          'objective_revision': 2,
        },
      },
);

class WorkspaceApi extends KinaiyaClient {
  WorkspaceApi()
    : super(
        sessionBase: 'http://127.0.0.1',
        agentBase: 'http://127.0.0.1',
        converseBase: 'http://127.0.0.1',
        ownerToken: 'synthetic',
      );
  final sessions = <String, SessionInfo>{
    'a': SessionInfo.fromJson(sessionJson('a')),
    'b': SessionInfo.fromJson(sessionJson('b')),
    'old': SessionInfo.fromJson(sessionJson('old', harness: null)),
  };
  final history = <String, List<JournalEntry>>{};
  Future<SessionInfo> Function(String)? getHook;
  Future<List<JournalEntry>> Function(String)? historyHook;
  Future<AgentSessionPage> Function(String, String?)? pageHook;
  Future<AgentHarnessPage> Function()? catalogHook;
  Future<SessionInfo> Function()? createHook;
  int creates = 0;
  final pageReads = <String>[];
  @override
  Future<Map<String, Object?>> agentCapabilities() async => {};
  @override
  Future<Map<String, Object?>> agentModelSelection(String? harness) async => {
    'schema_version': '1.0',
    'harness_version': harness ?? 'seed-v1',
    'provider': {'provider': 'fixture', 'model': 'synthetic', 'local': true},
    'pin': null,
  };
  @override
  Future<AgentHarnessPage> agentHarnesses({String? after}) async =>
      catalogHook == null ? catalog() : catalogHook!();
  @override
  Future<AgentSessionPage> agentSessions({
    required String harnessVersion,
    String? after,
  }) async {
    pageReads.add(harnessVersion);
    return pageHook == null
        ? page(
            sessions.values.where(
              (x) => (x.harnessVersion ?? 'unassociated') == harnessVersion,
            ),
          )
        : pageHook!(harnessVersion, after);
  }

  @override
  Future<SessionInfo> agentWorkspaceSession(String id) async =>
      getHook == null ? sessions[id]! : getHook!(id);
  @override
  Future<List<JournalEntry>> agentWorkspaceHistory({
    required String sessionId,
    int after = 0,
  }) async => historyHook == null
      ? (history[sessionId] ?? [])
            .where((x) => x.sequence > after)
            .take(200)
            .toList()
      : historyHook!(sessionId);
  @override
  Future<SessionInfo> createAgentSession(
    String harnessVersion, {
    String name = '',
  }) async {
    creates++;
    if (createHook != null) return createHook!();
    final item = SessionInfo.fromJson(
      sessionJson('created', harness: harnessVersion),
    );
    sessions[item.sessionId] = item;
    return item;
  }
}

AppState wired(WorkspaceApi api) =>
    AppState()..connect(ConnectionProfile.defaults(), injected: api);

void main() {
  test(
    'catalog and harness inspection only read; native eligibility gates creation',
    () async {
      final api = WorkspaceApi();
      final state = wired(api);
      addTearDown(state.dispose);
      await state.refreshAgentHarnesses();
      expect(state.selectedAgentHarness, 'seed-v1');
      expect(state.canCreateAgentSession, isTrue);
      await state.selectAgentHarness('unapproved');
      expect(state.selectedAgentHarness, 'unapproved');
      expect(state.canCreateAgentSession, isFalse);
      await state.createSelectedAgentSession();
      expect(api.creates, 0);
      expect(state.agentSession, isNull);
      await state.selectAgentHarness('unassociated');
      expect(state.agentWorkspaceSessions.single.sessionId, 'old');
      expect(state.canCreateAgentSession, isFalse);
      await state.selectAgentSession('old');
      expect(state.agentSession!.harnessVersion, isNull);
    },
  );
  test(
    'session drafts and authoritative journal histories restore independently',
    () async {
      final api = WorkspaceApi()
        ..history['a'] = [entry('a', 1)]
        ..history['b'] = [entry('b', 2, text: 'Other objective')];
      final state = wired(api);
      addTearDown(state.dispose);
      await state.selectAgentSession('a');
      state.setDraft(AppView.agent, 'Draft A');
      await state.selectAgentSession('b');
      expect(state.agentDraft, '');
      expect(state.agentHistory.single.text, 'Other objective');
      state.setDraft(AppView.agent, 'Draft B');
      await state.selectAgentSession('a');
      expect(state.agentDraft, 'Draft A');
      expect(state.agentHistory.single.text, 'Synthetic objective');
      await state.selectAgentSession('b');
      expect(state.agentDraft, 'Draft B');
      expect(state.agentHistory.length, 1);
    },
  );
  test('projects only scoped, attributable recorded tool activity', () async {
    final api = WorkspaceApi()
      ..history['a'] = [
        entry('a', 1),
        toolEntry(2),
        toolEntry(3, origin: 'owner'),
        toolEntry(4, epoch: 2),
        toolEntry(5, taskRef: 'different-task'),
        toolEntry(
          6,
          payload: {
            'task_id': 'task-a',
            'tool': 'shell',
            'ok': false,
            'admission_only': true,
            'effect_state': 'not_established',
          },
        ),
      ];
    final state = wired(api);
    addTearDown(state.dispose);
    await state.selectAgentSession('a');
    expect(state.agentHistory.map((message) => message.eventId), [
      'event-a-1',
      'tool-event-2',
      'tool-event-6',
    ]);
    final activity = state.agentHistory[1].toolActivity!;
    expect(activity.tool, 'shell');
    expect(activity.taskId, 'task-a');
    expect(activity.revision, 2);
    expect(activity.summary, 'shell exit=0: synthetic command');
    expect(activity.output, '<literal output>');
    expect(activity.status, 'Recorded success');
    expect(
      state.agentHistory[2].toolActivity!.status,
      'Result not established',
    );
    await state.refreshAgentHistory();
    expect(state.agentHistory.length, 3);
    await state.selectAgentSession('b');
    expect(state.agentHistory, isEmpty);
    await state.selectAgentSession('a');
    expect(state.agentHistory[1].toolActivity!.output, '<literal output>');
  });
  test(
    'recorded activity stays ordered across bounded history pages',
    () async {
      final api = WorkspaceApi()
        ..history['a'] = [
          entry('a', 1),
          for (var i = 2; i <= 202; i++) toolEntry(i),
        ];
      final state = wired(api);
      addTearDown(state.dispose);
      await state.selectAgentSession('a');
      expect(state.agentHistory.length, 200);
      expect(state.agentHistoryHasMore, isTrue);
      await state.refreshAgentHistory();
      expect(state.agentHistory.length, 202);
      expect(state.agentHistory.last.eventId, 'tool-event-202');
      expect(state.agentHistoryHasMore, isFalse);
      await state.refreshAgentHistory();
      expect(state.agentHistory.length, 202);
    },
  );
  test(
    'tool output is bounded and unestablished results stay explicit',
    () async {
      final api = WorkspaceApi()
        ..history['a'] = [
          toolEntry(
            1,
            payload: {
              'task_id': 'task-a',
              'tool': 'shell',
              'evidence': {
                'output': List.filled(8100, 'x').join(),
                'objective_revision': 1,
              },
            },
          ),
        ];
      final state = wired(api);
      addTearDown(state.dispose);
      await state.selectAgentSession('a');
      final activity = state.agentHistory.single.toolActivity!;
      expect(activity.status, 'Result not established');
      expect(activity.output, endsWith('…[display truncated]'));
      expect(activity.output!.length, lessThan(8100));
    },
  );
  test(
    'pending identity restore preserves a newly revised draft over target snapshot',
    () async {
      final api = WorkspaceApi();
      final state = wired(api);
      addTearDown(state.dispose);
      await state.selectAgentSession('b');
      state.setDraft(AppView.agent, 'Saved B draft');
      await state.selectAgentSession('a');
      final gate = Completer<SessionInfo>();
      api.getHook = (id) async => id == 'b' ? gate.future : api.sessions[id]!;
      final pending = state.selectAgentSession('b');
      state.setDraft(AppView.agent, 'New draft while B lookup is pending');
      gate.complete(api.sessions['b']);
      await pending;
      expect(state.agentDraft, 'New draft while B lookup is pending');
      expect(state.agentSession!.sessionId, 'b');
      expect(state.agentIdentitySelecting, isFalse);
    },
  );
  test('late session identity cannot replace a newer selection', () async {
    final gate = Completer<SessionInfo>();
    final api = WorkspaceApi();
    api.getHook = (id) async => id == 'a' ? gate.future : api.sessions[id]!;
    final state = wired(api);
    addTearDown(state.dispose);
    final pending = state.selectAgentSession('a');
    await state.selectAgentSession('b');
    gate.complete(api.sessions['a']);
    await pending;
    expect(state.agentSession!.sessionId, 'b');
    expect(state.agentHistoryLoading, isFalse);
  });
  test('late journal read cannot cross selection or connection', () async {
    final gate = Completer<List<JournalEntry>>();
    final api = WorkspaceApi();
    api.historyHook = (id) async => id == 'a' ? gate.future : [entry('b', 2)];
    final state = wired(api);
    addTearDown(state.dispose);
    final pending = state.selectAgentSession('a');
    await Future<void>.delayed(Duration.zero);
    await state.selectAgentSession('b');
    gate.complete([entry('a', 1)]);
    await pending;
    expect(state.agentHistory.single.eventId, 'event-b-2');
  });
  test(
    'native reset during journal await refuses old epoch publication',
    () async {
      final gate = Completer<List<JournalEntry>>();
      final api = WorkspaceApi();
      api.historyHook = (_) => gate.future;
      final state = wired(api);
      addTearDown(state.dispose);
      final pending = state.selectAgentSession('a');
      await Future<void>.delayed(Duration.zero);
      api.sessions['a'] = SessionInfo.fromJson(sessionJson('a', epoch: 2));
      gate.complete([entry('a', 1)]);
      await pending;
      expect(state.agentHistory, isEmpty);
      expect(state.agentHistoryError, isNotNull);
    },
  );
  test(
    'wrong identity, foreign journal rows and origins fail or stay literal',
    () async {
      final api = WorkspaceApi()
        ..history['a'] = [
          entry('a', 1, origin: 'owner'),
          entry('a', 2, text: '<literal> /stop'),
        ];
      final state = wired(api);
      addTearDown(state.dispose);
      await state.selectAgentSession('a');
      expect(state.agentHistory.single.text, '<literal> /stop');
      api.history['b'] = [entry('a', 3)];
      await state.selectAgentSession('b');
      expect(state.agentHistory, isEmpty);
      expect(state.agentHistoryError, isNotNull);
    },
  );
  test(
    'session page and catalog failures are explicit; stale pages do not mix harnesses',
    () async {
      final gate = Completer<AgentSessionPage>();
      final api = WorkspaceApi();
      final state = wired(api);
      addTearDown(state.dispose);
      await state.refreshAgentHarnesses();
      api.pageHook = (harness, _) async =>
          harness == 'seed-v1' ? gate.future : page([]);
      final pending = state.refreshAgentSessions();
      await state.selectAgentHarness('unapproved');
      gate.complete(page([api.sessions['a']!]));
      await pending;
      expect(state.agentWorkspaceSessions, isEmpty);
      api.catalogHook = () async => throw StateError('synthetic failure');
      await state.refreshAgentHarnesses();
      expect(state.agentHarnessError, isNotNull);
      expect(state.canCreateAgentSession, isFalse);
    },
  );
  test(
    'uncertain creation is retained across reconnect and never automatically replayed',
    () async {
      final api = WorkspaceApi()
        ..createHook = () async => throw TimeoutException('synthetic');
      final state = wired(api);
      addTearDown(state.dispose);
      await state.refreshAgentHarnesses();
      await state.createSelectedAgentSession();
      expect(api.creates, 1);
      expect(state.agentCreationUncertain, isTrue);
      state.disconnect();
      final next = WorkspaceApi();
      state.connect(ConnectionProfile.defaults(), injected: next);
      await state.refreshAgentHarnesses();
      await state.createSelectedAgentSession();
      expect(next.creates, 0);
      state.acknowledgeAgentCreationUncertainty();
      await state.createSelectedAgentSession();
      expect(next.creates, 1);
      expect(state.agentSession!.harnessVersion, 'seed-v1');
    },
  );
  test(
    'creation receipt cannot redirect work after the selected session changes',
    () async {
      final gate = Completer<SessionInfo>();
      final api = WorkspaceApi()..createHook = () => gate.future;
      final state = wired(api);
      addTearDown(state.dispose);
      await state.refreshAgentHarnesses();
      final pending = state.ensureSession('agent', name: 'Synthetic work');
      await state.selectAgentSession('b');
      state.setDraft(AppView.agent, 'New destination draft');
      gate.complete(SessionInfo.fromJson(sessionJson('created')));
      await expectLater(pending, throwsStateError);
      expect(state.agentSession!.sessionId, 'b');
      expect(state.agentDraft, 'New destination draft');
    },
  );
  test(
    'creation uncertainty cannot be cleared before a fresh same-harness read',
    () async {
      final api = WorkspaceApi()
        ..createHook = () async => throw TimeoutException('synthetic');
      final state = wired(api);
      addTearDown(state.dispose);
      await state.refreshAgentHarnesses();
      await state.createSelectedAgentSession();
      state.acknowledgeAgentCreationUncertainty();
      expect(state.agentCreationUncertain, isTrue);
      expect(state.canAcknowledgeAgentCreation, isFalse);
      await state.selectAgentHarness('unapproved');
      state.acknowledgeAgentCreationUncertainty();
      expect(state.agentCreationUncertain, isTrue);
      await state.selectAgentHarness('seed-v1');
      expect(state.canAcknowledgeAgentCreation, isTrue);
      state.acknowledgeAgentCreationUncertainty();
      expect(state.agentCreationUncertain, isFalse);
    },
  );
  test(
    'selection preserves global approvals and filters only chat task cards',
    () async {
      final api = WorkspaceApi();
      final state = wired(api);
      addTearDown(state.dispose);
      final approval = ApprovalRequest(
        approvalId: 'global',
        taskId: 'other',
        tool: 'write_file',
        actionDigest: 'synthetic',
        objectiveRevision: 1,
        sessionId: 'b',
        threadId: 'thread-b',
        epoch: 1,
      );
      state.recordApprovals([approval]);
      state.setTasks([
        AgentTask(
          taskId: 'other',
          sessionId: 'b',
          threadId: 'thread-b',
          epoch: 1,
          objective: 'Other session work',
          objectiveRevision: 1,
          state: 'awaiting_approval',
          idempotencyKey: 'synthetic',
        ),
      ]);
      await state.selectAgentSession('a');
      expect(state.selectedAgentTasks, isEmpty);
      expect(state.approvals.single.approvalId, 'global');
      await state.selectAgentSession('b');
      expect(state.selectedAgentTasks.single.taskId, 'other');
      expect(state.approvals.single.approvalId, 'global');
    },
  );
  test(
    'unknown session association stays unknown and approvals remain global',
    () async {
      final api = WorkspaceApi();
      final state = wired(api);
      addTearDown(state.dispose);
      await state.selectAgentSession('old');
      await state.refreshAgentHarnesses();
      expect(state.selectedAgentHarness, 'unassociated');
      expect(state.agentSession!.harnessVersion, isNull);
      expect(state.selectedAgentTasks, isEmpty);
    },
  );
}
