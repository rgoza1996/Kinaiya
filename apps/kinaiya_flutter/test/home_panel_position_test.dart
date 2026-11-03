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
    'connection panel resizes like a window from all sides and corners while respecting bounds',
    (tester) async {
      tester.view.physicalSize = const Size(1200, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      var opened = 0;
      await tester.pumpWidget(home(onSetup: () => opened++));
      await tester.pump();
      final card = find.byKey(const ValueKey('home-action-card'));
      final leftEdge = find.byKey(const ValueKey('home-panel-resize-left'));
      final rightEdge = find.byKey(const ValueKey('home-panel-resize-right'));
      final topEdge = find.byKey(const ValueKey('home-panel-resize-top'));
      final bottomEdge = find.byKey(const ValueKey('home-panel-resize-bottom'));
      final topLeftCorner = find.byKey(
        const ValueKey('home-panel-resize-top-left'),
      );

      // 1. Right side: drag right to expand, drag left to shrink; left edge stays anchored.
      final start = tester.getRect(card);
      await tester.drag(rightEdge, const Offset(140, 0));
      await tester.pump();
      final rightExpanded = tester.getRect(card);
      expect(rightExpanded.width, greaterThan(start.width + 100));
      expect(rightExpanded.left, closeTo(start.left, 1));
      expect(rightExpanded.bottom, closeTo(start.bottom, 1));

      await tester.drag(rightEdge, const Offset(-80, 0));
      await tester.pump();
      final rightShrunk = tester.getRect(card);
      expect(rightShrunk.width, lessThan(rightExpanded.width - 50));
      expect(rightShrunk.left, closeTo(rightExpanded.left, 1));

      // 2. Left side: drag left to expand, drag right to shrink; right edge stays anchored.
      await tester.drag(leftEdge, const Offset(-120, 0));
      await tester.pump();
      final leftExpanded = tester.getRect(card);
      expect(leftExpanded.width, greaterThan(rightShrunk.width + 80));
      expect(leftExpanded.right, closeTo(rightShrunk.right, 1));

      await tester.drag(leftEdge, const Offset(90, 0));
      await tester.pump();
      final leftShrunk = tester.getRect(card);
      expect(leftShrunk.width, lessThan(leftExpanded.width - 60));
      expect(leftShrunk.right, closeTo(leftExpanded.right, 1));

      // 3. Top side: drag up to make taller, drag down to make shorter; bottom edge stays anchored.
      await tester.drag(topEdge, const Offset(0, -100));
      await tester.pump();
      final topExpanded = tester.getRect(card);
      expect(topExpanded.height, greaterThan(leftShrunk.height + 70));
      expect(topExpanded.bottom, closeTo(leftShrunk.bottom, 1));

      await tester.drag(topEdge, const Offset(0, 80));
      await tester.pump();
      final topShrunk = tester.getRect(card);
      expect(topShrunk.height, lessThan(topExpanded.height - 40));
      expect(topShrunk.bottom, closeTo(topExpanded.bottom, 1));

      // 4. Bottom side: drag up to make shorter, drag down to make taller; top edge stays anchored.
      await tester.drag(bottomEdge, const Offset(0, -80));
      await tester.pump();
      final bottomShrunk = tester.getRect(card);
      expect(bottomShrunk.height, lessThan(topShrunk.height - 40));
      expect(bottomShrunk.top, closeTo(topShrunk.top, 1));

      await tester.drag(bottomEdge, const Offset(0, 70));
      await tester.pump();
      final bottomExpanded = tester.getRect(card);
      expect(bottomExpanded.height, greaterThan(bottomShrunk.height + 30));
      expect(bottomExpanded.top, closeTo(bottomShrunk.top, 1));

      // 5. Corner resize: top-left corner adjusts both width and height while anchoring bottom-right.
      await tester.drag(topLeftCorner, const Offset(-80, -70));
      await tester.pump();
      final cornerExpanded = tester.getRect(card);
      expect(cornerExpanded.width, greaterThan(bottomExpanded.width + 40));
      expect(cornerExpanded.height, greaterThan(bottomExpanded.height + 30));
      expect(cornerExpanded.right, closeTo(bottomExpanded.right, 1));
      expect(cornerExpanded.bottom, closeTo(bottomExpanded.bottom, 1));

      // 6. Rapid multi-step moves within the same frame.
      final gesture = await tester.startGesture(tester.getCenter(rightEdge));
      await gesture.moveBy(const Offset(30, 0));
      await tester.pump();
      final beforeRapidMoves = tester.getSize(card).width;
      await gesture.moveBy(const Offset(30, 0));
      await gesture.moveBy(const Offset(30, 0));
      await tester.pump();
      expect(tester.getSize(card).width, closeTo(beforeRapidMoves + 60, 1));
      await gesture.up();

      // 7. Keyboard arrow-key resizing.
      final beforeKey = tester.getSize(card).width;
      await tester.tap(rightEdge);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowLeft);
      await tester.pump();
      expect(tester.getSize(card).width, closeTo(beforeKey - 24, 1));

      // 8. Minimum and maximum bounds clamping and viewport shrink safety.
      await tester.drag(rightEdge, const Offset(-2000, 0));
      await tester.pump();
      expect(tester.getSize(card).width, closeTo(320, 1));
      await tester.drag(topEdge, const Offset(0, 2000));
      await tester.pump();
      expect(tester.getSize(card).height, closeTo(120, 1));

      await tester.ensureVisible(find.byKey(const ValueKey('home-setup')));
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('home-setup')));
      expect(opened, 1);

      await tester.drag(rightEdge, const Offset(3000, 0));
      await tester.pump();
      await tester.pump();
      expect(tester.getRect(card).left, greaterThanOrEqualTo(7));
      expect(tester.getRect(card).right, lessThanOrEqualTo(1193));

      tester.view.physicalSize = const Size(500, 800);
      await tester.pump();
      await tester.pump();
      expect(tester.getRect(card).right, lessThanOrEqualTo(493));
      expect(tester.takeException(), isNull);
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

  testWidgets(
    'Home displays actionable prerequisite and connection recovery copy without repeating liveRegion announcements',
    (tester) async {
      final semantics = tester.ensureSemantics();
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final helperFinder = find.byKey(const ValueKey('home-helper-copy'));
      final audio = LayoutAudio();

      // 1. Disconnected (no error yet): directs to Connect / Setup.
      final disconnected = AppState();
      await tester.pumpWidget(KinaiyaApp(state: disconnected, audio: audio));
      await tester.pump(const Duration(milliseconds: 200));
      expect(helperFinder, findsOneWidget);
      expect(
        find.text(
          'Not connected to a Kinaiya host. Open Connect / Setup to enter your host URLs and owner token.',
        ),
        findsOneWidget,
      );

      // 2. Known connection failure: shows concise cause and next real action.
      disconnected
        ..connectionError = 'Session timeout on 127.0.0.1:8760'
        ..touch();
      await tester.pump(const Duration(milliseconds: 100));
      expect(
        find.text(
          'Connection failed: Session timeout on 127.0.0.1:8760. Open Connect / Setup to check your host URLs and owner token, then retry.',
        ),
        findsOneWidget,
      );
      await tester.pumpWidget(const SizedBox.shrink());
      disconnected.dispose();

      // 3. Connected with missing prerequisite (model/disclosure not yet qualified).
      final api = SleepApi();
      final state = wired(api);
      state
        ..converseProvider = {'provider': 'ollama', 'model': 'llama3'}
        ..converseInferenceAllowed = false;
      await pumpHome(tester, state, audio);
      expect(
        find.text(
          'Prerequisite needed: select a qualified Converse model and confirm privacy disclosure in Setup.',
        ),
        findsOneWidget,
      );

      // 4. Connecting / pending control operation.
      state
        ..converseInferenceAllowed = true
        ..converseControlPending = true
        ..converseControlTarget = true;
      await pumpHome(tester, state, audio);
      expect(
        find.text('Waiting for the host to confirm Converse is awake.'),
        findsOneWidget,
      );

      // 5. Unknown / uncertain state stays explicitly unknown and points to Check host status.
      state
        ..converseControlPending = false
        ..converseControlUncertain = true;
      await pumpHome(tester, state, audio);
      expect(
        find.text(
          'Converse status is unconfirmed. Use Check host status below or open Setup.',
        ),
        findsOneWidget,
      );
      expect(find.byKey(const ValueKey('home-owner-refresh')), findsOneWidget);

      state
        ..converseControlUncertain = false
        ..resourceStatusStale = true;
      await pumpHome(tester, state, audio);
      expect(
        find.text(
          'Resource status is unconfirmed; voice readiness is unknown until the host responds.',
        ),
        findsOneWidget,
      );

      // Verify helper semantics do not mark liveRegion (no endless repeat on polling).
      final node = tester.getSemantics(helperFinder);
      expect(node.getSemanticsData().flagsCollection.isLiveRegion, isFalse);

      // 6. Ready state clears prerequisite helper copy.
      state.resourceStatusStale = false;
      state.applyConverseHealth(availability(true, 1));
      await pumpHome(tester, state, audio);
      expect(helperFinder, findsNothing);

      // 7. 320px at 2x text scale renders helper copy without overflow.
      tester.view.physicalSize = const Size(320, 568);
      tester.platformDispatcher.textScaleFactorTestValue = 2;
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      state.converseInferenceAllowed = false;
      await pumpHome(tester, state, audio);
      expect(helperFinder, findsOneWidget);
      expect(tester.takeException(), isNull);

      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
      semantics.dispose();
    },
  );

  testWidgets(
    'Home action panel strengthens contrast over moonlit artwork and keeps controls reachable across desktop and 320px 2x',
    (tester) async {
      double contrastRatio(Color a, Color b) {
        final x = a.computeLuminance(), y = b.computeLuminance();
        return (x > y ? x + .05 : y + .05) / (x > y ? y + .05 : x + .05);
      }

      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final api = SleepApi();
      final state = wired(api);
      final audio = LayoutAudio();
      await pumpHome(tester, state, audio);

      final cardFinder = find.byKey(const ValueKey('home-action-card'));
      expect(cardFinder, findsOneWidget);
      final cardContainer = tester.widget<Container>(cardFinder);
      final decoration = cardContainer.decoration! as BoxDecoration;
      final theme = Theme.of(tester.element(cardFinder));
      expect(decoration.color, theme.colorScheme.surfaceContainer);
      expect(
        contrastRatio(decoration.color!, theme.colorScheme.onSurface),
        greaterThanOrEqualTo(4.5),
      );
      expect(decoration.boxShadow, isNotEmpty);

      // Start Call so End conversation, Setup, and owner control are all present.
      await tester.tap(find.byKey(const ValueKey('call-kinaiya')));
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.text('End conversation').hitTestable(), findsOneWidget);
      expect(
        find.byKey(const ValueKey('home-setup')).hitTestable(),
        findsOneWidget,
      );
      expect(
        find.byKey(const ValueKey('home-owner-control')).hitTestable(),
        findsOneWidget,
      );

      // Verify 320px at 2x text has no horizontal scroll, overlap, or covered controls.
      tester.view.physicalSize = const Size(320, 568);
      tester.platformDispatcher.textScaleFactorTestValue = 2;
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      await tester.pump(const Duration(milliseconds: 100));

      final panelRect = tester.getRect(
        find.byKey(const ValueKey('home-status-panel')),
      );
      expect(panelRect.left, greaterThanOrEqualTo(8));
      expect(panelRect.right, lessThanOrEqualTo(312));
      await tester.ensureVisible(find.byKey(const ValueKey('call-kinaiya')));
      await tester.pump();
      expect(
        find.byKey(const ValueKey('call-kinaiya')).hitTestable(),
        findsOneWidget,
      );
      await tester.ensureVisible(find.byKey(const ValueKey('home-setup')));
      await tester.pump();
      expect(
        find.byKey(const ValueKey('home-setup')).hitTestable(),
        findsOneWidget,
      );
      await tester.ensureVisible(
        find.byKey(const ValueKey('home-owner-control')),
      );
      await tester.pump();
      expect(
        find.byKey(const ValueKey('home-owner-control')).hitTestable(),
        findsOneWidget,
      );
      expect(tester.takeException(), isNull);

      await tester.pumpWidget(const SizedBox.shrink());
      state.dispose();
    },
  );
}
