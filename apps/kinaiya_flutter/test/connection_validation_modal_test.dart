import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/setup_dialog.dart';

void main() {
  test(
    'ConnectionProfile.validate explains missing owner access token clearly',
    () {
      final profile = ConnectionProfile.defaults();
      final problem = profile.validate();
      expect(problem, isNotNull);
      expect(problem, contains('Owner access token is required.'));
      expect(problem, contains('KINAIYA_OWNER_TOKEN'));
      expect(problem, contains('Load local access token'));
      expect(problem, isNot(contains('[REDACTED]')));
    },
  );

  testWidgets(
    'SetupDialog shows modal alert on missing owner token, focuses OK, and preserves entered values on dismiss',
    (WidgetTester t) async {
      final state = AppState();
      addTearDown(state.dispose);

      await t.pumpWidget(
        MaterialApp(
          home: Scaffold(body: SetupDialog(state: state, embedded: true)),
        ),
      );

      expect(
        find.textContaining('Host authentication token (KINAIYA_OWNER_TOKEN)'),
        findsOneWidget,
      );
      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsNothing,
      );

      // Customize one of the URLs and leave owner token blank.
      await t.enterText(find.byType(TextField).at(0), 'http://127.0.0.1:18760');
      await t.tap(find.byKey(const ValueKey('setup-connect-primary-action')));
      await t.pump();

      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsOneWidget,
      );
      expect(find.text('Cannot connect to Kinaiya host'), findsOneWidget);
      expect(
        find.byKey(const ValueKey('setup-validation-error')),
        findsOneWidget,
      );
      expect(find.textContaining('KINAIYA_OWNER_TOKEN'), findsWidgets);

      final okButton = t.widget<FilledButton>(
        find.byKey(const ValueKey('setup-validation-alert-ok')),
      );
      expect(okButton.focusNode?.hasFocus, isTrue);

      await t.tap(find.byKey(const ValueKey('setup-validation-alert-ok')));
      await t.pump();

      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsNothing,
      );
      expect(find.text('http://127.0.0.1:18760'), findsOneWidget);
    },
  );

  testWidgets(
    'SetupDialog shows modal alert on invalid service URL and dismisses cleanly via OK',
    (WidgetTester t) async {
      final state = AppState();
      addTearDown(state.dispose);

      await t.pumpWidget(
        MaterialApp(
          home: Scaffold(body: SetupDialog(state: state)),
        ),
      );

      await t.enterText(
        find.byType(TextField).at(1),
        'http://127.0.0.1:8761//v1',
      );
      await t.enterText(find.byType(TextField).at(4), 'owner-token-123');
      await t.tap(find.text('Connect'));
      await t.pump();

      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsOneWidget,
      );
      expect(
        find.textContaining('Agent URL: invalid service path'),
        findsOneWidget,
      );

      await t.tap(find.byKey(const ValueKey('setup-validation-alert-ok')));
      await t.pump();

      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsNothing,
      );
      expect(find.text('http://127.0.0.1:8761//v1'), findsOneWidget);
    },
  );
}
