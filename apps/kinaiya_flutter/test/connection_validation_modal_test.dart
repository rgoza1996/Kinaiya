import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/setup_dialog.dart';

void main() {
  test(
    'ConnectionProfile.validate explains missing owner access token clearly using owner-login',
    () {
      final profile = ConnectionProfile.defaults();
      final problem = profile.validate();
      expect(problem, isNotNull);
      expect(problem, contains('Owner access token is required.'));
      expect(problem, contains('kinaiya --data-dir <dir> owner-login'));
      expect(problem, contains('owner-login.txt'));
      expect(problem, contains('Load local access token'));
      expect(problem, isNot(contains('KINAIYA_OWNER_TOKEN')));
      expect(problem, isNot(contains('service-tokens.json')));
      expect(problem, isNot(contains('[REDACTED]')));
    },
  );

  testWidgets(
    'SetupDialog shows modal alert on missing owner token, blocks background interaction and focus traversal, and restores focus on dismiss',
    (WidgetTester t) async {
      final state = AppState();
      addTearDown(state.dispose);

      await t.pumpWidget(
        MaterialApp(
          home: Scaffold(body: SetupDialog(state: state, embedded: true)),
        ),
      );

      expect(
        find.textContaining('kinaiya --data-dir <dir> owner-login'),
        findsOneWidget,
      );
      expect(find.textContaining('KINAIYA_OWNER_TOKEN'), findsNothing);
      expect(find.textContaining('service-tokens.json'), findsNothing);
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
      expect(
        find.textContaining('kinaiya --data-dir <dir> owner-login'),
        findsWidgets,
      );

      final okButton = t.widget<FilledButton>(
        find.byKey(const ValueKey('setup-validation-alert-ok')),
      );
      expect(okButton.focusNode?.hasFocus, isTrue);

      // Background controls cannot be hit-tested or activated while modal is open.
      expect(
        find
            .byKey(const ValueKey('setup-connect-primary-action'))
            .hitTestable(),
        findsNothing,
      );
      expect(
        find.byKey(const ValueKey('setup-endpoints-toggle')).hitTestable(),
        findsNothing,
      );
      expect(
        find.byKey(const ValueKey('setup-advanced-toggle')).hitTestable(),
        findsNothing,
      );
      expect(
        find.byType(DropdownButtonFormField<BackendProfile>).hitTestable(),
        findsNothing,
      );
      expect(find.byType(TextField).at(0).hitTestable(), findsNothing);

      // Attempting to tap background controls does nothing.
      await t.tap(
        find.byKey(const ValueKey('setup-endpoints-toggle')),
        warnIfMissed: false,
      );
      await t.tap(
        find.byKey(const ValueKey('setup-advanced-toggle')),
        warnIfMissed: false,
      );
      await t.tap(
        find.byType(DropdownButtonFormField<BackendProfile>),
        warnIfMissed: false,
      );
      await t.pump();
      expect(
        find.byKey(const ValueKey('setup-endpoints-summary')),
        findsNothing,
      );
      expect(find.byKey(const ValueKey('setup-advanced-panel')), findsNothing);

      // Keyboard focus traversal (Tab / Shift+Tab) stays trapped on OK button.
      await t.sendKeyEvent(LogicalKeyboardKey.tab);
      await t.pump();
      expect(okButton.focusNode?.hasFocus, isTrue);

      await t.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
      await t.sendKeyEvent(LogicalKeyboardKey.tab);
      await t.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
      await t.pump();
      expect(okButton.focusNode?.hasFocus, isTrue);

      // Dismissing via OK closes modal, preserves entered text, and restores focus to Connect.
      await t.tap(find.byKey(const ValueKey('setup-validation-alert-ok')));
      await t.pump();

      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsNothing,
      );
      expect(find.text('http://127.0.0.1:18760'), findsOneWidget);
      final connectButton = t.widget<FilledButton>(
        find.byKey(const ValueKey('setup-connect-primary-action')),
      );
      expect(connectButton.focusNode?.hasFocus, isTrue);
      expect(
        find.byKey(const ValueKey('setup-endpoints-toggle')).hitTestable(),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'SetupDialog validation alert also dismisses via Escape and restores focus cleanly',
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

      await t.sendKeyEvent(LogicalKeyboardKey.escape);
      await t.pump();

      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsNothing,
      );
      expect(find.text('http://127.0.0.1:8761//v1'), findsOneWidget);
      final connectButton = t.widget<FilledButton>(
        find.byKey(const ValueKey('setup-connect-primary-action')),
      );
      expect(connectButton.focusNode?.hasFocus, isTrue);
    },
  );
}
