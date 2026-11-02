// First-run Home -> Connection and Options discovery regression suite (#309).
//
// Command:
//   flutter test --no-pub test/first_run_journey_test.dart
//
// Fixture provenance:
//   Uses deterministic offline synthetic state (`AppState`, `StageAudio`, and
//   `PresentationController` with in-memory storage). Performs zero live
//   network requests, zero model inference calls, zero secret reads, and zero
//   hardware microphone activations.
//
// Repeatable manual QA steps:
//   1. Launch Kinaiya without an active host connection. Verify Home highlights
//      `Connect / Setup` as the primary filled action, disables `Talk with Kinaiya`,
//      and displays actionable connection guidance without claiming readiness.
//   2. Activate `Connect / Setup` (click or keyboard Tab + Enter) and confirm
//      the real Connection setup screen opens with editable service URLs and
//      owner token fields.
//   3. Open Options, search for `mic`, `API key`, `captions`, and `reduced motion`,
//      open each destination, use `Back to search` / `All settings` breadcrumb
//      navigation, and close Options to return to Home.

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/presentation_settings.dart';
import 'package:kinaiya_flutter/ui/setup_dialog.dart';

import 'converse_stage_test.dart' show StageAudio;
import 'settings_navigation_test.dart' show NavigationStorage, routePump;

void main() {
  testWidgets(
    'First-run disconnected Home -> Connection setup -> Options synonym discovery -> back to Home (desktop)',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(1280, 800));
      addTearDown(() => tester.binding.setSurfaceSize(null));

      final state = AppState()
        ..connected = false
        ..sessionAuthenticated = false
        ..converseInferenceAllowed = false
        ..setDraft(AppView.character, 'Retained first-run draft');
      final audio = StageAudio();
      final storage = NavigationStorage();
      final presentation = PresentationController(storage: storage);
      await tester.runAsync(presentation.initialize);

      await tester.pumpWidget(
        KinaiyaApp(state: state, audio: audio, presentation: presentation),
      );
      await routePump(tester);

      // 1. Disconnected Home prioritizes Connect / Setup and shows actionable guidance without false ready claims.
      final homeSetup = find.byKey(const ValueKey('home-setup'));
      final homeTalk = find.byKey(const ValueKey('home-talk'));
      expect(homeSetup, findsOneWidget);
      expect(homeTalk, findsOneWidget);
      expect(
        find.descendant(of: homeSetup, matching: find.text('Connect / Setup')),
        findsOneWidget,
      );
      expect(
        tester
            .widget<ButtonStyleButton>(
              find.descendant(
                of: homeTalk,
                matching: find.bySubtype<ButtonStyleButton>(),
              ),
            )
            .onPressed,
        isNull,
        reason: 'Call button must stay disabled when disconnected.',
      );
      expect(
        find.text(
          'Not connected to a Kinaiya host. Open Connect / Setup to enter your host URLs and owner token.',
        ),
        findsOneWidget,
      );
      expect(find.text('Ready when you are'), findsNothing);
      expect(find.text('Ready to talk'), findsNothing);

      // 2. Activating Connect / Setup opens the real Connection setup in Options.
      await tester.ensureVisible(homeSetup);
      await tester.tap(homeSetup);
      await routePump(tester);
      expect(find.byType(SetupDialog), findsOneWidget);
      expect(find.text('Session service URL'), findsOneWidget);
      expect(
        tester
            .widget<Text>(find.byKey(const ValueKey('options-breadcrumb-page')))
            .data,
        'Connection',
      );
      await tester.tap(find.byKey(const ValueKey('settings-close')));
      await routePump(tester);
      expect(find.byType(SetupDialog), findsNothing);

      // 3. Open Options and discover destinations for mic, API key, captions, and reduced motion.
      await tester.tap(find.byKey(const ValueKey('settings-open')));
      await routePump(tester);

      final search = find.byKey(const ValueKey('settings-search'));
      expect(search, findsOneWidget);

      // Query: mic -> Voice
      await tester.enterText(search, 'mic');
      await tester.pump();
      final voiceResult = find.byKey(const ValueKey('options-search-voice'));
      expect(voiceResult, findsOneWidget);
      await tester.tap(voiceResult);
      await routePump(tester);
      expect(
        tester
            .widget<Text>(find.byKey(const ValueKey('options-breadcrumb-page')))
            .data,
        'Voice',
      );
      expect(
        find.byKey(const ValueKey('options-breadcrumb-return-search')),
        findsOneWidget,
      );

      // Query: API key -> Connection
      await tester.ensureVisible(search);
      await tester.pump();
      await tester.enterText(search, 'API key');
      await tester.pump();
      expect(
        find.byKey(const ValueKey('options-search-providers')),
        findsOneWidget,
      );
      final connectionResult = find.byKey(
        const ValueKey('options-search-connection'),
      );
      expect(connectionResult, findsOneWidget);
      await tester.tap(connectionResult);
      await routePump(tester);
      expect(find.byType(SetupDialog), findsOneWidget);

      // Query: captions -> Accessibility
      await tester.ensureVisible(search);
      await tester.pump();
      await tester.enterText(search, 'captions');
      await tester.pump();
      final captionsResult = find.byKey(
        const ValueKey('options-search-accessibility'),
      );
      expect(captionsResult, findsOneWidget);
      await tester.tap(captionsResult);
      await routePump(tester);
      expect(find.byType(PresentationSettings), findsOneWidget);

      // Query: reduced motion -> Accessibility, then return to All settings overview.
      await tester.ensureVisible(search);
      await tester.pump();
      await tester.enterText(search, 'reduced motion');
      await tester.pump();
      expect(
        find.byKey(const ValueKey('options-search-accessibility')),
        findsOneWidget,
      );
      await tester.tap(find.byTooltip('Clear search'));
      await tester.pump();
      await tester.tap(
        find.byKey(const ValueKey('options-breadcrumb-all-settings')),
      );
      await routePump(tester);
      expect(
        find.byKey(const ValueKey('options-breadcrumb-bar')),
        findsNothing,
      );

      // 4. Close Options and verify we are back on Home with draft intact and zero mic/network side effects.
      await tester.tap(find.byKey(const ValueKey('settings-close')));
      await routePump(tester);
      expect(find.byKey(const ValueKey('home-setup')), findsOneWidget);
      expect(state.converseDraft, 'Retained first-run draft');
      expect(audio.arms, 0);
      expect(audio.recordStarts, 0);
      expect(storage.writes, 0);
      expect(tester.takeException(), isNull);

      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      presentation.dispose();
    },
  );

  testWidgets(
    'First-run journey remains reachable at 320x480 with 2x text scaling and keyboard focus',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(320, 480));
      tester.platformDispatcher.textScaleFactorTestValue = 2.0;
      addTearDown(() {
        tester.platformDispatcher.clearTextScaleFactorTestValue();
        return tester.binding.setSurfaceSize(null);
      });

      final state = AppState()
        ..connected = false
        ..sessionAuthenticated = false
        ..converseInferenceAllowed = false;
      final audio = StageAudio();
      final storage = NavigationStorage();
      final presentation = PresentationController(storage: storage);
      await tester.runAsync(presentation.initialize);

      await tester.pumpWidget(
        KinaiyaApp(state: state, audio: audio, presentation: presentation),
      );
      await routePump(tester);

      // Keyboard focus traversal reaches primary Connect / Setup control on Home.
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      final homeSetup = find.byKey(const ValueKey('home-setup'));
      await tester.ensureVisible(homeSetup);
      await tester.pump();
      expect(homeSetup.hitTestable(), findsOneWidget);

      // Open Options on 320x480 @ 2x text, open navigation drawer, search for microphone, and return to Home.
      await tester.tap(find.byKey(const ValueKey('settings-open')));
      await routePump(tester);
      await tester.tap(find.byTooltip('Options navigation'));
      await routePump(tester);

      final search = find.byKey(const ValueKey('settings-search'));
      await tester.enterText(search, 'microphone');
      await tester.pump();
      final voiceResult = find.byKey(const ValueKey('options-search-voice'));
      await tester.ensureVisible(voiceResult);
      await tester.pump();
      await tester.tap(voiceResult);
      await routePump(tester);

      expect(
        find.byKey(const ValueKey('options-breadcrumb-all-settings')),
        findsOneWidget,
      );
      await tester.tap(
        find.byKey(const ValueKey('options-breadcrumb-all-settings')),
      );
      await routePump(tester);
      await tester.tap(find.byKey(const ValueKey('settings-close')));
      await routePump(tester);

      expect(find.byKey(const ValueKey('home-setup')), findsOneWidget);
      expect(audio.arms, 0);
      expect(audio.recordStarts, 0);
      expect(tester.takeException(), isNull);

      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      presentation.dispose();
    },
  );
}
