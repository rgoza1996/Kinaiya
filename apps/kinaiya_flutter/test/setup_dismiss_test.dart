import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/gestures.dart';
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

const names = [
  'Setup',
  'Model',
  'Reasoning',
  'Inputs',
  'Speech',
  'Decisions',
  'View',
  'Close',
];
Finder bubble(String name) =>
    find.byKey(ValueKey('setup-bubble-${name.toLowerCase()}'));
final dialog = find.byType(ModelSetupDialog);
final panel = find.byKey(const ValueKey('setup-popover'));
final background = find.byKey(const ValueKey('setup-background-dismiss'));

class PendingStorage extends SetupStorage {
  Completer<void>? pending;
  @override
  Future<void> write(String value) async {
    await pending?.future;
    await super.write(value);
  }
}

class Host {
  final storage = PendingStorage();
  late final presentation = PresentationController(storage: storage);
  final state = AppState();
  final requests = <http.Request>[];
  int stops = 0;
}

Offset emptyPoint(WidgetTester tester) {
  final rect = tester.getRect(background);
  final occupied = [for (final name in names) tester.getRect(bubble(name))];
  for (double y = rect.top + 10; y < rect.bottom - 10; y += 16) {
    for (double x = rect.left + 10; x < rect.right - 10; x += 16) {
      final point = Offset(x, y);
      if (occupied.every(
        (body) => (body.center - point).distance > body.width / 2 + 2,
      )) {
        return point;
      }
    }
  }
  throw StateError('No empty point in the setup viewport');
}

Offset transparentCornerPoint(WidgetTester tester) {
  final field = tester.getRect(background);
  final model = tester.getRect(bubble('Model'));
  final bodies = [for (final name in names) tester.getRect(bubble(name))];
  // A rectangle corner can lie inside a neighbouring circle. Select a point
  // that actually exercises the intended transparent hit-test region.
  for (final point in [
    model.topLeft + const Offset(2, 2),
    model.topRight + const Offset(-2, 2),
    model.bottomLeft + const Offset(2, -2),
    model.bottomRight + const Offset(-2, -2),
  ]) {
    if (field.contains(point) &&
        bodies.every(
          (body) => (body.center - point).distance > body.width / 2 + 2,
        )) {
      return point;
    }
  }
  throw StateError('No unoccupied transparent Model corner');
}

Future<Host> mount(
  WidgetTester tester, {
  bool small = false,
  bool motion = false,
}) async {
  await tester.binding.setSurfaceSize(
    small ? const Size(320, 740) : const Size(1536, 1024),
  );
  addTearDown(() => tester.binding.setSurfaceSize(null));
  final host = Host();
  host.state.client = KinaiyaClient(
    sessionBase: 'http://synthetic',
    agentBase: 'http://synthetic',
    converseBase: 'http://synthetic',
    ownerToken: 'synthetic-owner',
    httpClient: MockClient((request) async {
      host.requests.add(request);
      return http.Response(jsonEncode(setupFixture()), 200);
    }),
  );
  await tester.runAsync(host.presentation.initialize);
  await host.presentation.setPreferences(
    host.presentation.preferences.copyWith(animateTransitions: motion),
  );
  host.storage.writes.clear();
  addTearDown(() async {
    if (host.storage.pending case final pending? when !pending.isCompleted) {
      pending.complete();
    }
    expect(
      host.requests.every(
        (r) => r.method == 'GET' && r.url.path == '/v1/converse/setup',
      ),
      isTrue,
    );
    expect(host.state.converseSession, isNull);
    expect(host.state.agentSession, isNull);
    expect(host.state.converseHistory, isEmpty);
    expect(host.state.converseEnabled, isTrue);
    await tester.pumpWidget(const SizedBox.shrink());
    host.state.dispose();
    host.presentation.dispose();
  });
  await tester.pumpWidget(
    MaterialApp(
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(
          context,
        ).copyWith(textScaler: TextScaler.linear(small ? 2 : 1)),
        child: child!,
      ),
      home: Builder(
        builder: (context) => Scaffold(
          body: TextButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) => ModelSetupDialog(
                state: host.state,
                bubbleRandom: math.Random(165),
                presentation: host.presentation,
                voiceControls: TextButton(
                  key: const ValueKey('test-stop'),
                  onPressed: () => host.stops++,
                  child: const Text('Stop voice'),
                ),
              ),
            ),
            child: const Text('Open setup'),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('Open setup'));
  await pumpSetupFrames(tester);
  return host;
}

void main() {
  testWidgets(
    'Close is an eighth floating body and its activation dismisses without saving',
    (tester) async {
      final host = await mount(tester, motion: true);
      for (final name in names) {
        expect(bubble(name), findsOneWidget);
      }
      final before = tester.getTopLeft(bubble('Close'));
      await pumpSetupFrames(tester);
      expect(tester.getTopLeft(bubble('Close')), isNot(before));
      await tester.tap(bubble('Close'));
      await pumpSetupFrames(tester);
      expect(dialog, findsNothing);
      expect(host.storage.writes, isEmpty);
      expect(tester.binding.transientCallbackCount, 0);
      expect(tester.takeException(), isNull);
    },
  );

  for (final corner in [false, true]) {
    testWidgets(
      '${corner ? 'Transparent circle corner' : 'Empty field'} tap exits setup',
      (tester) async {
        final host = await mount(tester);
        final point = corner
            ? transparentCornerPoint(tester)
            : emptyPoint(tester);
        await tester.tapAt(point);
        await pumpSetupFrames(tester);
        expect(dialog, findsNothing);
        expect(host.storage.writes, isEmpty);
        expect(tester.takeException(), isNull);
      },
    );
  }

  for (final kind in [PointerDeviceKind.mouse, PointerDeviceKind.touch]) {
    testWidgets(
      '$kind Close dragging stays in setup instead of activating Close or background',
      (tester) async {
        await mount(tester, motion: true);
        final pointer = await tester.startGesture(
          tester.getCenter(bubble('Close')),
          kind: kind,
        );
        await pointer.moveBy(const Offset(40, 0));
        await tester.pump(const Duration(milliseconds: 16));
        await pointer.moveBy(const Offset(20, 0));
        await tester.pump(const Duration(milliseconds: 16));
        await pointer.up();
        await pumpSetupFrames(tester);
        expect(dialog, findsOneWidget);
        expect(panel, findsNothing);
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets(
    'options content stays open; outside closes only the panel before setup',
    (tester) async {
      final host = await mount(tester, motion: true);
      await tester.tap(bubble('Model'));
      await pumpSetupFrames(tester);
      await tester.tap(find.text('Conversation model'));
      await tester.pump();
      expect(panel, findsOneWidget);
      expect(dialog, findsOneWidget);
      await tester.tapAt(tester.getTopLeft(background) + const Offset(8, 8));
      await pumpSetupFrames(tester);
      expect(panel, findsNothing);
      expect(dialog, findsOneWidget);
      await tester.tapAt(emptyPoint(tester));
      await pumpSetupFrames(tester);
      expect(dialog, findsNothing);
      expect(host.storage.writes, isEmpty);
    },
  );

  testWidgets('voice controls remain callable outside the dismissible field', (
    tester,
  ) async {
    final host = await mount(tester);
    await tester.tap(find.byKey(const ValueKey('test-stop')));
    await tester.pump();
    expect(host.stops, 1);
    expect(dialog, findsOneWidget);
    await tester.tap(bubble('Speech'));
    await pumpSetupFrames(tester);
    await tester.tap(find.byKey(const ValueKey('test-stop')));
    await tester.pump();
    expect(host.stops, 2);
    expect(dialog, findsOneWidget);
    expect(panel, findsOneWidget);
  });

  testWidgets(
    'narrow empty and corner swipes scroll without dismissal; Close remains reachable at2x',
    (tester) async {
      await mount(tester, small: true);
      final scroller = find.byKey(const ValueKey('setup-bubbles-narrow'));
      ScrollPosition position() => tester
          .state<ScrollableState>(
            find
                .descendant(of: scroller, matching: find.byType(Scrollable))
                .first,
          )
          .position;
      double scroll() => position().pixels;
      final initial = scroll();
      final start = emptyPoint(tester);
      await tester.dragFrom(start, const Offset(0, -160));
      await pumpSetupFrames(tester);
      expect(dialog, findsOneWidget);
      expect(scroll(), greaterThan(initial));
      await tester.ensureVisible(bubble('Model'));
      await tester.pump();
      final corner = transparentCornerPoint(tester);
      final beforeCorner = scroll();
      final down = position().extentAfter > 120;
      await tester.dragFrom(corner, Offset(0, down ? -80 : 80));
      await pumpSetupFrames(tester);
      expect(dialog, findsOneWidget);
      expect(
        scroll(),
        down ? greaterThan(beforeCorner) : lessThan(beforeCorner),
      );
      final beforeWheel = scroll();
      final wheelDown = position().extentAfter > 120;
      await tester.sendEventToBinding(
        PointerScrollEvent(
          position: emptyPoint(tester),
          scrollDelta: Offset(0, wheelDown ? 80 : -80),
        ),
      );
      await pumpSetupFrames(tester);
      expect(
        scroll(),
        wheelDown ? greaterThan(beforeWheel) : lessThan(beforeWheel),
      );
      expect(dialog, findsOneWidget);
      await tester.ensureVisible(bubble('Close'));
      await tester.pump();
      expect(bubble('Close').hitTestable(), findsOneWidget);
      await tester.tap(bubble('Close'));
      await pumpSetupFrames(tester);
      expect(dialog, findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('keyboard Close activation dismisses the route', (tester) async {
    await mount(tester);
    final ink = tester.widget<InkWell>(
      find.descendant(of: bubble('Close'), matching: find.byType(InkWell)),
    );
    ink.focusNode!.requestFocus();
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await pumpSetupFrames(tester);
    expect(dialog, findsNothing);
  });

  for (final fail in [false, true]) {
    testWidgets(
      'pending save blocks all exit paths; completion fail=$fail preserves result',
      (tester) async {
        final host = await mount(tester);
        await tester.tap(bubble('View'));
        await pumpSetupFrames(tester);
        await tester.tap(find.byKey(const ValueKey('show-converse-view')));
        await tester.pump();
        host.storage.pending = Completer<void>();
        host.storage.failWrites = fail;
        await tester.tap(find.byKey(const ValueKey('save-model-setup')));
        await tester.pump();
        await tester.tap(find.byTooltip('Close settings popover'));
        await pumpSetupFrames(tester);
        expect(panel, findsNothing);
        await tester.tap(bubble('Close'));
        await tester.pump();
        expect(dialog, findsOneWidget);
        await tester.tapAt(emptyPoint(tester));
        await tester.pump();
        expect(dialog, findsOneWidget);
        await tester.tapAt(const Offset(4, 4));
        await tester.pump();
        expect(dialog, findsOneWidget);
        await tester.sendKeyEvent(LogicalKeyboardKey.escape);
        await tester.pump();
        expect(dialog, findsOneWidget);
        host.storage.pending!.complete();
        await pumpSetupFrames(tester);
        expect(dialog, fail ? findsOneWidget : findsNothing);
        if (fail) {
          await tester.tap(bubble('View'));
          await pumpSetupFrames(tester);
          expect(
            find.byKey(const ValueKey('setup-save-notice')),
            findsOneWidget,
          );
          expect(host.storage.writes, isEmpty);
        } else {
          expect(host.storage.writes, hasLength(1));
        }
        expect(tester.takeException(), isNull);
      },
    );
  }
}
