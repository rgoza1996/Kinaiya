/// Reconnect repair regressions: registry reload without a tab switch,
/// stale-read isolation, stale side-effect isolation, and backend scoping.
///
/// All transports are deterministic synthetic fakes; no inference runs.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'client_test.dart' show mockClient;
import 'concept_layout_test.dart' show openConverseChat, closeConverseChat;

ConnectionProfile profileA() => ConnectionProfile(
  sessionUrl: 'http://127.0.0.1:8760',
  agentUrl: 'http://127.0.0.1:8761',
  converseUrl: 'http://127.0.0.1:8762',
  ownerToken: 'token-a',
);

ConnectionProfile profileB() => ConnectionProfile(
  sessionUrl: 'http://127.0.0.1:8770',
  agentUrl: 'http://127.0.0.1:8771',
  converseUrl: 'http://127.0.0.1:8772',
  ownerToken: 'token-b',
);

/// Synthetic client with hookable reads; never touches the network.
class FakeAgentClient extends KinaiyaClient {
  FakeAgentClient({
    this.commandsHook,
    this.tasksHook,
    this.createTaskHook,
    this.sessionsHook,
    this.createSessionHook,
    this.recentHook,
    this.approvalsHook,
    this.healthHook,
  }) : super(
         sessionBase: 'http://127.0.0.1:8760',
         agentBase: 'http://127.0.0.1:8761',
         converseBase: 'http://127.0.0.1:8762',
         ownerToken: 'synthetic',
       );

  Future<List<CommandDef>?> Function()? commandsHook;
  Future<List<AgentTask>> Function()? tasksHook;
  Future<AgentTask> Function()? createTaskHook;
  Future<List<SessionInfo>> Function()? sessionsHook;
  Future<SessionInfo> Function()? createSessionHook;
  Future<List<JournalEntry>> Function()? recentHook;
  Future<PendingApprovalPage?> Function(String? after)? approvalsHook;
  Future<Map<String, Object?>> Function()? healthHook;

  @override
  Future<Map<String, Object?>> converseHealth() =>
      healthHook?.call() ?? Future.value({'enabled': true});

  @override
  Future<List<CommandDef>?> fetchCommands() =>
      commandsHook?.call() ?? Future.value(null);

  @override
  Future<List<AgentTask>> listTasks() =>
      tasksHook?.call() ?? Future.value(const []);

  @override
  Future<AgentTask> createTask({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String objective,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) =>
      createTaskHook?.call() ??
      Future.value(
        AgentTask(
          taskId: 'task-synthetic',
          sessionId: sessionId,
          threadId: threadId,
          epoch: epoch,
          objective: objective,
          objectiveRevision: 1,
          state: 'accepted',
          idempotencyKey: idempotencyKey,
        ),
      );

  @override
  Future<List<SessionInfo>> listSessions() =>
      sessionsHook?.call() ?? Future.value(const []);

  @override
  Future<SessionInfo> createSession(String channel, {String name = ''}) =>
      createSessionHook?.call() ??
      Future.value(
        SessionInfo(
          sessionId: 's-$channel',
          threadId: 't-$channel',
          channel: channel,
          epoch: 1,
          name: name,
        ),
      );

  @override
  Future<List<JournalEntry>> recent({
    required String sessionId,
    int limit = 100,
  }) => recentHook?.call() ?? Future.value(const []);

  @override
  Future<PendingApprovalPage?> pendingApprovals({String? after}) =>
      approvalsHook?.call(after) ?? Future.value(null);
}

void main() {
  testWidgets('an old approval scan cannot cross a session epoch reset', (
    tester,
  ) async {
    final gate = Completer<List<JournalEntry>>();
    final api = FakeAgentClient(
      recentHook: () => gate.future,
      tasksHook: () async => [
        AgentTask(
          taskId: 't1',
          sessionId: 's1',
          threadId: 'th1',
          epoch: 1,
          objective: 'public synthetic approval',
          objectiveRevision: 1,
          state: 'awaiting_approval',
          idempotencyKey: 'approval-case',
        ),
      ],
    );
    final state = AppState()..connect(profileA(), injected: api);
    state.agentSession = SessionInfo(
      sessionId: 's1',
      threadId: 'th1',
      channel: 'agent',
      epoch: 1,
    );
    state.setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(seconds: 4));
    state.agentSession = SessionInfo(
      sessionId: 's1',
      threadId: 'th1',
      channel: 'agent',
      epoch: 2,
    );
    gate.complete([
      JournalEntry(
        eventId: 'a1',
        sequence: 1,
        sessionId: 's1',
        threadId: 'th1',
        epoch: 1,
        kind: 'approval.requested',
        origin: 'agent-supervisor',
        payload: {
          'approval_id': 'a1',
          'task_id': 't1',
          'tool': 'write_file',
          'action_digest': 'synthetic-digest',
          'objective_revision': 1,
          'expires_at': '2099-01-01T00:00:00Z',
          'arguments': <String, Object?>{},
        },
      ),
    ]);
    await tester.pump(const Duration(milliseconds: 200));
    expect(state.approvals, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  for (final view in AppView.values) {
    testWidgets('changing backend clears the visible $view draft', (
      tester,
    ) async {
      final state = AppState()
        ..connect(profileA(), injected: FakeAgentClient());
      state.setView(view);
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 200));
      if (view == AppView.character) await openConverseChat(tester);
      await tester.enterText(
        find.byType(TextField),
        'synthetic old-context draft',
      );
      await tester.pump();
      state.connect(profileB(), injected: FakeAgentClient());
      await tester.pump(const Duration(milliseconds: 200));
      if (view == AppView.character) await openConverseChat(tester);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        isEmpty,
      );
      expect(state.agentDraft, isEmpty);
      expect(state.converseDraft, isEmpty);
      if (view == AppView.character) await closeConverseChat(tester);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    });
  }

  testWidgets('old Converse health cannot override a new connection', (
    tester,
  ) async {
    final gate = Completer<Map<String, Object?>>();
    final state = AppState()
      ..connect(
        profileA(),
        injected: FakeAgentClient(healthHook: () => gate.future),
      );
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));
    state.connect(
      profileB(),
      injected: FakeAgentClient(healthHook: () async => {'enabled': false}),
    );
    await tester.pump(const Duration(milliseconds: 200));
    expect(state.converseEnabled, isFalse);
    gate.complete({'enabled': true});
    await tester.pump(const Duration(milliseconds: 200));
    expect(state.converseEnabled, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  testWidgets('hung registry reads time out within the attempt bound', (
    tester,
  ) async {
    final gate = Completer<List<CommandDef>?>();
    var calls = 0;
    final state = AppState()
      ..connect(
        profileA(),
        injected: FakeAgentClient(
          commandsHook: () {
            calls++;
            return gate.future;
          },
        ),
      );
    final pending = state.refreshCommands(maxAttempts: 2);
    await tester.pump(const Duration(seconds: 3));
    await tester.pump(const Duration(milliseconds: 50));
    await tester.pump(const Duration(seconds: 3));
    await pending;
    expect(calls, 2);
    expect(state.commandsAvailable, isFalse);
    gate.complete([CommandDef(name: 'late', description: 'timed out')]);
    await tester.pump();
    expect(state.commandsAvailable, isFalse);
    state.dispose();
  });

  testWidgets('agent chat mounted disconnected loads registry after connect', (
    tester,
  ) async {
    final state = AppState()..setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));
    expect(state.commandsAvailable, isFalse);
    expect(
      find.text('Command registry unavailable; plain instructions still work.'),
      findsOneWidget,
    );

    state.connect(profileA(), injected: mockClient(commandsMissing: false));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(milliseconds: 200));
    expect(state.commandsAvailable, isTrue);
    expect(state.commands!.single.name, 'status');
    expect(
      find.text('Command registry unavailable; plain instructions still work.'),
      findsNothing,
    );
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  test(
    'delayed old registry response cannot overwrite the new connection',
    () async {
      final oldGate = Completer<List<CommandDef>?>();
      final oldClient = FakeAgentClient(commandsHook: () => oldGate.future);
      final state = AppState();
      state.connect(profileA(), injected: oldClient);
      final staleLoad = state.refreshCommands();
      // Same-backend reconnect invalidates, then the new registry loads.
      final newClient = FakeAgentClient(
        commandsHook: () async => [
          CommandDef(
            name: 'status',
            description: 'new backend registry',
            executionType: 'runtime_control',
          ),
        ],
      );
      state.connect(profileA(), injected: newClient);
      expect(state.commandsAvailable, isFalse);
      await state.refreshCommands();
      expect(state.commands!.single.description, 'new backend registry');

      // The old backend answers late; it must be dropped.
      oldGate.complete([
        CommandDef(name: 'old', description: 'stale registry'),
      ]);
      await staleLoad;
      await Future<void>.delayed(Duration.zero);
      expect(state.commands!.single.name, 'status');
      state.dispose();
    },
  );

  testWidgets('delayed old task list cannot overwrite the new connection', (
    tester,
  ) async {
    AgentTask task(String id, String objective) => AgentTask(
      taskId: id,
      sessionId: 's',
      threadId: 't',
      epoch: 1,
      objective: objective,
      objectiveRevision: 1,
      state: 'accepted',
      idempotencyKey: 'k-$id',
    );
    final oldGate = Completer<List<AgentTask>>();
    final slowClient = FakeAgentClient(tasksHook: () => oldGate.future);
    final state = AppState();
    state.connect(profileA(), injected: slowClient);
    state.agentSession = SessionInfo(
      sessionId: 's-agent',
      threadId: 't-agent',
      channel: 'agent',
      epoch: 1,
      name: 'work',
    );
    state.setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));
    // Start the real periodic task poll against the old connection.
    await tester.pump(const Duration(seconds: 4));
    final fastClient = FakeAgentClient(
      tasksHook: () async => [task('new-1', 'new backend work')],
    );
    state.connect(profileA(), injected: fastClient);
    await tester.pump(const Duration(milliseconds: 300));
    expect(state.tasks.map((t) => t.taskId), contains('new-1'));
    // The old backend answers late; it must not replace the new list.
    oldGate.complete([task('old-1', 'stale work')]);
    await tester.pump(const Duration(milliseconds: 300));
    expect(state.tasks.map((t) => t.taskId), contains('new-1'));
    expect(state.tasks.map((t) => t.taskId), isNot(contains('old-1')));
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  test(
    'stale session acquisition cannot install into a new connection',
    () async {
      final listGate = Completer<List<SessionInfo>>();
      final slowClient = FakeAgentClient(sessionsHook: () => listGate.future);
      final state = AppState();
      state.connect(profileA(), injected: slowClient);
      final pending = state.ensureSession('agent', name: 'work');
      final fastClient = FakeAgentClient();
      state.connect(profileA(), injected: fastClient);
      listGate.complete([
        SessionInfo(
          sessionId: 's-stale',
          threadId: 't-stale',
          channel: 'agent',
          epoch: 1,
          name: 'work',
        ),
      ]);
      await expectLater(pending, throwsStateError);
      expect(state.agentSession, isNull);
      final fresh = await state.ensureSession('agent', name: 'work');
      expect(fresh.sessionId, 's-agent');
      expect(state.agentSession!.sessionId, 's-agent');
      state.dispose();
    },
  );

  testWidgets('stale send completion cannot clear a new draft or history', (
    tester,
  ) async {
    final createGate = Completer<AgentTask>();
    final slowClient = FakeAgentClient(createTaskHook: () => createGate.future);
    final state = AppState();
    state.connect(profileA(), injected: slowClient);
    state.agentSession = SessionInfo(
      sessionId: 's-agent',
      threadId: 't-agent',
      channel: 'agent',
      epoch: 1,
      name: 'work',
    );
    state.setView(AppView.agent);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));

    await tester.enterText(find.byType(TextField), 'first work');
    await tester.pump();
    await tester.tap(find.text('Send task'));
    await tester.pump();
    expect(state.busyAgent, isTrue);

    // Same-backend reconnect starts idle; the operator drafts new work.
    final fastClient = FakeAgentClient();
    state.connect(profileA(), injected: fastClient);
    expect(state.busyAgent, isFalse);
    await tester.pump(const Duration(milliseconds: 200));
    await tester.enterText(find.byType(TextField), 'fresh draft');
    await tester.pump();

    // The old backend answers late. It was already sent (cannot be
    // recalled) but must not mutate the new connection.
    createGate.complete(
      AgentTask(
        taskId: 'task-stale',
        sessionId: 's-agent',
        threadId: 't-agent',
        epoch: 1,
        objective: 'first work',
        objectiveRevision: 1,
        state: 'accepted',
        idempotencyKey: 'k-stale',
      ),
    );
    await tester.pump(const Duration(milliseconds: 300));
    expect(state.agentHistory, isEmpty);
    expect(state.agentDraft, 'fresh draft');
    expect(state.busyAgent, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

  test(
    'same backend preserves drafts/history; other backends stay isolated',
    () async {
      final state = AppState();
      state.connect(profileA(), injected: mockClient());
      state.setDraft(AppView.character, 'hello kinaiya');
      state.setDraft(AppView.agent, 'build proof');
      state.appendHistory(AppView.character, ChatMsg(role: 'user', text: 'hi'));
      state.appendHistory(AppView.agent, ChatMsg(role: 'user', text: 'work'));
      await state.ensureSession('converse', name: 'Kinaiya');
      state.agentSession = SessionInfo(
        sessionId: 's-agent',
        threadId: 't-agent',
        channel: 'agent',
        epoch: 1,
        name: 'work',
      );
      state.setTasks([
        AgentTask(
          taskId: 't1',
          sessionId: 's-agent',
          threadId: 't-agent',
          epoch: 1,
          objective: 'work',
          objectiveRevision: 1,
          state: 'accepted',
          idempotencyKey: 'k1',
        ),
      ]);

      state.disconnect();
      state.connect(profileA(), injected: mockClient());
      expect(state.converseDraft, 'hello kinaiya');
      expect(state.agentDraft, 'build proof');
      expect(state.converseHistory, isNotEmpty);
      expect(state.agentHistory, isNotEmpty);
      expect(state.tasks.single.taskId, 't1');

      state.connect(profileB(), injected: mockClient());
      expect(state.converseSession, isNull);
      expect(state.agentSession, isNull);
      expect(state.tasks, isEmpty);
      expect(state.approvals, isEmpty);
      expect(state.converseHistory, isEmpty);
      expect(state.agentHistory, isEmpty);
      expect(state.converseDraft, isEmpty);
      expect(state.agentDraft, isEmpty);
      expect(state.commandsAvailable, isFalse);

      // Same URLs with a different owner credential are also isolated,
      // compared in memory only (never logged or serialized here).
      state.setDraft(AppView.agent, 'backend-b draft');
      state.appendHistory(AppView.agent, ChatMsg(role: 'user', text: 'b work'));
      final rotated = ConnectionProfile(
        sessionUrl: profileB().sessionUrl,
        agentUrl: profileB().agentUrl,
        converseUrl: profileB().converseUrl,
        ownerToken: 'token-b-rotated',
      );
      state.connect(rotated, injected: mockClient());
      expect(state.agentDraft, isEmpty);
      expect(state.agentHistory, isEmpty);
      expect('$state', isNot(contains('token-b-rotated')));
      state.dispose();
    },
  );

  test('registry retries are bounded and never execute a command', () async {
    var calls = 0;
    final flaky = FakeAgentClient(
      commandsHook: () async {
        calls++;
        if (calls < 3) throw StateError('synthetic transport cut');
        return [
          CommandDef(
            name: 'status',
            description: 'recovered',
            executionType: 'runtime_control',
          ),
        ];
      },
    );
    final state = AppState();
    state.connect(profileA(), injected: flaky);
    await state.refreshCommands(maxAttempts: 3);
    expect(calls, 3);
    expect(state.commandsAvailable, isTrue);

    calls = 0;
    final down = FakeAgentClient(
      commandsHook: () async {
        calls++;
        throw StateError('synthetic transport cut');
      },
    );
    state.connect(profileA(), injected: down);
    await state.refreshCommands(maxAttempts: 3);
    expect(calls, 3);
    expect(state.commandsAvailable, isFalse);
    expect(state.commands, isNull);
    state.dispose();
  });
}
