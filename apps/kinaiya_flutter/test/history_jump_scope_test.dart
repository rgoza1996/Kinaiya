// Focused review regression for the sealed PR #256 marker-scroll scope race.
// These are real AgentChat widgets and ScrollPositions with synthetic readers;
// no tool, model, terminal, or external service is exercised.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/models.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'agent_workspace_integration_test.dart'
    show WorkspaceUiApi, host, settle, close;
import 'agent_workspace_state_test.dart' show entry, sessionJson, wired;

const _historyKey = ValueKey('agent-history-scroll');
const _railKey = ValueKey('agent-history-rail');
const _marker = 'Jump to your message 3 of 60';

List<JournalEntry> history(String session, String label, {int epoch = 1}) => [
  for (var index = 1; index <= 60; index++)
    entry(
      session,
      epoch == 1 && label == 'Original' ? index : 100 + index,
      epoch: epoch,
      text:
          '$label recorded message $index\n'
          'Synthetic content repeated to give every recorded row a stable height.\n'
          'No instruction or tool action is attached to this fixture.',
    ),
];

ScrollController historyController(WidgetTester tester) =>
    tester.widget<ListView>(find.byKey(_historyKey)).controller!;

Iterable<Semantics> selectedMarkers(WidgetTester tester) => tester
    .widgetList<Semantics>(
      find.descendant(
        of: find.byKey(_railKey),
        matching: find.byType(Semantics),
      ),
    )
    .where((widget) => widget.properties.selected == true);

Future<AppState> mountHistory(WidgetTester tester, WorkspaceUiApi api) async {
  tester.view.physicalSize = const Size(1800, 1000);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final state = wired(api);
  addTearDown(() => close(tester, state));
  await state.selectAgentSession('a');
  await tester.pumpWidget(host(state));
  await settle(tester);
  expect(state.agentHistory.length, 60);
  return state;
}

Future<double> startActualJump(WidgetTester tester) async {
  final controller = historyController(tester);
  expect(controller.hasClients, isTrue);
  expect(controller.offset, 0);
  await tester.tap(find.byTooltip(_marker));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 30));
  expect(controller.position.isScrollingNotifier.value, isTrue);
  expect(controller.offset, greaterThan(0));
  return controller.offset;
}

void main() {
  testWidgets('same-scope recorded marker jump completes normally', (
    tester,
  ) async {
    final api = WorkspaceUiApi()..history['a'] = history('a', 'Original');
    await mountHistory(tester, api);
    final inFlight = await startActualJump(tester);
    await tester.pump(const Duration(milliseconds: 200));
    final controller = historyController(tester);
    expect(controller.offset, greaterThan(inFlight));
    expect(controller.position.isScrollingNotifier.value, isFalse);
    expect(selectedMarkers(tester), hasLength(1));
    expect(
      tester
          .getRect(find.byKey(_historyKey))
          .overlaps(
            tester.getRect(
              find.text(api.history['a']![2].payload['objective'] as String),
            ),
          ),
      isTrue,
    );
    expect(api.workRequests, 0);
    expect(api.commandRequests, 0);
    expect(tester.takeException(), isNull);
  });

  for (final replacement in ['session', 'epoch', 'connection']) {
    testWidgets(
      'in-progress marker jump cannot move $replacement replacement history',
      (tester) async {
        final api = WorkspaceUiApi()
          ..history['a'] = history('a', 'Original')
          ..history['b'] = history('b', 'Replacement');
        final state = await mountHistory(tester, api);
        final oldController = historyController(tester);
        final inFlight = await startActualJump(tester);
        final originalConnection = state.connectionGeneration;
        var activeApi = api;
        if (replacement == 'session') {
          await state.selectAgentSession('b');
          expect(state.agentSession!.sessionId, 'b');
        } else if (replacement == 'epoch') {
          api.sessions['a'] = SessionInfo.fromJson(sessionJson('a', epoch: 2));
          api.history['a'] = history('a', 'Replacement', epoch: 2);
          await state.selectAgentSession('a');
          expect(state.agentSession!.epoch, 2);
        } else {
          activeApi = WorkspaceUiApi()
            ..history['b'] = history('b', 'Replacement');
          state.connect(ConnectionProfile.defaults(), injected: activeApi);
          await state.selectAgentSession('b');
          expect(state.connectionGeneration, greaterThan(originalConnection));
          expect(identical(state.client, activeApi), isTrue);
        }
        await tester.pump();
        final current = historyController(tester);
        final replacedAt = current.offset;
        final scrollingAtReplacement =
            current.position.isScrollingNotifier.value;
        await tester.pump(const Duration(milliseconds: 60));
        final later = current.offset;
        await tester.pump(const Duration(milliseconds: 160));
        final finished = current.offset;
        debugPrint(
          'HISTORY_JUMP_ORACLE ${jsonEncode({'replacement': replacement, 'shared_controller': identical(oldController, current), 'old_in_flight_offset': inFlight, 'replacement_offset': replacedAt, 'later_offset': later, 'finished_offset': finished, 'scrolling_at_replacement': scrollingAtReplacement, 'selected_replacement_markers': selectedMarkers(tester).length, 'work_requests': api.workRequests + (identical(api, activeApi) ? 0 : activeApi.workRequests), 'command_requests': api.commandRequests + (identical(api, activeApi) ? 0 : activeApi.commandRequests)})}',
        );
        expect(state.agentHistory.length, 60);
        expect(selectedMarkers(tester), isEmpty);
        expect(later, closeTo(replacedAt, 0.01));
        expect(finished, closeTo(replacedAt, 0.01));
        expect(scrollingAtReplacement, isFalse);
        expect(api.workRequests + activeApi.workRequests, 0);
        expect(api.commandRequests + activeApi.commandRequests, 0);
        expect(tester.takeException(), isNull);
      },
    );
  }
}
