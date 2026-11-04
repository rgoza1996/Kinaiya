import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/config/connection.dart';
import 'package:kinaiya_flutter/config/options_navigation.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';
import 'package:kinaiya_flutter/ui/moonlit_home.dart';
import 'package:kinaiya_flutter/ui/presentation_settings.dart';
import 'package:kinaiya_flutter/ui/setup_dialog.dart';
import 'package:kinaiya_flutter/ui/widgets.dart';

import 'client_test.dart' show mockClient;
import 'concept_layout_test.dart' show openConverseChat, navigateTo;
import 'converse_quick_model_test.dart' as quick_fixtures;
import 'model_setup_test.dart' show SetupStorage;

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
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('What can we explore today?'), findsOneWidget);
      await tester.tap(find.text('Open Kinaiya setup'));
      await tester.pump();
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
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('What can we explore today?'), findsOneWidget);
      await tester.tap(find.text('Open Kinaiya setup'));
      await tester.pump();
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
      final trigger = find.byKey(const ValueKey('mascot-prompt-trigger'));
      await tester.tapAt(tester.getTopLeft(trigger) + const Offset(50, 50));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.text('Open Kinaiya setup'));
      await tester.pump();
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

  testWidgets(
    'mascot suggestions dismiss and preserve the conversation draft',
    (tester) async {
      tester.view.physicalSize = const Size(1440, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final state = wiredState()..setDraft(AppView.character, 'Existing draft');
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 300));
      final trigger = find.byKey(const ValueKey('mascot-prompt-trigger'));
      await tester.tapAt(tester.getTopLeft(trigger) + const Offset(50, 50));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('What can we explore today?'), findsOneWidget);
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('What can we explore today?'), findsNothing);
      expect(state.converseDraft, 'Existing draft');
      await tester.tapAt(tester.getTopLeft(trigger) + const Offset(50, 50));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.text('Help me think something through.'));
      await tester.pump(const Duration(milliseconds: 300));
      expect(
        state.converseDraft,
        'Existing draft\nHelp me think something through.',
      );
      expect(state.agentHistory, isEmpty);
      expect(state.converseHistory, isEmpty);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'Converse portrait suggestions preserve draft and dismiss outside',
    (tester) async {
      tester.view.physicalSize = const Size(1440, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final state = wiredState()
        ..setDraft(AppView.character, 'Work in progress');
      await tester.pumpWidget(KinaiyaApp(state: state));
      await tester.pump(const Duration(milliseconds: 300));
      await navigateTo(tester, AppView.character);
      final trigger = find.byKey(const ValueKey('mascot-prompt-trigger'));
      await tester.tap(trigger);
      await tester.pump();
      expect(find.text('What can we explore today?'), findsOneWidget);
      await tester.tapAt(const Offset(700, 80));
      await tester.pump();
      expect(find.text('What can we explore today?'), findsNothing);
      expect(state.converseDraft, 'Work in progress');
      expect(state.agentHistory, isEmpty);
      await tester.tap(trigger);
      await tester.pump();
      await tester.tap(find.text('Tell me something interesting.'));
      await tester.pump();
      expect(
        state.converseDraft,
        'Work in progress\nTell me something interesting.',
      );
      expect(state.converseHistory, isEmpty);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
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

  testWidgets(
    'Converse quick model picker dispatches save and settles cleanly when Agent is disabled',
    (tester) async {
      tester.view.physicalSize = const Size(1000, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      Map<String, Object?>? savedRequest;
      var postCount = 0;
      http.Client createTransport() => MockClient((request) async {
        if (request.url.path == '/v1/converse/options/model') {
          if (request.method == 'GET') {
            return quick_fixtures.jsonReply(
              quick_fixtures.snapshot(
                selection: savedRequest?['selection'] as Map<String, Object?>?,
                revision: savedRequest == null ? 0 : 1,
              ),
            );
          }
          if (request.method == 'POST') {
            postCount++;
            savedRequest = jsonDecode(request.body) as Map<String, Object?>;
            return quick_fixtures.jsonReply(
              quick_fixtures.receipt(savedRequest!),
            );
          }
        }
        if (request.url.path == '/health') {
          return quick_fixtures.jsonReply({
            'service': 'converse',
            'enabled': true,
            'provider': quick_fixtures.provider(
              'Ollama',
              savedRequest == null ? 'host-model' : 'alpha-model',
            ),
          });
        }
        return http.Response('unexpected ${request.url.path}', 500);
      });
      final client = KinaiyaClient(
        sessionBase: 'http://127.0.0.1:8760',
        agentBase: 'http://127.0.0.1:8761',
        converseBase: 'http://127.0.0.1:8762',
        ownerToken: 'test-token',
        httpClient: createTransport(),
        agentWorkspaceClientFactory: createTransport,
      );

      final state = AppState();
      state.connect(
        ConnectionProfile(
          sessionUrl: 'http://127.0.0.1:8760',
          agentUrl: 'http://127.0.0.1:8761',
          converseUrl: 'http://127.0.0.1:8762',
          ownerToken: 'test-token',
        ),
        injected: client,
      );
      // Simulate Converse-only connection with Agent unavailable/unconsented.
      state
        ..sessionAuthenticated = true
        ..agentOnline = false
        ..agentInferenceAllowed = false
        ..converseOnline = true
        ..converseInferenceAllowed = true
        ..converseProvider = quick_fixtures.provider('local', 'host-model')
        ..connected = true;

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: Center(
              child: ListenableBuilder(
                listenable: state,
                builder: (_, _) => ConverseModelSummary(state: state),
              ),
            ),
          ),
        ),
      );
      await tester.tap(
        find.byKey(const ValueKey('converse-model-summary-settings')),
      );
      await tester.pumpAndSettle();

      expect(find.text('Alpha'), findsOneWidget);
      await tester.tap(find.text('Alpha'));
      await tester.pumpAndSettle();

      expect(postCount, 1);
      expect((savedRequest?['selection'] as Map?)?['catalog_binding'], {
        'id': 'alpha',
        'generation': 1,
        'configuration_sha256':
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      });
      expect(state.converseInferenceAllowed, isTrue);
      expect(state.connected, isTrue);
      expect(state.converseProvider?['model'], 'alpha-model');

      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'Home panel preserves unscaled readable controls at minimum height with 2x text scale',
    (tester) async {
      tester.view.physicalSize = const Size(1200, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      var called = 0;
      var setupOpened = 0;
      var hiddenSetupOpened = 0;
      await tester.pumpWidget(
        MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: const TextScaler.linear(2.0)),
            child: child!,
          ),
          home: Scaffold(
            body: MoonlitHome(
              character: const SizedBox(width: 120, height: 120),
              status: 'Ready when you are',
              helperText:
                  'Long helper explanation that wraps across multiple lines when text scaling is doubled so the panel scrolls instead of shrinking text.',
              callLabel: 'Talk with Kinaiya',
              onCall: () => called++,
              onSetup: () => setupOpened++,
              onConverseSetup: () => hiddenSetupOpened++,
              showConverseView: false,
            ),
          ),
        ),
      );
      await tester.pump();

      final card = find.byKey(const ValueKey('home-action-card'));
      final topEdge = find.byKey(const ValueKey('home-panel-resize-top'));
      await tester.drag(topEdge, const Offset(0, 2000));
      await tester.pump();

      expect(tester.getSize(card).height, closeTo(120, 1));
      expect(find.byKey(const ValueKey('home-panel-scroll')), findsOneWidget);
      expect(
        find.ancestor(
          of: find.byKey(const ValueKey('home-setup')),
          matching: find.byType(FittedBox),
        ),
        findsNothing,
      );

      final callButton = find.byKey(const ValueKey('call-kinaiya'));
      final setupButton = find.byKey(const ValueKey('home-setup'));
      final hiddenButton = find.byKey(
        const ValueKey('home-converse-hidden-setup'),
      );
      expect(tester.getSize(callButton).height, greaterThanOrEqualTo(48));
      expect(tester.getSize(setupButton).height, greaterThanOrEqualTo(48));

      await tester.ensureVisible(callButton);
      await tester.pump();
      await tester.tap(callButton);
      expect(called, 1);

      await tester.ensureVisible(setupButton);
      await tester.pump();
      await tester.tap(setupButton);
      expect(setupOpened, 1);

      await tester.ensureVisible(hiddenButton);
      await tester.pump();
      await tester.tap(hiddenButton);
      expect(hiddenSetupOpened, 1);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'keyboard Tab reaches resize handles, scopes arrow keys to focused handles, and leaves focused buttons unchanged',
    (tester) async {
      tester.view.physicalSize = const Size(1200, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: MoonlitHome(
              character: const SizedBox(width: 120, height: 120),
              status: 'Ready when you are',
              callLabel: 'Talk with Kinaiya',
              onCall: () {},
              onSetup: () {},
            ),
          ),
        ),
      );
      await tester.pump();

      final card = find.byKey(const ValueKey('home-action-card'));
      final setupButton = find.byKey(const ValueKey('home-setup'));
      final rightHandle = find.byKey(
        const Key('home-panel-resize-handle-right'),
      );
      final bottomHandle = find.byKey(
        const Key('home-panel-resize-handle-bottom'),
      );
      final cornerHandle = find.byKey(
        const Key('home-panel-resize-handle-corner'),
      );

      // Tab onto the ordinary panel buttons (`call-kinaiya`, then `home-setup`).
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      expect(Focus.of(tester.element(setupButton)).hasFocus, isTrue);

      // Arrow keys while an ordinary panel button has focus must not resize the panel.
      final beforeButtonArrows = tester.getRect(card);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      expect(tester.getRect(card), beforeButtonArrows);

      // Next Tab enters the right resize handle.
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      expect(
        tester.widget<Focus>(rightHandle).focusNode!.hasPrimaryFocus,
        isTrue,
      );
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
      await tester.pump();
      final afterRightExpand = tester.getRect(card);
      expect(afterRightExpand.width, closeTo(beforeButtonArrows.width + 24, 1));
      expect(afterRightExpand.left, closeTo(beforeButtonArrows.left, 1));
      expect(afterRightExpand.height, closeTo(beforeButtonArrows.height, 1));

      // Next Tab enters the bottom resize handle.
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      expect(
        tester.widget<Focus>(bottomHandle).focusNode!.hasPrimaryFocus,
        isTrue,
      );
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      final afterBottomShrink = tester.getRect(card);
      expect(
        afterBottomShrink.height,
        closeTo(afterRightExpand.height - 24, 1),
      );
      expect(afterBottomShrink.top, closeTo(afterRightExpand.top, 1));

      // Next Tab enters the corner resize handle.
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      expect(
        tester.widget<Focus>(cornerHandle).focusNode!.hasPrimaryFocus,
        isTrue,
      );
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      final afterCornerRestore = tester.getRect(card);
      expect(afterCornerRestore.width, closeTo(beforeButtonArrows.width, 1));
      expect(afterCornerRestore.height, closeTo(beforeButtonArrows.height, 1));

      // Shift+Tab returns focus back through the handles to the panel button.
      await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
      await tester.pump();
      expect(Focus.of(tester.element(setupButton)).hasFocus, isTrue);
      expect(
        tester.widget<Focus>(rightHandle).focusNode!.hasPrimaryFocus,
        isFalse,
      );
    },
  );

  testWidgets(
    'short 868x340 window keeps all setup bubbles inside viewport and recovers hidden Converse view when disconnected',
    (tester) async {
      tester.view.physicalSize = const Size(868, 340);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final storage = SetupStorage();
      final presentation = PresentationController(storage: storage);
      // Earlier tests may cache bundled-asset futures inside the fake-async
      // zone; clear them so real-async initialization cannot wait forever.
      rootBundle.clear();
      await tester.runAsync(presentation.initialize);
      await tester.runAsync(
        () => presentation.setPreferences(
          presentation.preferences.copyWith(showConverseView: false),
        ),
      );
      final state = AppState();
      addTearDown(() {
        state.dispose();
        presentation.dispose();
      });

      await tester.pumpWidget(
        KinaiyaApp(state: state, presentation: presentation),
      );
      await tester.pump(const Duration(milliseconds: 300));

      // Hidden Converse notice is shown on Home even when disconnected/unavailable.
      final hiddenNotice = find.byKey(
        const ValueKey('home-converse-hidden-setup'),
      );
      await tester.ensureVisible(hiddenNotice);
      await tester.pump();
      await tester.tap(hiddenNotice);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));

      expect(find.byType(ModelSetupDialog), findsOneWidget);
      for (final section in [
        'Setup',
        'Model',
        'Reasoning',
        'Inputs',
        'Speech',
        'Decisions',
        'View',
        'Close',
      ]) {
        final rect = tester.getRect(
          find.byKey(ValueKey('setup-bubble-${section.toLowerCase()}')),
        );
        expect(rect.left, greaterThanOrEqualTo(0));
        expect(rect.top, greaterThanOrEqualTo(0));
        expect(rect.right, lessThanOrEqualTo(868));
        expect(rect.bottom, lessThanOrEqualTo(340));
      }

      final viewBubble = find.byKey(const Key('mascot-setup-bubble-view'));
      expect(viewBubble, findsOneWidget);
      final viewRect = tester.getRect(viewBubble);
      expect(viewRect.top, greaterThanOrEqualTo(0));
      expect(viewRect.bottom, lessThanOrEqualTo(340));

      await tester.tap(viewBubble);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      final toggle = find.byKey(const ValueKey('show-converse-view'));
      await tester.ensureVisible(toggle);
      await tester.pump();
      await tester.tap(toggle);
      await tester.pump();
      final saveButton = find.byKey(const ValueKey('save-model-setup'));
      await tester.ensureVisible(saveButton);
      await tester.pump();
      await tester.tap(saveButton);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));

      expect(presentation.preferences.showConverseView, isTrue);

      // Verify Options search terms and Character section switch for Converse page visibility.
      expect(
        searchOptionsPages('unhide converse').map((r) => r.page),
        contains(OptionsPage.appearance),
      );
      expect(
        searchOptionsPages('converse view').map((r) => r.page),
        containsAll([OptionsPage.appearance, OptionsPage.character]),
      );
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: PresentationSettings(
              controller: presentation,
              section: PresentationSection.character,
            ),
          ),
        ),
      );
      await tester.pump();
      final characterSwitch = find.byKey(
        const ValueKey('character-show-converse-page'),
      );
      expect(characterSwitch, findsOneWidget);
      await tester.ensureVisible(characterSwitch);
      await tester.pump();
      await tester.tap(characterSwitch);
      await tester.pump();
      expect(presentation.preferences.showConverseView, isFalse);
      // Let the settings save (and its bounded timeout) settle before teardown.
      await tester.runAsync(() => Future<void>.delayed(Duration.zero));
      await tester.pump();

      await tester.pumpWidget(const SizedBox.shrink());
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
