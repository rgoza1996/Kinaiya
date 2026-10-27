import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';

import 'client_test.dart' show mockClient;

AppState wiredState() {
  final state = AppState();
  state.connect(
    ConnectionProfile(
      sessionUrl: 'http://127.0.0.1:8760',
      agentUrl: 'http://127.0.0.1:8761',
      converseUrl: 'http://127.0.0.1:8762',
      ownerToken: 'test-token',
    ),
    injected: mockClient(),
  );
  return state;
}

void main() {
  testWidgets('converse has no command UI; slash text stays ordinary', (
    tester,
  ) async {
    final state = wiredState();
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));

    expect(find.text('Talk with Kinaiya'), findsOneWidget);
    expect(find.text('Send'), findsOneWidget);
    expect(find.text('Send task'), findsNothing);

    await tester.enterText(find.byType(TextField), '/status do stuff');
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.text('Send'));
    await tester.pump(const Duration(milliseconds: 300));

    // Ordinary reply arrives; nothing was dispatched as a command.
    expect(find.text('hi'), findsOneWidget);
    expect(state.agentHistory, isEmpty);
    expect(state.tasks, isEmpty);
  });

  testWidgets('drafts survive view changes; agent slash needs registry', (
    tester,
  ) async {
    final state = wiredState();
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));

    await tester.enterText(find.byType(TextField), 'remember me');
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.text('Agent Chat'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text('Send task'), findsOneWidget);

    await tester.enterText(find.byType(TextField), '/nope');
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.text('Send task'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(state.agentNotice, contains('registry unavailable'));
    expect(state.agentHistory, isEmpty);

    await tester.tap(find.text('Converse'));
    await tester.pump(const Duration(milliseconds: 200));
    expect(state.converseDraft, 'remember me');
  });
}
