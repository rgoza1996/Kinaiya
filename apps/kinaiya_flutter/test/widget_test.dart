import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';
import 'package:kinaiya_flutter/ui/setup_dialog.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

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
    'clicking the seated Home mascot opens setup with Agent offline',
    (tester) async {
      tester.view.physicalSize = const Size(1440, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final state = wiredState()..agentOnline = false;
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 300));

      final mascot = find.byType(MascotWidget);
      expect(mascot, findsOneWidget);
      final rect = tester.getRect(mascot);
      await tester.tapAt(
        Offset(rect.left + rect.width * .6, rect.top + rect.height * .3),
      );
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byType(ModelSetupDialog), findsOneWidget);
      expect(state.view, AppView.character);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'clicking the resting Home mascot when disconnected opens floating setup bubbles',
    (tester) async {
      tester.view.physicalSize = const Size(1440, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final state = AppState();
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 300));

      final sleepingMascot = find.byKey(
        const ValueKey('home-sleeping-artwork'),
      );
      expect(sleepingMascot, findsOneWidget);
      final rect = tester.getRect(sleepingMascot);
      await tester.tapAt(
        Offset(rect.left + rect.width * .5, rect.top + rect.height * .35),
      );
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byType(ModelSetupDialog), findsOneWidget);
      expect(find.byKey(const ValueKey('setup-bubble-model')), findsOneWidget);
      expect(find.byKey(const ValueKey('setup-bubble-view')), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

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

  testWidgets('Converse model button and summary open the same picker', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1440, 900);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final state = AppState();
    await tester.pumpWidget(KinaiyaApp(state: state));
    await tester.pump(const Duration(milliseconds: 300));
    await navigateTo(tester, AppView.character);
    final button = find.byKey(const ValueKey('converse-model-settings'));
    await tester.ensureVisible(button);
    await tester.tap(button);
    await tester.pump();
    expect(find.byKey(const ValueKey('quick-model-picker')), findsOneWidget);
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pump();
    expect(find.byKey(const ValueKey('quick-model-picker')), findsNothing);
    final summary = find.byKey(
      const ValueKey('converse-model-summary-identity'),
    );
    await tester.ensureVisible(summary);
    await tester.tap(summary);
    await tester.pump();
    expect(find.byKey(const ValueKey('quick-model-picker')), findsOneWidget);
    expect(state.converseDraft, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
  });

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

  testWidgets(
    'setup validation modal blocks background clicks and focus traversal and points to owner-login',
    (tester) async {
      final state = AppState();
      addTearDown(state.dispose);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: SetupDialog(state: state, embedded: true)),
        ),
      );

      expect(
        find.textContaining('kinaiya --data-dir <dir> owner-login'),
        findsOneWidget,
      );
      expect(find.textContaining('KINAIYA_OWNER_TOKEN'), findsNothing);

      await tester.enterText(
        find.byType(TextField).at(0),
        'http://127.0.0.1:18760',
      );
      await tester.tap(
        find.byKey(const ValueKey('setup-connect-primary-action')),
      );
      await tester.pump();

      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsOneWidget,
      );
      expect(find.textContaining('owner-login.txt'), findsWidgets);

      final okButton = tester.widget<FilledButton>(
        find.byKey(const ValueKey('setup-validation-alert-ok')),
      );
      expect(okButton.focusNode?.hasFocus, isTrue);

      // Background controls cannot be clicked or focused while the validation modal is open.
      expect(
        find
            .byKey(const ValueKey('setup-connect-primary-action'))
            .hitTestable(),
        findsNothing,
      );
      expect(find.byType(TextField).at(0).hitTestable(), findsNothing);
      expect(
        find.byType(DropdownButtonFormField<BackendProfile>).hitTestable(),
        findsNothing,
      );
      await tester.tap(
        find.byKey(const ValueKey('setup-endpoints-toggle')),
        warnIfMissed: false,
      );
      await tester.pump();
      expect(
        find.byKey(const ValueKey('setup-endpoints-summary')),
        findsNothing,
      );

      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      expect(okButton.focusNode?.hasFocus, isTrue);

      await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
      await tester.pump();
      expect(okButton.focusNode?.hasFocus, isTrue);

      await tester.tap(find.byKey(const ValueKey('setup-validation-alert-ok')));
      await tester.pump();

      expect(
        find.byKey(const ValueKey('setup-validation-alert-dialog')),
        findsNothing,
      );
      final connectButton = tester.widget<FilledButton>(
        find.byKey(const ValueKey('setup-connect-primary-action')),
      );
      expect(connectButton.focusNode?.hasFocus, isTrue);
      expect(find.text('http://127.0.0.1:18760'), findsOneWidget);
    },
  );

  testWidgets(
    'trusted local first-run auto-connects without entering 4 service URLs or typing an owner token',
    (tester) async {
      final state = AppState(
        localSignInExchange: () async => LocalSignInResult(
          ownerToken: 'ram-only-local-token',
          converseEnabled: false,
        ),
      );
      await state.attemptLocalSignIn(injected: mockClient());
      addTearDown(state.dispose);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: SetupDialog(state: state, embedded: true)),
        ),
      );
      await tester.pump();

      expect(
        find.byKey(const Key('local-host-connection-card')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('local-host-status-connected')),
        findsOneWidget,
      );
      expect(find.text('Connected to local Kinaiya host'), findsOneWidget);
      expect(find.textContaining('Connected (Agent-only)'), findsOneWidget);
      expect(
        find.textContaining('Converse optional / not enabled'),
        findsOneWidget,
      );
      // Service URL fields start collapsed on trusted local first run.
      expect(find.text('Session service URL'), findsNothing);
      expect(state.connected, isTrue);
      expect(state.sessionAuthenticated, isTrue);
      expect(state.connection!.backendProfile, BackendProfile.clean);
    },
  );

  testWidgets(
    'unavailable local sign-in shows truthful status, retry recovery, and advanced manual connection',
    (tester) async {
      var attempts = 0;
      final state = AppState(
        localSignInExchange: () async {
          attempts++;
          if (attempts == 1) {
            throw const FormatException('local handoff unavailable');
          }
          return LocalSignInResult(ownerToken: 'recovered-ram-token');
        },
      );
      addTearDown(state.dispose);

      await state.attemptLocalSignIn(injected: mockClient());
      expect(state.connected, isFalse);
      expect(state.localSignInState, LocalHostSignInState.unavailable);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: SetupDialog(state: state, embedded: true)),
        ),
      );
      await tester.pump();

      expect(
        find.byKey(const Key('local-host-status-unavailable')),
        findsOneWidget,
      );
      expect(find.text('Local automatic sign-in unavailable'), findsOneWidget);
      expect(find.byKey(const Key('local-host-retry-signin')), findsOneWidget);

      // Expand Advanced connection to verify manual endpoint & token fields remain usable.
      await tester.tap(find.byKey(const Key('setup-toggle-endpoint-fields')));
      await tester.pump();
      expect(find.text('Session service URL'), findsOneWidget);
      expect(find.text('Owner access token'), findsOneWidget);

      // Retry local sign-in recovers cleanly when injected/client succeeds.
      await state.attemptLocalSignIn(injected: mockClient());
      await tester.pump();
      expect(state.connected, isTrue);
      expect(
        find.byKey(const Key('local-host-status-connected')),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'local sign-in preserves inference disclosure and distinguishes Agent-only from broken Agent host',
    (tester) async {
      final state = AppState(
        localSignInExchange: () async => LocalSignInResult.fromJson({
          'owner_token': 'ram-token-2',
          'session_url': 'http://127.0.0.1:8760',
          'agent_url': 'http://127.0.0.1:8761',
          'converse_url': 'http://127.0.0.1:8762',
          'converse_enabled': false,
          'allow_cloud': true,
          'backend_profile': 'cloudDevelopment',
        }),
      );
      addTearDown(state.dispose);

      await state.attemptLocalSignIn(
        backendProfile: BackendProfile.clean,
        injected: mockClient(),
      );
      expect(state.connection!.backendProfile, BackendProfile.clean);
      expect(state.isAgentOnlyConnected, isTrue);
      expect(
        state.hostRoleStatusSummary,
        'Connected (Agent-only) · Converse optional / not enabled',
      );

      // Simulate broken Agent host (session authenticated, Agent offline).
      state.agentOnline = false;
      state.agentInferenceAllowed = false;
      state.converseOnline = false;
      state.converseInferenceAllowed = false;
      state.connected = false;
      expect(state.isAgentOnlyConnected, isFalse);
      expect(state.hostRoleStatusSummary, 'Agent host unreachable');
    },
  );
}
