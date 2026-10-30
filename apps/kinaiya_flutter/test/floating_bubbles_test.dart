import 'dart:convert';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/presentation/controller.dart';
import 'package:kinaiya_flutter/state/app_state.dart';
import 'package:kinaiya_flutter/ui/floating_bubbles.dart';
import 'package:kinaiya_flutter/ui/model_setup.dart';

import 'model_setup_test.dart' show SetupStorage, setupFixture;

const _sections = ['Model', 'Reasoning', 'Inputs', 'Speech', 'Decisions'];

Future<void> _frames(WidgetTester tester, {int count = 120}) async {
  for (var i = 0; i < count; i++) {
    await tester.pump(const Duration(milliseconds: 16));
  }
}

Map<String, Offset> _positions(WidgetTester tester) => {
  for (final section in _sections)
    section: tester.getTopLeft(find.byKey(ValueKey('floating-$section'))),
};

void _expectMoved(Map<String, Offset> before, Map<String, Offset> after) {
  expect(
    _sections.any(
      (section) => (after[section]! - before[section]!).distance > 1,
    ),
    isTrue,
    reason: 'At least one visible bubble should drift across two seconds.',
  );
}

Widget _field({
  bool enabled = true,
  bool paused = false,
  bool tickerMode = true,
}) => MaterialApp(
  home: Scaffold(
    body: Center(
      child: SizedBox(
        width: 760,
        height: 540,
        child: TickerMode(
          enabled: tickerMode,
          child: FloatingBubbles(
            children: {
              for (final section in _sections)
                section: ColoredBox(
                  color: Colors.blue,
                  child: Center(child: Text(section)),
                ),
            },
            diameter: 80,
            enabled: enabled,
            paused: paused,
            fallback: const Text('Insufficient room'),
          ),
        ),
      ),
    ),
  ),
);

class _SetupHost {
  _SetupHost(this.state, this.presentation, this.requests);

  final AppState state;
  final PresentationController presentation;
  final List<http.Request> requests;

  Widget app({bool launcher = false, double textScale = 1}) => MaterialApp(
    builder: (context, child) => MediaQuery(
      data: MediaQuery.of(
        context,
      ).copyWith(textScaler: TextScaler.linear(textScale)),
      child: child!,
    ),
    home: launcher
        ? Builder(
            builder: (context) => Scaffold(
              body: TextButton(
                onPressed: () => showDialog<void>(
                  context: context,
                  builder: (_) => ModelSetupDialog(
                    state: state,
                    presentation: presentation,
                  ),
                ),
                child: const Text('Open setup'),
              ),
            ),
          )
        : ModelSetupDialog(state: state, presentation: presentation),
  );

  void expectReadOnly() {
    expect(requests, isNotEmpty);
    expect(
      requests.every(
        (request) =>
            request.method == 'GET' && request.url.path == '/v1/converse/setup',
      ),
      isTrue,
    );
    expect(state.agentSession, isNull);
    expect(state.converseSession, isNull);
    expect(state.converseHistory, isEmpty);
    expect(state.converseEnabled, isTrue);
  }
}

Future<_SetupHost> _setup(
  WidgetTester tester, {
  int responseStatus = 200,
}) async {
  final requests = <http.Request>[];
  final state = AppState()
    ..client = KinaiyaClient(
      sessionBase: 'http://synthetic',
      agentBase: 'http://synthetic',
      converseBase: 'http://synthetic',
      ownerToken: 'synthetic-owner',
      httpClient: MockClient((request) async {
        requests.add(request);
        return http.Response(jsonEncode(setupFixture()), responseStatus);
      }),
    );
  final presentation = PresentationController(storage: SetupStorage());
  await tester.runAsync(presentation.initialize);
  addTearDown(() async {
    await tester.pumpWidget(const SizedBox.shrink());
    state.dispose();
    presentation.dispose();
  });
  return _SetupHost(state, presentation, requests);
}

Future<void> _wide(WidgetTester tester) async {
  await tester.binding.setSurfaceSize(const Size(1536, 1024));
  addTearDown(() => tester.binding.setSurfaceSize(null));
}

Finder _bubble(String section) =>
    find.byKey(ValueKey('setup-bubble-${section.toLowerCase()}'));

void main() {
  testWidgets(
    'visible bubbles drift over bounded frames and dispose their ticker',
    (tester) async {
      await tester.pumpWidget(_field());
      await _frames(tester, count: 2);
      final initial = _positions(tester);
      await _frames(tester);
      _expectMoved(initial, _positions(tester));
      expect(tester.binding.transientCallbackCount, greaterThan(0));
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
      expect(tester.binding.transientCallbackCount, 0);
      expect(tester.takeException(), isNull);
    },
  );

  for (final gate in [
    'paused',
    'disabled',
    'ticker mode',
    'OS disableAnimations',
  ]) {
    testWidgets(
      '$gate stops frames and enabling motion resumes without re-layout',
      (tester) async {
        addTearDown(
          tester.platformDispatcher.clearAccessibilityFeaturesTestValue,
        );
        await tester.pumpWidget(_field());
        await _frames(tester, count: 2);
        if (gate == 'OS disableAnimations') {
          tester.platformDispatcher.accessibilityFeaturesTestValue =
              const FakeAccessibilityFeatures(disableAnimations: true);
        } else {
          await tester.pumpWidget(
            _field(
              enabled: gate != 'disabled',
              paused: gate == 'paused',
              tickerMode: gate != 'ticker mode',
            ),
          );
        }
        await tester.pump();
        final stopped = _positions(tester);
        await _frames(tester);
        expect(_positions(tester), stopped);
        expect(tester.binding.transientCallbackCount, 0);
        tester.platformDispatcher.clearAccessibilityFeaturesTestValue();
        await tester.pumpWidget(_field());
        await _frames(tester, count: 2);
        final resumed = _positions(tester);
        await _frames(tester);
        _expectMoved(resumed, _positions(tester));
        await tester.pumpWidget(const SizedBox.shrink());
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets('background time does not become a resume position jump', (
    tester,
  ) async {
    addTearDown(() {
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    });
    await tester.pumpWidget(_field());
    await _frames(tester, count: 30);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await tester.pump();
    final stopped = _positions(tester);
    await tester.pump(const Duration(minutes: 1));
    expect(_positions(tester), stopped);
    expect(tester.binding.transientCallbackCount, 0);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump(const Duration(milliseconds: 16));
    expect(_positions(tester), stopped);
    await _frames(tester);
    _expectMoved(stopped, _positions(tester));
    await tester.pumpWidget(const SizedBox.shrink());
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'hover, pressed pointer and keyboard focus pause the actual setup field',
    (tester) async {
      await _wide(tester);
      final host = await _setup(tester);
      await tester.pumpWidget(host.app());
      await _frames(tester, count: 20);
      final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
      await mouse.addPointer(location: Offset.zero);
      await mouse.moveTo(tester.getCenter(_bubble('Model')));
      await _frames(tester, count: 2);
      final hovered = _positions(tester);
      await _frames(tester);
      expect(_positions(tester), hovered);
      await mouse.moveTo(Offset.zero);
      await _frames(tester, count: 2);
      final unhovered = _positions(tester);
      await _frames(tester);
      _expectMoved(unhovered, _positions(tester));

      final pointer = await tester.startGesture(
        tester.getCenter(_bubble('Speech')),
      );
      await _frames(tester, count: 2);
      final pressed = _positions(tester);
      await _frames(tester);
      expect(_positions(tester), pressed);
      await pointer.cancel();
      await _frames(tester, count: 2);

      final invoker = tester.widget<InkWell>(
        find.descendant(of: _bubble('Inputs'), matching: find.byType(InkWell)),
      );
      invoker.focusNode!.requestFocus();
      await _frames(tester, count: 2);
      expect(invoker.focusNode!.hasFocus, isTrue);
      final focused = _positions(tester);
      await _frames(tester);
      expect(_positions(tester), focused);
      invoker.focusNode!.unfocus();
      await _frames(tester, count: 2);
      final unfocused = _positions(tester);
      await _frames(tester);
      _expectMoved(unfocused, _positions(tester));
      await mouse.removePointer();
      host.expectReadOnly();
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('a hovered field resumes after its breakpoint layout unmounts', (
    tester,
  ) async {
    await _wide(tester);
    final host = await _setup(tester);
    await tester.pumpWidget(host.app());
    await _frames(tester, count: 20);
    final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await mouse.addPointer(location: Offset.zero);
    await mouse.moveTo(tester.getCenter(_bubble('Model')));
    await _frames(tester, count: 2);
    final hovered = _positions(tester);
    await _frames(tester);
    expect(_positions(tester), hovered);

    // The old MouseRegion disappears at this breakpoint and cannot deliver
    // onExit. Moving outside must not leave the replacement field paused.
    await tester.binding.setSurfaceSize(const Size(320, 740));
    await tester.pump();
    expect(find.byKey(const ValueKey('setup-floating-wide')), findsNothing);
    expect(find.byKey(const ValueKey('setup-floating-narrow')), findsOneWidget);
    await mouse.moveTo(Offset.zero);
    await _frames(tester, count: 2);
    final replaced = _positions(tester);
    await _frames(tester);
    _expectMoved(replaced, _positions(tester));
    expect(tester.binding.transientCallbackCount, greaterThan(0));
    await mouse.removePointer();
    expect(host.requests.length, 1);
    host.expectReadOnly();
    expect(tester.takeException(), isNull);
  });

  for (final unavailable in ['disconnected host', 'HTTP error']) {
    testWidgets('wide $unavailable header remains clear of drifting circles', (
      tester,
    ) async {
      await _wide(tester);
      final host = await _setup(tester, responseStatus: 503);
      if (unavailable == 'disconnected host') host.state.disconnect();
      await tester.pumpWidget(host.app());
      await _frames(tester, count: 20);
      expect(
        find.byKey(const ValueKey('model-setup-unavailable')),
        findsOneWidget,
      );
      final header = tester.getRect(
        find.byKey(const ValueKey('setup-fixed-header')),
      );
      for (var frame = 0; frame < 120; frame++) {
        for (final section in _sections) {
          final circle = tester.getRect(_bubble(section));
          final center = circle.center;
          final nearest = Offset(
            center.dx.clamp(header.left, header.right),
            center.dy.clamp(header.top, header.bottom),
          );
          expect(
            (center - nearest).distance,
            greaterThanOrEqualTo(circle.width / 2),
            reason: '$section must remain outside the fixed error header.',
          );
        }
        await tester.pump(const Duration(milliseconds: 16));
      }
      if (unavailable == 'disconnected host') {
        expect(host.requests, isEmpty);
      } else {
        expect(host.requests.length, 1);
        host.expectReadOnly();
      }
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets(
    'an open popover stays anchored while pointer and focus leave its bubble',
    (tester) async {
      await _wide(tester);
      final host = await _setup(tester);
      await tester.pumpWidget(host.app());
      await _frames(tester, count: 20);
      await tester.tap(_bubble('Model'));
      await _frames(tester, count: 20);
      expect(find.byKey(const ValueKey('setup-popover')), findsOneWidget);
      FocusManager.instance.primaryFocus?.unfocus();
      await _frames(tester, count: 2);
      final opened = _positions(tester);
      await _frames(tester);
      expect(_positions(tester), opened);
      await tester.tap(find.byTooltip('Close settings popover'));
      await _frames(tester, count: 20);
      expect(find.byKey(const ValueKey('setup-popover')), findsNothing);
      final invoker = tester.widget<InkWell>(
        find.descendant(of: _bubble('Model'), matching: find.byType(InkWell)),
      );
      expect(invoker.focusNode!.hasFocus, isTrue);
      invoker.focusNode!.unfocus();
      await _frames(tester, count: 2);
      final closed = _positions(tester);
      await _frames(tester);
      _expectMoved(closed, _positions(tester));
      host.expectReadOnly();
    },
  );

  for (final preference in ['animations disabled', 'owner reduced motion']) {
    testWidgets('$preference freezes the setup field and can be reversed', (
      tester,
    ) async {
      await _wide(tester);
      final host = await _setup(tester);
      await tester.pumpWidget(host.app());
      await _frames(tester, count: 20);
      await host.presentation.setPreferences(
        host.presentation.preferences.copyWith(
          animateTransitions: preference != 'animations disabled',
          reducedMotion: preference == 'owner reduced motion',
        ),
      );
      await _frames(tester, count: 2);
      final stopped = _positions(tester);
      await _frames(tester);
      expect(_positions(tester), stopped);
      await host.presentation.setPreferences(
        host.presentation.preferences.copyWith(
          animateTransitions: true,
          reducedMotion: false,
        ),
      );
      await _frames(tester, count: 2);
      final resumed = _positions(tester);
      await _frames(tester);
      _expectMoved(resumed, _positions(tester));
      host.expectReadOnly();
    });
  }

  testWidgets(
    'dismissal stops presentation frames and reopening randomizes positions',
    (tester) async {
      await _wide(tester);
      final host = await _setup(tester);
      await tester.pumpWidget(host.app(launcher: true));
      await tester.tap(find.text('Open setup'));
      await _frames(tester, count: 20);
      final first = _positions(tester);
      await tester.tap(find.byTooltip('Close setup'));
      await _frames(tester, count: 20);
      expect(find.byType(FloatingBubbles), findsNothing);
      expect(tester.binding.transientCallbackCount, 0);
      await tester.tap(find.text('Open setup'));
      await _frames(tester, count: 20);
      expect(_positions(tester), isNot(equals(first)));
      expect(host.requests.length, 2);
      host.expectReadOnly();
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'all five controls remain reachable at320px2x and after resizing',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(320, 740));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final host = await _setup(tester);

      Future<void> inspectEverySection() async {
        for (final section in _sections) {
          final bubble = _bubble(section);
          await tester.ensureVisible(bubble);
          await _frames(tester, count: 20);
          expect(bubble.hitTestable(), findsOneWidget);
          await tester.tap(bubble);
          await _frames(tester, count: 20);
          final popover = find.byKey(const ValueKey('setup-popover'));
          await tester.ensureVisible(popover);
          await _frames(tester, count: 20);
          expect(find.text('$section settings').hitTestable(), findsOneWidget);
          await tester.sendKeyEvent(LogicalKeyboardKey.escape);
          await _frames(tester, count: 20);
          expect(popover, findsNothing);
          expect(tester.takeException(), isNull);
        }
      }

      await tester.pumpWidget(host.app(textScale: 2));
      await _frames(tester, count: 20);
      await inspectEverySection();
      await tester.binding.setSurfaceSize(const Size(1536, 1024));
      await tester.pumpWidget(host.app());
      await _frames(tester, count: 20);
      await inspectEverySection();
      expect(host.requests.length, 1);
      host.expectReadOnly();
    },
  );
}
