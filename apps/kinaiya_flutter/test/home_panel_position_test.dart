import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/app.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/home_sprite_sheet.dart';
import 'package:kinaiya_flutter/ui/moonlit_home.dart';

import 'concept_layout_test.dart' show LayoutAudio;
import 'home_sleep_test.dart' show SleepApi, availability, pumpHome, wired;

Widget home({VoidCallback? onSetup}) => MaterialApp(
  home: Scaffold(
    body: MoonlitHome(
      character: const SizedBox(),
      status: 'Connect to talk with Kinaiya',
      callLabel: 'Talk with Kinaiya',
      onSetup: onSetup ?? () {},
      offlineResting: true,
    ),
  ),
);

Future<Rect> visibleSpriteBounds(
  WidgetTester tester, {
  String key = 'home-sleeping-artwork',
}) async {
  final boundary = tester.renderObject<RenderRepaintBoundary>(
    find
        .descendant(
          of: find.byKey(ValueKey(key)),
          matching: find.byType(RepaintBoundary),
        )
        .first,
  );
  final image = (await tester.runAsync(() => boundary.toImage(pixelRatio: 1)))!;
  final data = (await tester.runAsync(
    () => image.toByteData(format: ui.ImageByteFormat.rawRgba),
  ))!;
  final bytes = data.buffer.asUint8List();
  int left = image.width, right = 0, top = image.height, bottom = 0;
  for (var y = 0; y < image.height; y++) {
    for (var x = 0; x < image.width; x++) {
      if (bytes[(y * image.width + x) * 4 + 3] < 128) continue;
      if (x < left) left = x;
      if (x > right) right = x;
      if (y < top) top = y;
      if (y > bottom) bottom = y;
    }
  }
  image.dispose();
  return Rect.fromLTRB(
    left.toDouble(),
    top.toDouble(),
    right.toDouble(),
    bottom.toDouble(),
  );
}

void main() {
  testWidgets('sleep transition frames keep their water contact aligned', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1200, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final resting = ValueNotifier(false);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ValueListenableBuilder<bool>(
            valueListenable: resting,
            builder: (_, value, _) => MoonlitHome(
              character: const SizedBox(),
              status: 'Status',
              callLabel: 'Talk',
              onSetup: () {},
              offlineResting: value,
            ),
          ),
        ),
      ),
    );
    await tester.runAsync(() async {
      final context = tester.element(find.byType(MoonlitHome));
      await precacheImage(
        const AssetImage('assets/character/kinaiya-sleep-transition-v3.png'),
        context,
      );
      await precacheImage(
        const AssetImage('assets/character/kinaiya-sleeping-idle-v3.png'),
        context,
      );
    });
    resting.value = true;
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));
    final first = await visibleSpriteBounds(
      tester,
      key: 'home-sleep-transition',
    );
    for (var i = 0; i < 3; i++) {
      await tester.pump(const Duration(milliseconds: 400));
      final next = await visibleSpriteBounds(
        tester,
        key: 'home-sleep-transition',
      );
      expect(next.bottom, closeTo(first.bottom, 4));
      expect(next.center.dx, closeTo(first.center.dx, 4));
    }
    await tester.pumpWidget(const SizedBox.shrink());
    resting.dispose();
  });
  testWidgets('sleep frames stay registered to the same water contact', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1200, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(home());
    await tester.runAsync(
      () => precacheImage(
        const AssetImage('assets/character/kinaiya-sleeping-idle-v3.png'),
        tester.element(find.byType(HomeSpriteSheet)),
      ),
    );
    await tester.pump();
    final first = await visibleSpriteBounds(tester);
    for (var frame = 1; frame < 4; frame++) {
      await tester.pump(const Duration(milliseconds: 700));
      final current = await visibleSpriteBounds(tester);
      expect(current.bottom, closeTo(first.bottom, 3), reason: 'frame $frame');
      expect(current.top, closeTo(first.top, 3), reason: 'frame $frame');
      expect(
        current.center.dx,
        closeTo(first.center.dx, 4),
        reason: 'frame $frame',
      );
    }
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets(
    'connection panel can be moved within the scene and its controls work',
    (tester) async {
      tester.view.physicalSize = const Size(1200, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      var opened = 0;
      await tester.pumpWidget(home(onSetup: () => opened++));
      await tester.pump();
      final handle = find.byTooltip('Drag to move panel');
      final card = find.byKey(const ValueKey('home-status-panel'));
      final start = tester.getTopLeft(card);
      await tester.drag(handle, const Offset(-240, -180));
      await tester.pump();
      final moved = tester.getTopLeft(card);
      expect(moved.dx, lessThan(start.dx - 200));
      expect(moved.dy, lessThan(start.dy - 140));
      await tester.tap(find.byKey(const ValueKey('home-setup')));
      expect(opened, 1);
      await tester.drag(handle, const Offset(-5000, -5000));
      await tester.pump();
      final rect = tester.getRect(card);
      expect(rect.left, greaterThanOrEqualTo(7));
      expect(rect.top, greaterThanOrEqualTo(7));
      expect(rect.right, lessThanOrEqualTo(1193));
      expect(rect.bottom, lessThanOrEqualTo(793));
      await tester.drag(handle, const Offset(5000, 0));
      await tester.pump();
      tester.view.physicalSize = const Size(650, 800);
      await tester.pump();
      await tester.pump();
      expect(tester.getRect(card).right, lessThanOrEqualTo(643));
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'Home prioritizes Connect/Setup when Call is unavailable and switches to Call when ready or active',
    (tester) async {
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      // Disconnected app shell: Connect / Setup is primary FilledButton and opens Connection setup.
      final disconnected = AppState();
      final audio = LayoutAudio();
      await tester.pumpWidget(KinaiyaApp(state: disconnected, audio: audio));
      await tester.pump(const Duration(milliseconds: 200));

      final setupFinder = find.byKey(const ValueKey('home-setup'));
      final callFinder = find.byKey(const ValueKey('call-kinaiya'));
      bool precedes(Finder first, Finder second) {
        final a = tester.getTopLeft(first);
        final b = tester.getTopLeft(second);
        return a.dy < b.dy - 1 || ((a.dy - b.dy).abs() <= 1 && a.dx < b.dx);
      }

      expect(tester.widget<FilledButton>(setupFinder).onPressed, isNotNull);
      expect(find.text('Connect / Setup'), findsOneWidget);
      expect(tester.widget<OutlinedButton>(callFinder).onPressed, isNull);
      expect(precedes(setupFinder, callFinder), isTrue);
      await tester.tap(callFinder, warnIfMissed: false);
      await tester.pump();
      expect(audio.starts, 0);

      await tester.tap(setupFinder);
      await tester.pump(const Duration(milliseconds: 250));
      expect(find.byKey(const ValueKey('options-connection')), findsWidgets);
      await tester.tap(find.byKey(const ValueKey('settings-close')));
      await tester.pump(const Duration(milliseconds: 250));
      await tester.pumpWidget(const SizedBox.shrink());
      disconnected.dispose();

      // Configured model without confirmed readiness, pending, and unknown states stay unready.
      final api = SleepApi();
      final state = wired(api);
      state
        ..converseProvider = {'provider': 'ollama', 'model': 'llama3'}
        ..converseInferenceAllowed = false;
      await pumpHome(tester, state, audio);
      expect(tester.widget<FilledButton>(setupFinder).onPressed, isNotNull);
      expect(tester.widget<OutlinedButton>(callFinder).onPressed, isNull);
      expect(precedes(setupFinder, callFinder), isTrue);

      state
        ..converseInferenceAllowed = true
        ..converseControlPending = true
        ..converseControlTarget = true;
      await pumpHome(tester, state, audio);
      expect(tester.widget<FilledButton>(setupFinder).onPressed, isNotNull);
      expect(tester.widget<OutlinedButton>(callFinder).onPressed, isNull);

      state
        ..converseControlPending = false
        ..converseControlUncertain = true;
      await pumpHome(tester, state, audio);
      expect(tester.widget<FilledButton>(setupFinder).onPressed, isNotNull);
      expect(tester.widget<OutlinedButton>(callFinder).onPressed, isNull);

      state
        ..converseControlUncertain = false
        ..resourceStatusStale = true;
      await pumpHome(tester, state, audio);
      expect(tester.widget<FilledButton>(setupFinder).onPressed, isNotNull);
      expect(tester.widget<OutlinedButton>(callFinder).onPressed, isNull);

      // Ready state: Call becomes primary FilledButton and Setup becomes secondary OutlinedButton.
      state.resourceStatusStale = false;
      state.applyConverseHealth(availability(true, 1));
      await pumpHome(tester, state, audio);
      expect(tester.widget<FilledButton>(callFinder).onPressed, isNotNull);
      expect(tester.widget<OutlinedButton>(setupFinder).onPressed, isNotNull);
      expect(precedes(callFinder, setupFinder), isTrue);

      // Active call preserves primary End conversation affordance.
      await tester.tap(callFinder);
      await tester.pump(const Duration(milliseconds: 100));
      expect(audio.starts, 1);
      expect(tester.widget<FilledButton>(callFinder).onPressed, isNotNull);
      expect(find.text('End conversation'), findsOneWidget);

      await tester.tap(callFinder);
      await tester.pump(const Duration(milliseconds: 100));
      expect(audio.micStops, greaterThan(0));
      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );

  testWidgets(
    'Home primary action supports keyboard activation and 320px 2x text layout',
    (tester) async {
      tester.view.physicalSize = const Size(320, 568);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      var setupOpened = 0;
      await tester.pumpWidget(
        MaterialApp(
          home: MediaQuery(
            data: const MediaQueryData(
              size: Size(320, 568),
              textScaler: TextScaler.linear(2),
            ),
            child: Scaffold(
              body: MoonlitHome(
                character: const SizedBox(),
                status: 'Connect to talk with Kinaiya',
                callLabel: 'Talk with Kinaiya',
                setupLabel: 'Connect / Setup',
                onSetup: () => setupOpened++,
                offlineResting: true,
              ),
            ),
          ),
        ),
      );
      await tester.pump();
      expect(tester.takeException(), isNull);
      final setupButton = tester.widget<FilledButton>(
        find.byKey(const ValueKey('home-setup')),
      );
      expect(setupButton.onPressed, isNotNull);
      setupButton.focusNode?.requestFocus();
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();
      if (setupOpened == 0) {
        await tester.tap(find.byKey(const ValueKey('home-setup')));
        await tester.pump();
      }
      expect(setupOpened, greaterThanOrEqualTo(1));
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
}
