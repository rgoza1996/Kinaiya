import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/api/agent_workspace.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';

import 'agent_workspace_state_test.dart'
    show WorkspaceApi, wired, entry, catalog, harnessJson;

class WorkspaceUiApi extends WorkspaceApi {
  List<AgentTask> fixtures = [];
  int workRequests = 0, commandRequests = 0;
  List<CommandDef> commandRows = [];
  Completer<AgentTask>? submitted;
  @override
  Future<List<CommandDef>?> fetchCommands() async => commandRows;
  @override
  Future<List<AgentTask>> listTasks() async => fixtures;
  @override
  Future<PendingApprovalPage?> pendingApprovals({String? after}) async =>
      PendingApprovalPage([], null);
  @override
  Future<Map<String, Object?>?> executeCommand({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String input,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    commandRequests++;
    return {'status': 'ok'};
  }

  @override
  Future<AgentTask> createTask({
    required String sessionId,
    required String threadId,
    required int epoch,
    required String objective,
    required String idempotencyKey,
    ContentScope contentScope = ContentScope.localOnly,
  }) async {
    workRequests++;
    return submitted == null
        ? AgentTask(
            taskId: 'synthetic-task',
            sessionId: sessionId,
            threadId: threadId,
            epoch: epoch,
            objective: objective,
            objectiveRevision: 1,
            state: 'accepted',
            idempotencyKey: idempotencyKey,
          )
        : submitted!.future;
  }
}

// Exercises real reader admission; transport bytes and identities are synthetic.
class HeldCatalogClient extends http.BaseClient {
  final response = Completer<http.StreamedResponse>();
  int closes = 0;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    expect(request.url.path, '/v1/agent/harnesses');
    expect(request.followRedirects, isFalse);
    return response.future;
  }

  @override
  void close() => closes++;
  void complete({String name = 'Synthetic seed', int status = 200}) {
    response.complete(
      http.StreamedResponse(
        Stream.value(
          utf8.encode(
            jsonEncode({
              'schema_version': '1.0',
              'configured': false,
              'items': [
                {...harnessJson('seed-v1', active: true), 'name': name},
              ],
              'next_cursor': null,
            }),
          ),
        ),
        status,
        headers: {'content-type': 'application/json'},
      ),
    );
  }
}

class TransportWorkspaceUiApi extends WorkspaceUiApi {
  TransportWorkspaceUiApi() {
    transport = KinaiyaClient(
      sessionBase: 'http://127.0.0.1',
      agentBase: 'http://127.0.0.1',
      converseBase: 'http://127.0.0.1',
      ownerToken: 'synthetic',
      agentWorkspaceClientFactory: () {
        final client = HeldCatalogClient();
        clients.add(client);
        return client;
      },
    );
  }
  final clients = <HeldCatalogClient>[];
  late final KinaiyaClient transport;
  @override
  Future<AgentHarnessPage> agentHarnesses({String? after}) =>
      transport.agentHarnesses(after: after);
  @override
  void close() {
    transport.close();
    super.close();
  }
}

Widget host(AppState state) => MaterialApp(
  home: Scaffold(
    body: ListenableBuilder(
      listenable: state,
      builder: (_, _) => AgentChat(state: state),
    ),
  ),
);
Future<void> settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
}

Future<void> close(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(const SizedBox.shrink());
  state.dispose();
}

void main() {
  testWidgets(
    'connection listener and simultaneous refresh share one real reader admission',
    (tester) async {
      final state = AppState();
      final api = TransportWorkspaceUiApi();
      // Connect immediately before AgentChat's own first-frame callback, using
      // its actual synchronous connection/auth listener and the real reader.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        state.connect(ConnectionProfile.defaults(), injected: api);
      });
      await tester.pumpWidget(host(state));
      await tester.pump();
      expect(api.clients.length, 1);
      final refreshes = List.generate(5, (_) => state.refreshAgentHarnesses());
      expect(api.clients.length, 1);
      expect(state.agentHarnessError, isNull);
      api.clients.single.complete();
      await Future.wait(refreshes);
      await settle(tester);
      expect(state.agentHarnesses.single.name, 'Synthetic seed');
      expect(state.agentHarnessError, isNull);
      expect(state.agentHarnessesLoading, isFalse);
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );

  testWidgets(
    'manual retry works after failure and reconnect cannot join old catalog',
    (tester) async {
      final old = TransportWorkspaceUiApi();
      final state = wired(old);
      await tester.pumpWidget(host(state));
      await tester.pump();
      expect(old.clients.length, 1);
      final next = TransportWorkspaceUiApi();
      state.connect(ConnectionProfile.defaults(), injected: next);
      await tester.pump();
      expect(next.clients.length, 1);
      old.clients.single.complete(name: 'Old catalog');
      await tester.pump();
      expect(state.agentHarnessesLoading, isTrue);
      next.clients.single.complete(status: 503);
      await settle(tester);
      expect(state.agentHarnessError, isNotNull);
      final retry = state.refreshAgentHarnesses();
      expect(next.clients.length, 2);
      next.clients.last.complete(name: 'New catalog');
      await retry;
      await settle(tester);
      expect(state.agentHarnesses.single.name, 'New catalog');
      expect(state.agentHarnessError, isNull);
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );

  testWidgets(
    'pending selected identity blocks queued inference and deterministic slash commands',
    (tester) async {
      final api = WorkspaceUiApi()
        ..commandRows = [
          CommandDef(
            name: 'skill',
            description: 'Synthetic skill',
            executionType: 'skill_invocation',
          ),
          CommandDef(
            name: 'status',
            description: 'Synthetic status',
            executionType: 'runtime_control',
          ),
        ];
      final state = wired(api);
      await state.selectAgentSession('b');
      state.setDraft(AppView.agent, 'Saved B');
      await state.selectAgentSession('a');
      await tester.pumpWidget(host(state));
      await settle(tester);
      final gate = Completer<SessionInfo>();
      api.getHook = (id) async => id == 'b' ? gate.future : api.sessions[id]!;
      final pending = state.selectAgentSession('b');
      await tester.pump();
      final input = find.byKey(const ValueKey('agent-input'));
      var field = tester.widget<TextField>(input);
      expect(field.enabled, isFalse);
      for (final command in ['/skill synthetic', '/status']) {
        field.controller!.text = command;
        field.onSubmitted!(command);
        await tester.pump();
      }
      expect(api.commandRequests, 0);
      expect(api.workRequests, 0);
      expect(api.creates, 0);
      state.setDraft(AppView.agent, 'New pending B draft');
      gate.complete(api.sessions['b']);
      await pending;
      await settle(tester);
      field = tester.widget<TextField>(input);
      expect(field.controller!.text, 'New pending B draft');
      expect(state.agentSession!.sessionId, 'b');
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );
  testWidgets(
    'initial harness discovery never acquires an arbitrary legacy destination',
    (tester) async {
      final api = WorkspaceUiApi();
      final gate = Completer<AgentHarnessPage>();
      api.catalogHook = () => gate.future;
      final state = wired(api);
      await tester.pumpWidget(host(state));
      await settle(tester);
      final input = find.byKey(const ValueKey('agent-input'));
      await tester.enterText(input, 'First draft');
      tester.widget<TextField>(input).onSubmitted!('First draft');
      await tester.pump();
      expect(api.creates, 0);
      expect(api.workRequests, 0);
      expect(state.agentSession, isNull);
      gate.complete(catalog());
      await settle(tester);
      expect(api.creates, 0);
      expect(state.agentDraft, 'First draft');
      await close(tester, state);
    },
  );

  testWidgets(
    'real harness sessions replace task sidebar; selection restores drafts and journal',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(1600, 1000);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      final api = WorkspaceUiApi()
        ..history['a'] = [entry('a', 1, text: 'Recorded objective A')];
      final state = wired(api);
      await tester.pumpWidget(host(state));
      await settle(tester);
      expect(find.byKey(const ValueKey('harness-seed-v1')), findsOneWidget);
      expect(find.byKey(const ValueKey('session-a')), findsOneWidget);
      expect(find.byKey(const ValueKey('session-b')), findsOneWidget);
      expect(find.text('Kinaiya Core'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('session-a')));
      await settle(tester);
      expect(find.text('Recorded objective A'), findsOneWidget);
      final input = find.byKey(const ValueKey('agent-input'));
      await tester.enterText(input, 'Draft A');
      await tester.tap(find.byKey(const ValueKey('session-b')));
      await settle(tester);
      expect(tester.widget<TextField>(input).controller!.text, '');
      await tester.enterText(input, 'Draft B');
      await tester.tap(find.byKey(const ValueKey('session-a')));
      await settle(tester);
      expect(tester.widget<TextField>(input).controller!.text, 'Draft A');
      expect(api.creates, 0);
      expect(api.workRequests, 0);
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );
  testWidgets(
    'two sidebars collapse independently and ineligible harness stays inspectable',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(1600, 1000);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      final api = WorkspaceUiApi();
      final state = wired(api);
      await tester.pumpWidget(host(state));
      await settle(tester);
      await tester.tap(find.byTooltip('Collapse harnesses'));
      await tester.pump();
      expect(find.byTooltip('Expand harnesses'), findsOneWidget);
      expect(find.byTooltip('Collapse sessions'), findsOneWidget);
      await tester.tap(find.byTooltip('Collapse sessions'));
      await tester.pump();
      expect(find.byTooltip('Expand sessions'), findsOneWidget);
      await tester.tap(find.byTooltip('Expand harnesses'));
      await tester.pump();
      await tester.ensureVisible(
        find.byKey(const ValueKey('harness-unapproved')),
      );
      await tester.tap(find.byKey(const ValueKey('harness-unapproved')));
      await settle(tester);
      await tester.tap(find.byTooltip('Expand sessions'));
      await tester.pump();
      expect(state.selectedAgentHarness, 'unapproved');
      expect(state.canCreateAgentSession, isFalse);
      expect(
        tester
            .widget<OutlinedButton>(
              find.widgetWithText(OutlinedButton, 'New session'),
            )
            .onPressed,
        isNull,
      );
      expect(api.creates, 0);
      expect(api.workRequests, 0);
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );
  testWidgets(
    'phone sheets at doubled text keep selection and composer reachable',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(320, 640);
      tester.platformDispatcher.textScaleFactorTestValue = 2;
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      final state = wired(WorkspaceUiApi());
      await tester.pumpWidget(host(state));
      await settle(tester);
      await tester.tap(find.byKey(const ValueKey('open-agent-harnesses')));
      await settle(tester);
      expect(
        find.byKey(const ValueKey('agent-harness-sidebar')),
        findsOneWidget,
      );
      expect(tester.takeException(), isNull);
      await tester.tap(find.byTooltip('Collapse harnesses'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('open-agent-workspace')));
      await settle(tester);
      expect(
        find.byKey(const ValueKey('agent-session-sidebar')),
        findsOneWidget,
      );
      expect(tester.takeException(), isNull);
      await tester.tap(find.byTooltip('Collapse sessions'));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byKey(const ValueKey('agent-input')));
      expect(find.byKey(const ValueKey('agent-input')), findsOneWidget);
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );
  testWidgets(
    'late submitted task receipt cannot clear another selected draft or history',
    (tester) async {
      final api = WorkspaceUiApi()..submitted = Completer<AgentTask>();
      final state = wired(api);
      await state.selectAgentSession('a');
      await tester.pumpWidget(host(state));
      await settle(tester);
      final input = find.byKey(const ValueKey('agent-input'));
      await tester.enterText(input, 'Submitted A');
      await tester.pump();
      await tester.tap(find.text('Send task'));
      await tester.pump();
      expect(api.workRequests, 1);
      await state.selectAgentSession('b');
      state.setDraft(AppView.agent, 'Draft B');
      await settle(tester);
      api.submitted!.complete(
        AgentTask(
          taskId: 'old',
          sessionId: 'a',
          threadId: 'thread-a',
          epoch: 1,
          objective: 'Submitted A',
          objectiveRevision: 1,
          state: 'accepted',
          idempotencyKey: 'synthetic',
        ),
      );
      await settle(tester);
      expect(state.agentDraft, 'Draft B');
      expect(state.agentHistory, isEmpty);
      expect(state.agentSession!.sessionId, 'b');
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );
}
