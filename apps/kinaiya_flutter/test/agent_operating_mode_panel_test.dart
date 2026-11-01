import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/agent_control.dart';
import 'package:kinaiya_flutter/api/agent_control_client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/agent_mode_writes.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_operating_mode_panel.dart';
import 'agent_control_test.dart';
import 'agent_control_transport_test.dart' show controlApi;
import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, sentBody;
import 'agent_workspace_integration_test.dart'
    show WorkspaceUiApi, host, settle, close;
import 'agent_workspace_state_test.dart' show wired;

class ModeWorkspaceApi extends WorkspaceUiApi {
  ModeWorkspaceApi(this.control);
  final AgentControlApi control;
  @override
  AgentControlApi get agentControl => control;
}

class ModePanelFixture {
  ModePanelFixture({
    this.token = 'synthetic-mode-panel-owner',
    this.backend = controlBackendA,
    this.instance = controlInstanceA,
    this.mode = 'on_demand',
    this.revision = 0,
    this.writable = true,
    this.holdFirst = false,
    this.historyOnly = false,
    this.metadataStatus = 200,
  });
  final String token, backend, instance;
  String mode;
  int revision, metadataStatus;
  bool writable;
  final bool holdFirst, historyOnly;
  int reads = 0;
  final posts = <Map<String, Object?>>[], lookups = <Map<String, Object?>>[];
  final first = Completer<http.StreamedResponse>();
  Map<String, Object?> get state => controlStateJson(
    backend: backend,
    instance: instance,
    revision: revision,
    mode: mode,
    writable: writable,
  );
  late final api = controlApi(
    () => ModelTestClient((request) async {
      if (request.method == 'GET') {
        reads++;
        return modelResponse(
          metadataStatus == 200 ? state : {'private': 'PRIVATE CONTROL ERROR'},
          status: metadataStatus,
        );
      }
      final original = sentBody(request);
      if (request.url.path.endsWith('/lookup')) {
        lookups.add(original);
        return modelResponse(modeLookupJson(original, state: state));
      }
      posts.add(original);
      if (holdFirst && posts.length == 1) return first.future;
      revision = (original['expected_revision'] as int) + (historyOnly ? 2 : 1);
      mode = historyOnly ? 'on_demand' : original['mode'] as String;
      return modelResponse(modeReceiptJson(original));
    }),
    token: token,
    timeoutMs: 10000,
  );
}

Future<void> mountMode(
  WidgetTester tester,
  AgentControlApi? api,
  AgentModeWrites writes, {
  bool Function()? active,
  bool expand = true,
  bool ownerAuthenticated = true,
}) async {
  await tester.binding.setSurfaceSize(const Size(900, 1000));
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: AgentOperatingModePanel(
            key: ObjectKey(api),
            api: api,
            ownerAuthenticated: ownerAuthenticated,
            active: active ?? () => true,
            writes: writes,
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  if (expand) {
    await tester.tap(find.text('Agent operating mode'));
    await tester.pumpAndSettle();
  }
}

Future<void> chooseMode(WidgetTester tester, AgentMode mode) async {
  await tester.tap(find.byKey(const ValueKey('agent-mode-selection')));
  await tester.pumpAndSettle();
  await tester.tap(find.text(mode.label).last);
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'Agent Chat scroll inspection saves through the Session mode API and refreshes actual values',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.binding.setSurfaceSize(const Size(900, 1100));
      final fixture = ModePanelFixture(
        token: 'synthetic-integrated-mode-owner',
      );
      final api = ModeWorkspaceApi(fixture.api)
        ..fixtures = List.generate(
          12,
          (i) => AgentTask(
            taskId: 'synthetic-recorded-$i',
            sessionId: 'a',
            threadId: 'thread-a',
            epoch: 1,
            objective: 'Recorded synthetic task $i',
            objectiveRevision: 1,
            state: 'succeeded',
            idempotencyKey: 'recorded-key-$i',
          ),
        );
      final state = wired(api);
      await state.selectAgentSession('a');
      state.setTasks(api.fixtures);
      await tester.pumpWidget(host(state));
      await settle(tester);
      expect(fixture.reads, 0);
      final scrolling = find
          .descendant(
            of: find.byType(ListView).first,
            matching: find.byType(Scrollable),
          )
          .first;
      await tester.scrollUntilVisible(
        find.text('Agent operating mode'),
        400,
        scrollable: scrolling,
      );
      await tester.tap(find.text('Agent operating mode'));
      await tester.pumpAndSettle();
      expect(fixture.reads, 1);
      await tester.ensureVisible(
        find.byKey(const ValueKey('agent-mode-selection')),
      );
      await tester.pumpAndSettle();
      await chooseMode(tester, AgentMode.off);
      await tester.ensureVisible(find.text('Save mode'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Save mode'));
      await tester.pumpAndSettle();
      expect(fixture.posts.length, 1);
      expect(fixture.posts.single.length, 7);
      expect(fixture.posts.single['mode'], 'off');
      expect(fixture.posts.single['expected_backend'], controlBackendA);
      expect(fixture.posts.single['expected_instance'], controlInstanceA);
      expect(fixture.posts.single['expected_revision'], 0);
      expect(fixture.reads, 2);
      expect(fixture.mode, 'off');
      expect(fixture.lookups, isEmpty);
      expect(find.text('Current mode: Off · revision 1'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );
  testWidgets(
    'active controller eligibility never grants the reader owner permission',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final writes = AgentModeWrites();
      final f = ModePanelFixture();
      await mountMode(tester, f.api, writes, ownerAuthenticated: false);
      expect(f.reads, 1);
      expect(find.text('Current mode: On-demand · revision 0'), findsOneWidget);
      expect(
        find.text('Read-only: owner authentication is not verified.'),
        findsOneWidget,
      );
      expect(
        tester
            .widget<DropdownButton<AgentMode>>(
              find.byKey(const ValueKey('agent-mode-selection')),
            )
            .onChanged,
        isNull,
      );
      expect(
        tester
            .widget<FilledButton>(
              find.widgetWithText(FilledButton, 'Save mode'),
            )
            .onPressed,
        isNull,
      );
      expect(f.posts, isEmpty);
      expect(f.lookups, isEmpty);
      await tester.pumpWidget(const SizedBox());
      await f.api.close();
      writes.dispose();
    },
  );
  testWidgets(
    'same facade reconnect invalidates the earlier mode observation',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.binding.setSurfaceSize(const Size(1400, 1000));
      final fixture = ModePanelFixture(mode: 'off');
      final api = ModeWorkspaceApi(fixture.api);
      final state = wired(api);
      await state.selectAgentSession('a');
      await tester.pumpWidget(host(state));
      await settle(tester);
      await tester.tap(find.text('Agent operating mode'));
      await tester.pumpAndSettle();
      expect(find.text('Current mode: Off · revision 0'), findsOneWidget);
      expect(fixture.reads, 1);
      state.connect(ConnectionProfile.defaults(), injected: api);
      await settle(tester);
      expect(find.text('Current mode: unknown · read-only'), findsOneWidget);
      expect(find.text('Current mode: Off · revision 0'), findsNothing);
      expect(fixture.reads, 1);
      await close(tester, state);
    },
  );
  testWidgets(
    'closed panel is explicitly unknown; inspection reads once without background polling',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final writes = AgentModeWrites();
      final f = ModePanelFixture();
      await mountMode(tester, f.api, writes, expand: false);
      expect(find.text('Current mode: unknown · read-only'), findsOneWidget);
      expect(f.reads, 0);
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pumpAndSettle();
      expect(f.reads, 1);
      expect(find.text('Current mode: On-demand · revision 0'), findsOneWidget);
      final dropdown = tester.widget<DropdownButton<AgentMode>>(
        find.byKey(const ValueKey('agent-mode-selection')),
      );
      expect(
        dropdown.items!
            .singleWhere((item) => item.value == AgentMode.autonomous)
            .enabled,
        isFalse,
      );
      await tester.pump(const Duration(seconds: 20));
      expect(f.reads, 1);
      expect(f.posts, isEmpty);
      expect(f.lookups, isEmpty);
      await tester.pumpWidget(const SizedBox());
      await f.api.close();
      writes.dispose();
    },
  );
  testWidgets(
    'unknown or unsupported mode has no assumed default and cannot be edited',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final writes = AgentModeWrites();
      final f = ModePanelFixture(metadataStatus: 404);
      await mountMode(tester, f.api, writes);
      expect(find.text('Current mode: unknown · read-only'), findsOneWidget);
      expect(
        find.textContaining('Read-only until current mode'),
        findsOneWidget,
      );
      expect(find.byKey(const ValueKey('agent-mode-selection')), findsNothing);
      expect(find.text('Save mode'), findsNothing);
      expect(find.textContaining('PRIVATE'), findsNothing);
      expect(f.posts, isEmpty);
      expect(f.lookups, isEmpty);
      await tester.pumpWidget(const SizedBox());
      await f.api.close();
      writes.dispose();
    },
  );
  testWidgets(
    'historical mode save never becomes active state and explicit save reads fresh metadata once',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final writes = AgentModeWrites();
      final f = ModePanelFixture(historyOnly: true);
      await mountMode(tester, f.api, writes);
      await chooseMode(tester, AgentMode.off);
      await tester.tap(find.text('Save mode'));
      await tester.pumpAndSettle();
      expect(f.posts.length, 1);
      expect(f.posts.single.length, 7);
      expect(f.posts.single['mode'], 'off');
      expect(f.posts.single['expected_backend'], controlBackendA);
      expect(f.posts.single['expected_instance'], controlInstanceA);
      expect(f.posts.single['expected_revision'], 0);
      expect(f.posts.single['active_work_policy'], 'finish_already_accepted');
      expect(f.reads, 2);
      expect(f.lookups, isEmpty);
      expect(find.text('Current mode: On-demand · revision 2'), findsOneWidget);
      expect(find.textContaining('confirmed in history'), findsOneWidget);
      expect(
        tester
            .widget<DropdownButton<AgentMode>>(
              find.byKey(const ValueKey('agent-mode-selection')),
            )
            .value,
        AgentMode.onDemand,
      );
      expect(
        find.textContaining('Work already accepted may finish'),
        findsOneWidget,
      );
      await tester.pump(const Duration(seconds: 20));
      expect(f.posts.length, 1);
      expect(f.reads, 2);
      await tester.pumpWidget(const SizedBox());
      await f.api.close();
      writes.dispose();
    },
  );
  testWidgets(
    'persisted unavailable autonomy displays truthfully and retired writer stays read-only',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final writes = AgentModeWrites();
      final f = ModePanelFixture(mode: 'autonomous', writable: false);
      await mountMode(tester, f.api, writes);
      expect(
        find.text('Current mode: Autonomous · revision 0 · read-only'),
        findsOneWidget,
      );
      expect(
        find.textContaining('monitoring is not configured'),
        findsOneWidget,
      );
      final dropdown = tester.widget<DropdownButton<AgentMode>>(
        find.byKey(const ValueKey('agent-mode-selection')),
      );
      expect(dropdown.value, AgentMode.autonomous);
      expect(dropdown.onChanged, isNull);
      expect(dropdown.items!.last.enabled, isFalse);
      expect(
        tester
            .widget<FilledButton>(
              find.widgetWithText(FilledButton, 'Save mode'),
            )
            .onPressed,
        isNull,
      );
      expect(f.posts, isEmpty);
      await tester.pumpWidget(const SizedBox());
      await f.api.close();
      writes.dispose();
    },
  );
  testWidgets(
    'pending survives close and same-owner reconnect; retry uses all original fields',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final writes = AgentModeWrites();
      final original = ModePanelFixture(holdFirst: true);
      await mountMode(tester, original.api, writes);
      await chooseMode(tester, AgentMode.off);
      await tester.tap(find.text('Save mode'));
      await tester.pumpAndSettle();
      final body = original.posts.single;
      await tester.pump(const Duration(seconds: 8));
      await tester.pump();
      expect(
        find.textContaining('Earlier mode save is unconfirmed'),
        findsOneWidget,
      );
      await tester.pumpWidget(const SizedBox());
      await original.api.close();
      final reconnect = ModePanelFixture();
      await mountMode(tester, reconnect.api, writes);
      expect(reconnect.posts, isEmpty);
      expect(reconnect.lookups, isEmpty);
      expect(find.text('Check original save'), findsOneWidget);
      await tester.tap(find.text('Retry exact save'));
      await tester.pumpAndSettle();
      expect(reconnect.posts.single, body);
      expect(reconnect.lookups, isEmpty);
      expect(find.text('Current mode: Off · revision 1'), findsOneWidget);
      original.first.complete(modelResponse(modeReceiptJson(body)));
      await tester.pumpAndSettle();
      expect(find.text('Current mode: Off · revision 1'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      await reconnect.api.close();
      writes.dispose();
    },
  );
  testWidgets(
    'owner/backend changes hide pending; same backend restart reconciles by lookup only',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final writes = AgentModeWrites();
      final original = ModePanelFixture(holdFirst: true);
      await mountMode(tester, original.api, writes);
      await chooseMode(tester, AgentMode.off);
      await tester.tap(find.text('Save mode'));
      await tester.pumpAndSettle();
      final body = original.posts.single;
      for (final f in [
        ModePanelFixture(token: 'other-synthetic-mode-owner'),
        ModePanelFixture(backend: controlBackendB),
      ]) {
        await tester.pumpWidget(const SizedBox());
        await mountMode(tester, f.api, writes);
        expect(find.text('Check original save'), findsNothing);
        expect(find.textContaining('unconfirmed'), findsNothing);
        expect(f.posts, isEmpty);
        expect(f.lookups, isEmpty);
        await tester.pumpWidget(const SizedBox());
        await f.api.close();
      }
      final restarted = ModePanelFixture(
        instance: controlInstanceB,
        revision: 2,
        writable: false,
      );
      await mountMode(tester, restarted.api, writes);
      expect(find.text('Check original save'), findsOneWidget);
      expect(
        tester
            .widget<TextButton>(
              find.widgetWithText(TextButton, 'Retry exact save'),
            )
            .onPressed,
        isNull,
      );
      await tester.tap(find.text('Check original save'));
      await tester.pumpAndSettle();
      expect(restarted.lookups.single, body);
      expect(restarted.posts, isEmpty);
      expect(
        find.text('Current mode: On-demand · revision 2 · read-only'),
        findsOneWidget,
      );
      expect(find.text('Check original save'), findsNothing);
      original.first.complete(modelResponse(modeReceiptJson(body)));
      await tester.pumpAndSettle();
      expect(
        find.text('Current mode: On-demand · revision 2 · read-only'),
        findsOneWidget,
      );
      await tester.pumpWidget(const SizedBox());
      await original.api.close();
      await restarted.api.close();
      writes.dispose();
    },
  );
  testWidgets(
    'late metadata from an inactive connection cannot replace current scope or notice',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final writes = AgentModeWrites();
      final held = Completer<http.StreamedResponse>();
      var active = true;
      final old = controlApi(() => ModelTestClient((_) => held.future));
      await mountMode(tester, old, writes, active: () => active);
      active = false;
      final replacement = ModePanelFixture(
        backend: controlBackendB,
        mode: 'off',
      );
      await tester.pumpWidget(const SizedBox());
      await mountMode(tester, replacement.api, writes);
      expect(find.text('Current mode: Off · revision 0'), findsOneWidget);
      held.complete(modelResponse(controlStateJson()));
      await tester.pumpAndSettle();
      expect(find.text('Current mode: Off · revision 0'), findsOneWidget);
      expect(find.textContaining('On-demand ·'), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      await old.close();
      await replacement.api.close();
      writes.dispose();
    },
  );
  testWidgets(
    'Agent Chat leaves explicit work request behavior unchanged while mode is unknown',
    (tester) async {
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.binding.setSurfaceSize(const Size(1400, 1000));
      final api = WorkspaceUiApi();
      final state = wired(api);
      await state.selectAgentSession('a');
      await tester.pumpWidget(host(state));
      await settle(tester);
      expect(find.text('Agent operating mode'), findsOneWidget);
      expect(find.text('Current mode: unknown · read-only'), findsOneWidget);
      await tester.enterText(
        find.byKey(const ValueKey('agent-input')),
        'Synthetic explicit owner task',
      );
      await tester.pump();
      await tester.tap(find.text('Send task'));
      await settle(tester);
      expect(api.workRequests, 1);
      expect(tester.takeException(), isNull);
      await close(tester, state);
    },
  );
}
