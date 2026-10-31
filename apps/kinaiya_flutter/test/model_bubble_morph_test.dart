import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';

import 'model_setup_test.dart' show SetupStorage, setupFixture, pumpSetupFrames;

Finder bubble(String name) => find.byKey(ValueKey('setup-bubble-$name'));
final surface = find.byKey(const ValueKey('setup-morph-surface'));

void main() {
  Future<PresentationController> mount(WidgetTester tester) async {
    await tester.binding.setSurfaceSize(const Size(1536, 1024));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final requests = <http.Request>[];
    final state = AppState()
      ..client = KinaiyaClient(
        sessionBase: 'http://synthetic',
        agentBase: 'http://synthetic',
        converseBase: 'http://synthetic',
        ownerToken: 'synthetic-owner',
        httpClient: MockClient((request) async {
          requests.add(request);
          return http.Response(jsonEncode(setupFixture()), 200);
        }),
      );
    final presentation = PresentationController(storage: SetupStorage());
    await tester.runAsync(presentation.initialize);
    addTearDown(() async {
      expect(
        requests.every(
          (r) => r.method == 'GET' && r.url.path == '/v1/converse/setup',
        ),
        isTrue,
      );
      expect(state.agentSession, isNull);
      expect(state.converseSession, isNull);
      expect(state.converseHistory, isEmpty);
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      presentation.dispose();
    });
    await tester.pumpWidget(
      MaterialApp(
        home: ModelSetupDialog(state: state, presentation: presentation),
      ),
    );
    await pumpSetupFrames(tester);
    return presentation;
  }

  testWidgets(
    'eight bubbles replace fixed cards and expand from the clicked bounds',
    (tester) async {
      await mount(tester);
      for (final name in [
        'setup',
        'model',
        'reasoning',
        'inputs',
        'speech',
        'decisions',
        'view',
        'close',
      ]) {
        expect(bubble(name).hitTestable(), findsOneWidget);
      }
      expect(find.byKey(const ValueKey('setup-fixed-header')), findsNothing);
      expect(find.byKey(const ValueKey('show-converse-view')), findsNothing);
      final origin = tester.getRect(bubble('model'));
      await tester.tap(bubble('model'));
      await tester.pump();
      expect(tester.getRect(surface), origin);
      await tester.pump(const Duration(milliseconds: 100));
      final middle = tester.getRect(surface);
      expect(middle.width, greaterThan(origin.width));
      expect(middle.width, lessThan(440));
      expect(tester.getRect(bubble('model')), origin);
      await tester.pump(const Duration(milliseconds: 180));
      final expanded = tester.getRect(surface);
      expect(expanded.width, 440);
      expect(find.text('Model settings').hitTestable(), findsOneWidget);
      final neighbor = tester.getTopLeft(bubble('speech'));
      await pumpSetupFrames(tester);
      expect(tester.getTopLeft(bubble('speech')), isNot(neighbor));
      expect(tester.getRect(surface), expanded);
      await tester.tap(find.byTooltip('Close settings popover'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 120));
      final shrinking = tester.getRect(surface);
      expect(shrinking.width, lessThan(expanded.width));
      expect(shrinking.width, greaterThan(origin.width));
      await pumpSetupFrames(tester);
      expect(surface, findsNothing);
      expect(tester.getRect(bubble('model')), origin);
      expect(bubble('model').hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('late pointer release after setup dismissal is harmless', (
    tester,
  ) async {
    await mount(tester);
    final pointer = await tester.startGesture(
      tester.getCenter(bubble('model')),
    );
    await tester.pump();
    await tester.pumpWidget(const SizedBox.shrink());
    await pointer.up();
    await tester.pump();
    expect(tester.takeException(), isNull);
  });

  testWidgets('Escape during opening reverses the morph and retains setup', (
    tester,
  ) async {
    await mount(tester);
    await tester.tap(bubble('model'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));
    expect(surface, findsOneWidget);
    expect(tester.getSize(surface).width, lessThan(440));
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await pumpSetupFrames(tester);
    expect(surface, findsNothing);
    expect(find.byType(ModelSetupDialog), findsOneWidget);
    expect(bubble('model').hitTestable(), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  for (final mode in ['reduced', 'disabled', 'OS']) {
    testWidgets('$mode opens/closes without animated intermediate geometry', (
      tester,
    ) async {
      final presentation = await mount(tester);
      addTearDown(
        tester.platformDispatcher.clearAccessibilityFeaturesTestValue,
      );
      if (mode == 'OS') {
        tester.platformDispatcher.accessibilityFeaturesTestValue =
            const FakeAccessibilityFeatures(disableAnimations: true);
      } else {
        await presentation.setPreferences(
          presentation.preferences.copyWith(
            reducedMotion: mode == 'reduced',
            animateTransitions: mode != 'disabled',
          ),
        );
      }
      await tester.pump();
      await tester.tap(bubble('view'));
      await tester.pump();
      expect(tester.getSize(surface).width, 440);
      expect(
        find.byKey(const ValueKey('show-converse-view')).hitTestable(),
        findsOneWidget,
      );
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pump();
      expect(surface, findsNothing);
      expect(bubble('view').hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets(
    'turning motion off mid-transition completes the current open or close',
    (tester) async {
      final presentation = await mount(tester);
      await tester.tap(bubble('model'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 80));
      expect(tester.getSize(surface).width, lessThan(440));
      await presentation.setPreferences(
        presentation.preferences.copyWith(reducedMotion: true),
      );
      await tester.pump();
      expect(tester.getSize(surface).width, 440);
      await tester.pump();
      await presentation.setPreferences(
        presentation.preferences.copyWith(reducedMotion: false),
      );
      await tester.tap(find.byTooltip('Close settings popover'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 80));
      expect(surface, findsOneWidget);
      await presentation.setPreferences(
        presentation.preferences.copyWith(animateTransitions: false),
      );
      await tester.pump();
      expect(surface, findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'resizing an open modal retains reachable content and closes to the new circle',
    (tester) async {
      await mount(tester);
      await tester.tap(bubble('speech'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      await tester.binding.setSurfaceSize(const Size(320, 640));
      await pumpSetupFrames(tester);
      final rect = tester.getRect(surface);
      expect(rect.left, greaterThanOrEqualTo(0));
      expect(rect.right, lessThanOrEqualTo(320));
      expect(
        find.byTooltip('Close settings popover').hitTestable(),
        findsOneWidget,
      );
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await pumpSetupFrames(tester);
      expect(surface, findsNothing);
      await tester.ensureVisible(bubble('speech'));
      await tester.pump();
      expect(bubble('speech').hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}
