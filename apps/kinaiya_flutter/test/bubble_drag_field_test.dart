import 'dart:math';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/bubble_physics.dart';
import 'package:kinaiya_flutter/ui/floating_bubbles.dart';

Future<void> _frames(WidgetTester tester, [int count = 60]) async {
  for (var i = 0; i < count; i++) {
    await tester.pump(const Duration(milliseconds: 16));
  }
}

Finder _body(String key) => find.byKey(ValueKey('floating-$key'));
Offset _position(WidgetTester tester, String key) =>
    tester.getTopLeft(_body(key));

class _Control extends StatefulWidget {
  const _Control({super.key, required this.name});
  final String name;

  @override
  State<_Control> createState() => _ControlState();
}

class _ControlState extends State<_Control> {
  final focus = FocusNode();
  int taps = 0;

  void tap() => setState(() => taps++);

  @override
  void dispose() {
    focus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Semantics(
    label: 'Control ${widget.name}',
    button: true,
    onTap: tap,
    child: ExcludeSemantics(
      child: Material(
        color: Colors.blue,
        shape: const CircleBorder(),
        child: InkWell(
          focusNode: focus,
          onTap: tap,
          child: Center(child: Text('${widget.name}:$taps')),
        ),
      ),
    ),
  );
}

class _Harness extends StatefulWidget {
  const _Harness({super.key, this.initialKeys = const ['a', 'b'], this.random});
  final List<String> initialKeys;
  final Random? random;

  @override
  State<_Harness> createState() => _HarnessState();
}

class _HarnessState extends State<_Harness> {
  late List<String> order = widget.initialKeys;
  final controls = <String, GlobalKey<_ControlState>>{};
  final started = <String>[], ended = <String>[];
  Set<String> held = {}, hidden = {};
  bool enabled = true;
  Size size = const Size(600, 500);

  void configure({
    List<String>? keys,
    Set<String>? heldKeys,
    Set<String>? hiddenKeys,
    bool? motion,
    Size? fieldSize,
  }) => setState(() {
    order = keys ?? order;
    held = heldKeys ?? held;
    hidden = hiddenKeys ?? hidden;
    enabled = motion ?? enabled;
    size = fieldSize ?? size;
  });

  @override
  Widget build(BuildContext context) => MaterialApp(
    home: Scaffold(
      body: SizedBox(
        width: size.width,
        height: size.height,
        child: FloatingBubbles(
          key: const ValueKey('field'),
          random: widget.random,
          children: {
            for (final name in order)
              name: _Control(
                key: controls.putIfAbsent(
                  name,
                  () => GlobalKey<_ControlState>(),
                ),
                name: name,
              ),
          },
          diameter: 72,
          enabled: enabled,
          paused: false,
          heldKeys: held,
          hiddenKeys: hidden,
          onDragStarted: (name) => setState(() {
            started.add(name);
            held = {...held}..remove(name);
          }),
          onDragEnded: (name) => setState(() {
            ended.add(name);
            held = {...held}..remove(name);
          }),
          fallback: const Text('Field does not fit'),
        ),
      ),
    ),
  );
}

Future<_HarnessState> _mount(
  WidgetTester tester, {
  List<String> keys = const ['a', 'b'],
  Random? random,
}) async {
  final key = GlobalKey<_HarnessState>();
  await tester.pumpWidget(
    _Harness(key: key, initialKeys: keys, random: random),
  );
  await _frames(tester, 2);
  addTearDown(() async {
    await tester.pumpWidget(const SizedBox.shrink());
    tester.platformDispatcher.clearAccessibilityFeaturesTestValue();
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  });
  return key.currentState!;
}

Offset _safeDelta(WidgetTester tester, String name, [double distance = 100]) {
  final field = tester.getRect(find.byKey(const ValueKey('field')));
  final center = tester.getCenter(_body(name));
  return Offset(center.dx < field.center.dx ? distance : -distance, 0);
}

Future<TestGesture> _startDrag(WidgetTester tester, String name) async {
  final gesture = await tester.startGesture(tester.getCenter(_body(name)));
  await gesture.moveBy(_safeDelta(tester, name, 24));
  await tester.pump();
  return gesture;
}

void main() {
  testWidgets('holding one identified body leaves the other bodies drifting', (
    tester,
  ) async {
    final host = await _mount(tester);
    host.configure(heldKeys: {'a'});
    await tester.pump();
    final held = _position(tester, 'a'), free = _position(tester, 'b');
    await _frames(tester);
    expect(_position(tester, 'a'), held);
    expect((_position(tester, 'b') - free).distance, greaterThan(1));
    host.configure(heldKeys: {});
    await tester.pump();
    final released = _position(tester, 'a');
    await _frames(tester);
    expect((_position(tester, 'a') - released).distance, greaterThan(1));
    expect(tester.takeException(), isNull);
  });

  for (final kind in [PointerDeviceKind.touch, PointerDeviceKind.mouse]) {
    testWidgets(
      '$kind drag holds only its body and never activates its InkWell tap',
      (tester) async {
        final host = await _mount(tester);
        await tester.tap(_body('a'), kind: kind);
        await tester.pump();
        expect(host.controls['a']!.currentState!.taps, 1);
        final gesture = await tester.startGesture(
          tester.getCenter(_body('a')),
          kind: kind,
        );
        await gesture.moveBy(_safeDelta(tester, 'a', 24));
        await tester.pump();
        await gesture.moveBy(_safeDelta(tester, 'a', 24));
        await tester.pump();
        expect(host.started, ['a']);
        final dragged = _position(tester, 'a'), other = _position(tester, 'b');
        await _frames(tester);
        expect(_position(tester, 'a'), dragged);
        expect((_position(tester, 'b') - other).distance, greaterThan(1));
        await gesture.up();
        await tester.pump();
        expect(host.ended, ['a']);
        expect(host.controls['a']!.currentState!.taps, 1);
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets('a short centering move remains a tap without starting a pan', (
    tester,
  ) async {
    final host = await _mount(tester, keys: ['a'], random: Random(2403));
    host.configure(motion: false);
    await tester.pump();
    final field = tester.getRect(find.byKey(const ValueKey('field')));
    final centeringDelta = field.center - tester.getCenter(_body('a'));
    expect(centeringDelta.distance, lessThan(kTouchSlop));
    await tester.drag(_body('a'), centeringDelta);
    await tester.pump();
    expect(host.controls['a']!.currentState!.taps, 1);
    expect(host.started, isEmpty);
    expect(host.ended, isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'release velocity launches the body within the engine speed cap',
    (tester) async {
      final host = await _mount(tester, keys: ['a'], random: Random(0));
      host.configure(motion: false);
      await tester.pump();
      final field = tester.getRect(find.byKey(const ValueKey('field')));
      final centeringDelta = field.center - tester.getCenter(_body('a'));
      // The setup move must become a pan before testing the later fling.
      expect(centeringDelta.distance, greaterThan(2 * kTouchSlop));
      await tester.drag(_body('a'), centeringDelta);
      await tester.pump();
      host.configure(motion: true);
      await _frames(tester, 2);
      await tester.fling(_body('a'), const Offset(80, 0), 1200);
      await tester.pump();
      final released = _position(tester, 'a');
      await _frames(tester, 6);
      final distance = (_position(tester, 'a') - released).distance;
      expect(distance, greaterThan(8));
      expect(
        distance,
        lessThanOrEqualTo(BubblePhysics.maxLaunchSpeed * .096 + 1),
      );
      expect(host.controls['a']!.currentState!.taps, 0);
      expect(tester.takeException(), isNull);
    },
  );

  for (final gate in ['Animations off', 'OS disableAnimations']) {
    testWidgets('$gate permits dragging but discards release velocity', (
      tester,
    ) async {
      final host = await _mount(tester, keys: ['a']);
      if (gate == 'Animations off') {
        host.configure(motion: false);
      } else {
        tester.platformDispatcher.accessibilityFeaturesTestValue =
            const FakeAccessibilityFeatures(disableAnimations: true);
      }
      await tester.pump();
      final initial = _position(tester, 'a');
      await tester.fling(_body('a'), _safeDelta(tester, 'a'), 1200);
      await tester.pump();
      final released = _position(tester, 'a');
      expect((released - initial).distance, greaterThan(20));
      await _frames(tester);
      expect(_position(tester, 'a'), released);
      expect(tester.binding.transientCallbackCount, 0);
      expect(host.started, ['a']);
      expect(host.ended, ['a']);
      host.configure(motion: true);
      tester.platformDispatcher.clearAccessibilityFeaturesTestValue();
      await _frames(tester, 2);
      final resumed = _position(tester, 'a');
      await _frames(tester, 6);
      expect((_position(tester, 'a') - resumed).distance, lessThan(4));
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets(
    'pointer cancellation releases its hold without applying a throw',
    (tester) async {
      final host = await _mount(tester, keys: ['a']);
      final gesture = await _startDrag(tester, 'a');
      await gesture.moveBy(_safeDelta(tester, 'a', 30));
      await tester.pump();
      await gesture.cancel();
      await tester.pump();
      final cancelled = _position(tester, 'a');
      await _frames(tester, 6);
      expect((_position(tester, 'a') - cancelled).distance, lessThan(4));
      await _frames(tester);
      expect((_position(tester, 'a') - cancelled).distance, greaterThan(1));
      expect(host.started, ['a']);
      expect(host.ended, ['a']);
      expect(host.controls['a']!.currentState!.taps, 0);
      expect(tester.takeException(), isNull);
    },
  );

  for (final replacement in ['resize', 'body order']) {
    testWidgets(
      '$replacement cancels ownership and late drag events cannot move the replacement',
      (tester) async {
        final host = await _mount(tester);
        final gesture = await _startDrag(tester, 'a');
        expect(host.started, ['a']);
        host.configure(
          fieldSize: replacement == 'resize' ? const Size(500, 400) : null,
          keys: replacement == 'body order' ? ['b', 'a'] : null,
        );
        await tester.pump();
        expect(host.ended, ['a']);
        final replaced = _position(tester, 'a');
        await gesture.moveBy(const Offset(150, 30));
        await gesture.up();
        await tester.pump();
        expect(_position(tester, 'a'), replaced);
        expect(host.ended, ['a']);
        await _frames(tester, 6);
        expect((_position(tester, 'a') - replaced).distance, lessThan(4));
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets(
    'background cancellation rejects stale drag updates and resume fling',
    (tester) async {
      final host = await _mount(tester, keys: ['a']);
      final gesture = await _startDrag(tester, 'a');
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await tester.pump();
      final stopped = _position(tester, 'a');
      await tester.pump(const Duration(minutes: 1));
      expect(_position(tester, 'a'), stopped);
      expect(host.ended, ['a']);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      expect(_position(tester, 'a'), stopped);
      await gesture.moveBy(const Offset(150, 0));
      await gesture.up();
      await tester.pump();
      expect(_position(tester, 'a'), stopped);
      await _frames(tester, 6);
      expect((_position(tester, 'a') - stopped).distance, lessThan(4));
      expect(host.ended, ['a']);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'hidden bodies retain state and render geometry without focus or hit targets',
    (tester) async {
      final host = await _mount(tester, keys: ['a']);
      host.configure(motion: false, heldKeys: {'a'});
      await tester.pump();
      await tester.tap(_body('a'));
      await tester.pump();
      final key = host.controls['a']!,
          state = host.controls['a']!.currentState!;
      final geometry = tester.getRect(find.byKey(key));
      state.focus.requestFocus();
      await tester.pump();
      expect(state.focus.hasFocus, isTrue);
      final semantics = tester.ensureSemantics();
      try {
        expect(find.semantics.byLabel('Control a'), findsOne);
        host.configure(hiddenKeys: {'a'});
        await tester.pump();
        expect(key.currentState, same(state));
        expect(tester.getRect(find.byKey(key)), geometry);
        expect(state.focus.hasFocus, isFalse);
        expect(find.semantics.byLabel('Control a'), findsNothing);
        expect(find.byKey(key).hitTestable(), findsNothing);
        await tester.tapAt(geometry.center);
        await tester.pump();
        expect(state.taps, 1);
        host.configure(hiddenKeys: {});
        await tester.pump();
        expect(find.semantics.byLabel('Control a'), findsOne);
        await tester.tap(_body('a'));
        await tester.pump();
        expect(key.currentState, same(state));
        expect(state.taps, 2);
        expect(tester.takeException(), isNull);
      } finally {
        semantics.dispose();
      }
    },
  );

  testWidgets('disposing an owned drag produces no late callbacks or ticker', (
    tester,
  ) async {
    final host = await _mount(tester, keys: ['a']);
    final gesture = await _startDrag(tester, 'a');
    expect(host.started, ['a']);
    await tester.pumpWidget(const SizedBox.shrink());
    await gesture.moveBy(const Offset(150, 0));
    await gesture.up();
    await tester.pump();
    expect(host.ended, isEmpty);
    expect(tester.binding.transientCallbackCount, 0);
    expect(tester.takeException(), isNull);
  });
}
