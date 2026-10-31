import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/motion_transition.dart';

void main() {
  for (final motion in [true, false]) {
    testWidgets('top alignment survives short page fades: $motion', (
      tester,
    ) async {
      Widget page(String name, double height) => MaterialApp(
        home: Scaffold(
          body: SizedBox.expand(
            child: UiFadeTransition(
              enabled: motion,
              alignment: Alignment.topLeft,
              transitionKey: name,
              child: SizedBox(height: height, child: Text(name)),
            ),
          ),
        ),
      );
      await tester.pumpWidget(page('Models', 300));
      expect(tester.getTopLeft(find.text('Models')).dy, 0);
      await tester.pumpWidget(page('Audio', 70));
      await tester.pump(const Duration(milliseconds: 100));
      expect(tester.getTopLeft(find.text('Audio')).dy, 0);
      await tester.pumpAndSettle();
      expect(tester.getTopLeft(find.text('Audio')).dy, 0);
    });
  }

  Widget scene(
    String name, {
    bool enabled = true,
    bool systemOff = false,
    Widget? child,
  }) => MaterialApp(
    home: MediaQuery(
      data: MediaQueryData(disableAnimations: systemOff),
      child: UiFadeTransition(
        enabled: enabled,
        transitionKey: name,
        child: child ?? Text(name),
      ),
    ),
  );

  testWidgets('fade is 220ms and removes the old presentation at its end', (
    tester,
  ) async {
    await tester.pumpWidget(scene('Home'));
    await tester.pumpWidget(scene('Converse'));
    expect(find.text('Home'), findsOneWidget);
    expect(find.text('Converse'), findsOneWidget);
    expect(
      tester.widget<AnimatedSwitcher>(find.byType(AnimatedSwitcher)).duration,
      const Duration(milliseconds: 220),
    );
    await tester.pump(const Duration(milliseconds: 110));
    expect(find.text('Home'), findsOneWidget);
    await tester.pumpAndSettle();
    expect(find.text('Home'), findsNothing);
    expect(find.text('Converse'), findsOneWidget);
  });

  for (final systemOff in [false, true]) {
    testWidgets(
      'instant preference/system $systemOff skips visible outgoing content',
      (tester) async {
        await tester.pumpWidget(scene('Home'));
        await tester.pumpWidget(
          scene('Converse', enabled: systemOff, systemOff: systemOff),
        );
        expect(find.text('Home'), findsNothing);
        expect(find.text('Converse'), findsOneWidget);
        expect(
          tester
              .widget<AnimatedSwitcher>(find.byType(AnimatedSwitcher))
              .duration,
          Duration.zero,
        );
      },
    );
  }

  testWidgets('outgoing actions cannot receive taps during a fade', (
    tester,
  ) async {
    var oldTaps = 0;
    await tester.pumpWidget(
      scene(
        'old',
        child: ElevatedButton(
          onPressed: () => oldTaps++,
          child: const Text('Old action'),
        ),
      ),
    );
    await tester.pumpWidget(
      scene('new', child: const SizedBox(width: 10, height: 10)),
    );
    final ignored = find.ancestor(
      of: find.text('Old action'),
      matching: find.byType(IgnorePointer),
    );
    expect(ignored, findsWidgets);
    expect(
      tester.widgetList<IgnorePointer>(ignored).any((w) => w.ignoring),
      isTrue,
    );
    await tester.tap(find.text('Old action'), warnIfMissed: false);
    expect(oldTaps, 0);
    await tester.pumpAndSettle();
  });

  testWidgets(
    'rapid reversal and turning motion off remove stale surfaces immediately',
    (tester) async {
      await tester.pumpWidget(scene('Home'));
      await tester.pumpWidget(scene('Converse'));
      await tester.pump(const Duration(milliseconds: 70));
      await tester.pumpWidget(scene('Home'));
      await tester.pump(const Duration(milliseconds: 30));
      await tester.pumpWidget(scene('Home', enabled: false));
      expect(find.text('Converse'), findsNothing);
      expect(find.text('Home'), findsOneWidget);
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('toggling animation does not replace the current child state', (
    tester,
  ) async {
    var mounts = 0;
    Widget child() => _MountProbe(onMount: () => mounts++);
    await tester.pumpWidget(scene('same', child: child()));
    await tester.pumpWidget(scene('same', enabled: false, child: child()));
    await tester.pumpWidget(scene('same', child: child()));
    expect(mounts, 1);
  });
}

class _MountProbe extends StatefulWidget {
  const _MountProbe({required this.onMount});
  final VoidCallback onMount;
  @override
  State<_MountProbe> createState() => _MountProbeState();
}

class _MountProbeState extends State<_MountProbe> {
  @override
  void initState() {
    super.initState();
    widget.onMount();
  }

  @override
  Widget build(BuildContext context) => const Text('retained');
}
