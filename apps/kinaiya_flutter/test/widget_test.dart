import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';

import 'client_test.dart' show mockClient;
import 'concept_layout_test.dart' show openConverseChat, navigateTo;

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
  testWidgets(
    'mascot opens setup during a resource pause and Agent remains independent',
    (tester) async {
      final semantics = tester.ensureSemantics();
      final state = wiredState()
        ..converseEnabled = false
        ..resourcePhase = 'local_work'
        ..resourcesConfigured = true;
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 200));
      await tester.tap(find.bySemanticsLabel(RegExp('Open Kinaiya setup')));
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byType(ModelSetupDialog), findsOneWidget);
      expect(state.view, AppView.character);
      expect(state.converseEnabled, isFalse);
      await tester.tap(find.byTooltip('Close setup'));
      await tester.pump(const Duration(milliseconds: 300));
      await navigateTo(tester, AppView.agent);
      expect(state.view, AppView.agent);
      expect(find.text('Kinaiya'), findsOneWidget);
      expect(find.text('Send task'), findsOneWidget);
      expect(find.byType(ModelSetupDialog), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      semantics.dispose();
    },
  );

  testWidgets('converse has no command UI; slash text stays ordinary', (
    tester,
  ) async {
    final state = wiredState();
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));

    expect(find.byKey(const ValueKey('converse-input')), findsNothing);
    expect(find.byKey(const ValueKey('call-kinaiya')), findsOneWidget);
    expect(find.byKey(const ValueKey('home-setup')), findsOneWidget);
    expect(find.text('Send task'), findsNothing);

    await openConverseChat(tester);
    expect(
      find.descendant(
        of: find.byType(Dialog),
        matching: find.text('Talk with Kinaiya'),
      ),
      findsOneWidget,
    );
    expect(find.text('Send'), findsOneWidget);

    await tester.enterText(find.byType(TextField), '/status do stuff');
    await tester.pump(const Duration(milliseconds: 100));
    await tester.ensureVisible(find.text('Send'));
    await tester.pump(const Duration(milliseconds: 200));
    expect(find.text('Send').hitTestable(), findsOneWidget);
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
    await state.ensureSession('agent', name: 'work');
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 200));

    await openConverseChat(tester);
    await tester.enterText(find.byType(TextField), 'remember me');
    await tester.pump(const Duration(milliseconds: 100));
    await navigateTo(tester, AppView.agent);
    expect(find.text('Send task'), findsOneWidget);

    await tester.enterText(find.byType(TextField), '/nope');
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.text('Send task'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(state.agentNotice, contains('registry unavailable'));
    expect(state.agentHistory, isEmpty);

    await navigateTo(tester, AppView.character);
    await openConverseChat(tester);
    expect(
      tester
          .widget<TextField>(find.byKey(const ValueKey('converse-input')))
          .controller!
          .text,
      'remember me',
    );
    expect(state.converseDraft, 'remember me');
  });
}
