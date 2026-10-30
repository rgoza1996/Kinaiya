import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/agent_chat.dart';
import 'package:kinaiya_flutter/ui/widgets.dart' show StatusChip;

import 'client_test.dart' show mockTransport;

class PenpotAgentApi extends KinaiyaClient {
  PenpotAgentApi()
    : super(
        sessionBase: 'http://fixture',
        agentBase: 'http://fixture',
        converseBase: 'http://fixture',
        ownerToken: 'synthetic',
        httpClient: mockTransport(commandsMissing: false),
      );
  List<AgentTask> fixtures = [];
  @override
  Future<List<AgentTask>> listTasks() async => fixtures;
}

AgentTask penpotTask(
  String id,
  String state, {
  String objective = '',
  List<Evidence> evidence = const [],
  String summary = '',
  String? error,
}) => AgentTask(
  taskId: id,
  sessionId: 'agent',
  threadId: 'thread-agent',
  epoch: 1,
  objective: objective.isEmpty ? 'Synthetic task $id' : objective,
  objectiveRevision: 1,
  state: state,
  idempotencyKey: 'synthetic-$id',
  summary: summary,
  evidence: evidence,
  error: error,
);

AppState penpotWired(PenpotAgentApi api) => AppState()
  ..connect(ConnectionProfile.defaults(), injected: api)
  ..agentSession = SessionInfo(
    sessionId: 'agent',
    threadId: 'thread-agent',
    channel: 'agent',
    epoch: 1,
    name: 'Synthetic agent',
  )
  ..setView(AppView.agent);

Future<void> penpotSettle(WidgetTester tester) async {
  await tester.pump(const Duration(milliseconds: 200));
  await tester.pump(const Duration(milliseconds: 20));
}

Future<void> penpotClose(WidgetTester tester, AppState state) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
  state.dispose();
}

void main() {
  testWidgets('desktop shows workspace, conversation and details', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(1560, 1000);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final api = PenpotAgentApi()..fixtures = [penpotTask('one', 'running')];
    final state = penpotWired(api);
    state.setTasks(api.fixtures);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await penpotSettle(tester);
    expect(find.text('Workspace'), findsOneWidget);
    expect(find.widgetWithText(TextField, 'Search tasks'), findsOneWidget);
    expect(find.byKey(const ValueKey('workspace-task-one')), findsOneWidget);
    expect(find.text('Direct Agent Chat'), findsOneWidget);
    expect(find.byKey(const ValueKey('agent-input')), findsOneWidget);
    expect(find.text('Send task'), findsOneWidget);
    expect(find.text('Task details'), findsOneWidget);
    expect(find.bySemanticsLabel(RegExp(r'\bStatus: running\b')), findsWidgets);
    expect(find.text('No tasks yet.'), findsNothing);
    expect(tester.takeException(), isNull);
    await penpotClose(tester, state);
    semantics.dispose();
  });

  testWidgets('narrow hides workspace and details, keeps conversation', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(360, 800);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final api = PenpotAgentApi()..fixtures = [penpotTask('one', 'running')];
    final state = penpotWired(api);
    state.setTasks(api.fixtures);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await penpotSettle(tester);
    expect(find.text('Workspace'), findsNothing);
    expect(find.text('Task details'), findsNothing);
    expect(find.byKey(const ValueKey('agent-input')), findsOneWidget);
    expect(find.text('Synthetic task one'), findsWidgets);
    expect(tester.takeException(), isNull);
    await penpotClose(tester, state);
  });

  testWidgets('320x480 at scale2 keeps agent draft through keyboard inset', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 480);
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetViewInsets);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    final api = PenpotAgentApi();
    final state = penpotWired(api);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await penpotSettle(tester);
    final field = find.byKey(const ValueKey('agent-input'));
    await tester.ensureVisible(field);
    await tester.showKeyboard(field);
    await tester.enterText(field, 'Synthetic draft');
    final original = tester
        .widget<EditableText>(
          find.descendant(of: field, matching: find.byType(EditableText)),
        )
        .focusNode;
    tester.view.viewInsets = const FakeViewPadding(bottom: 320);
    await penpotSettle(tester);
    final current = tester
        .widget<EditableText>(
          find.descendant(of: field, matching: find.byType(EditableText)),
        )
        .focusNode;
    expect(identical(original, current), isTrue);
    expect(current.hasFocus, isTrue);
    expect(state.agentDraft, 'Synthetic draft');
    expect(tester.takeException(), isNull);
    await penpotClose(tester, state);
  });

  testWidgets('honest empty and ungrounded success states', (tester) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(360, 800);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final api = PenpotAgentApi();
    final state = penpotWired(api);
    state.setTasks(const []);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await penpotSettle(tester);
    expect(find.text('No tasks yet.'), findsNothing);
    expect(find.text('What would you like to work on?'), findsOneWidget);
    expect(find.text('Succeeded with evidence below.'), findsNothing);
    api.fixtures = [penpotTask('u1', 'succeeded')];
    state.setTasks(api.fixtures);
    await penpotSettle(tester);
    expect(
      find.text('Finished without independent completion evidence.'),
      findsOneWidget,
    );
    expect(find.text('Succeeded with evidence below.'), findsNothing);
    expect(tester.takeException(), isNull);
    await penpotClose(tester, state);
  });

  testWidgets('grounded success shows evidence receipt honestly', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(360, 800);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final api = PenpotAgentApi()
      ..fixtures = [
        penpotTask(
          'g1',
          'succeeded',
          evidence: [
            Evidence(
              kind: 'file',
              summary: 'wrote proof',
              path: '/proof.txt',
              sha256: 'abcdef1234567890',
              objectiveRevision: 1,
            ),
          ],
        ),
      ];
    final state = penpotWired(api);
    state.setTasks(api.fixtures);
    await tester.pumpWidget(KinaiyaApp(state: state));
    await penpotSettle(tester);
    expect(find.text('Succeeded with evidence below.'), findsOneWidget);
    expect(
      find.text('Finished without independent completion evidence.'),
      findsNothing,
    );
    expect(
      find.bySemanticsLabel(RegExp(r'\bStatus: succeeded\b')),
      findsWidgets,
    );
    expect(tester.takeException(), isNull);
    await penpotClose(tester, state);
    semantics.dispose();
  });

  for (final scheme in [
    const ColorScheme.light(primary: Color(0xFF075BFF)),
    const ColorScheme.dark(
      primary: Color(0xFF075BFF),
      surface: Color(0xFF0B1B2E),
      onSurface: Color(0xFFF3F6FF),
    ),
  ]) {
    testWidgets('grounded body text remains readable in ${scheme.brightness}', (
      tester,
    ) async {
      final api = PenpotAgentApi()
        ..fixtures = [
          penpotTask(
            'contrast',
            'succeeded',
            evidence: [Evidence(kind: 'file', summary: 'Synthetic receipt')],
          ),
        ];
      final state = penpotWired(api)..setTasks(api.fixtures);
      await tester.pumpWidget(
        MaterialApp(
          theme: ThemeData.from(
            colorScheme: scheme,
          ).copyWith(cardTheme: CardThemeData(color: scheme.surface)),
          home: Scaffold(
            body: ListenableBuilder(
              listenable: state,
              builder: (_, _) => AgentChat(state: state),
            ),
          ),
        ),
      );
      await penpotSettle(tester);
      final text = tester.widget<Text>(
        find.text('Succeeded with evidence below.'),
      );
      final ink = text.style!.color!.computeLuminance();
      final fill = scheme.surface.computeLuminance();
      final contrast = ink > fill
          ? (ink + .05) / (fill + .05)
          : (fill + .05) / (ink + .05);
      expect(contrast, greaterThanOrEqualTo(4.5));
      expect(
        tester.widget<StatusChip>(find.byType(StatusChip)).tone,
        scheme.primary,
      );
      expect(tester.takeException(), isNull);
      await penpotClose(tester, state);
    });
  }
}
