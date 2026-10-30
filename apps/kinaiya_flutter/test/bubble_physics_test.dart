import 'dart:math';
import 'dart:ui';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/bubble_physics.dart';

const Size kSize = Size(400, 600);
const double kDiameter = 48;
const double kRadius = kDiameter / 2;

double centerDistance(Offset topLeftA, Offset topLeftB) {
  final Offset a = topLeftA + const Offset(kRadius, kRadius);
  final Offset b = topLeftB + const Offset(kRadius, kRadius);
  return (a - b).distance;
}

bool circleHitsRect(Offset topLeft, Rect r) {
  final Offset c = topLeft + const Offset(kRadius, kRadius);
  final double qx = c.dx.clamp(r.left, r.right);
  final double qy = c.dy.clamp(r.top, r.bottom);
  final double dx = c.dx - qx;
  final double dy = c.dy - qy;
  return dx * dx + dy * dy < kRadius * kRadius - 1e-6;
}

void expectValidLayout(BubblePhysics p, {List<Rect> obstacles = const []}) {
  expect(p.fits, isTrue);
  expect(p.positions.length, p.count);
  for (final Offset t in p.positions) {
    expect(t.dx, greaterThanOrEqualTo(-1e-9));
    expect(t.dy, greaterThanOrEqualTo(-1e-9));
    expect(t.dx + kDiameter, lessThanOrEqualTo(kSize.width + 1e-9));
    expect(t.dy + kDiameter, lessThanOrEqualTo(kSize.height + 1e-9));
    for (final Rect r in obstacles) {
      expect(circleHitsRect(t, r), isFalse);
    }
  }
  for (int i = 0; i < p.positions.length; i++) {
    for (int j = i + 1; j < p.positions.length; j++) {
      expect(
        centerDistance(p.positions[i], p.positions[j]),
        greaterThanOrEqualTo(kDiameter - 1e-6),
      );
    }
  }
}

void main() {
  group('BubblePhysics deterministic spawn', () {
    test('same seed reproduces identical positions', () {
      final a = BubblePhysics(
        count: 5,
        size: kSize,
        diameter: kDiameter,
        random: Random(42),
      );
      final b = BubblePhysics(
        count: 5,
        size: kSize,
        diameter: kDiameter,
        random: Random(42),
      );
      expect(a.fits, isTrue);
      expect(b.positions, equals(a.positions));
    });

    test('different seeds give different randomized locations', () {
      final a = BubblePhysics(
        count: 5,
        size: kSize,
        diameter: kDiameter,
        random: Random(1),
      );
      final b = BubblePhysics(
        count: 5,
        size: kSize,
        diameter: kDiameter,
        random: Random(2),
      );
      expect(a.fits, isTrue);
      expect(b.fits, isTrue);
      expect(b.positions, isNot(equals(a.positions)));
      // Locations are spread out, not all stacked at one spot.
      final Set<String> uniq = a.positions
          .map((o) => '${o.dx},${o.dy}')
          .toSet();
      expect(uniq.length, equals(5));
    });

    test('positions are an immutable snapshot', () {
      final p = BubblePhysics(
        count: 3,
        size: kSize,
        diameter: kDiameter,
        random: Random(7),
      );
      expect(
        () => p.positions.add(Offset.zero),
        throwsA(isA<UnsupportedError>()),
      );
    });
  });

  group('BubblePhysics motion', () {
    test('no overlap, bounds, or obstacle hits over 30+ simulated seconds', () {
      final obstacles = <Rect>[
        const Rect.fromLTWH(0, 0, 400, 64), // header
        const Rect.fromLTWH(0, 536, 400, 64), // footer
      ];
      final p = BubblePhysics(
        count: 5,
        size: kSize,
        diameter: kDiameter,
        random: Random(11),
        obstacles: obstacles,
      );
      expect(p.fits, isTrue);
      const double dt = 1 / 60;
      for (int i = 0; i < 1860; i++) {
        // 31 seconds.
        p.step(dt);
        expect(p.positions.length, equals(5));
        for (final Offset t in p.positions) {
          expect(t.dx, greaterThanOrEqualTo(-1e-6));
          expect(t.dy, greaterThanOrEqualTo(-1e-6));
          expect(t.dx + kDiameter, lessThanOrEqualTo(kSize.width + 1e-6));
          expect(t.dy + kDiameter, lessThanOrEqualTo(kSize.height + 1e-6));
          for (final Rect r in obstacles) {
            expect(circleHitsRect(t, r), isFalse);
          }
        }
        for (int a = 0; a < 5; a++) {
          for (int b = a + 1; b < 5; b++) {
            expect(
              centerDistance(p.positions[a], p.positions[b]),
              greaterThanOrEqualTo(kDiameter - 1e-6),
            );
          }
        }
      }
    });

    test('slow drift moves meaningfully but does not teleport', () {
      final p = BubblePhysics(
        count: 5,
        size: kSize,
        diameter: kDiameter,
        random: Random(21),
      );
      final List<Offset> start = p.positions.map((o) => o).toList();
      for (int i = 0; i < 60; i++) {
        p.step(1 / 60); // 1 second total.
      }
      double total = 0;
      for (int i = 0; i < 5; i++) {
        final double d = (p.positions[i] - start[i]).distance;
        total += d;
        // Slow: each bubble drifts on the order of 10..20 px/sec.
        expect(d, greaterThan(1.0));
        expect(d, lessThan(40.0));
      }
      expect(total, greaterThan(10.0));
    });

    test('long pause dt is clamped to 0.1s', () {
      final a = BubblePhysics(
        count: 5,
        size: kSize,
        diameter: kDiameter,
        random: Random(33),
      );
      final b = BubblePhysics(
        count: 5,
        size: kSize,
        diameter: kDiameter,
        random: Random(33),
      );
      a.step(10.0); // e.g. background resume.
      b.step(0.1);
      for (int i = 0; i < 5; i++) {
        expect(
          (a.positions[i] - b.positions[i]).distance,
          lessThanOrEqualTo(1e-9),
        );
      }
    });
  });

  group('BubblePhysics impossible + invalid inputs', () {
    test('impossible geometry reports fits=false with no positions', () {
      final tiny = BubblePhysics(
        count: 5,
        size: const Size(50, 50),
        diameter: 48,
        random: Random(5),
      );
      expect(tiny.fits, isFalse);
      expect(tiny.positions, isEmpty);
      // Stepping an unfit engine is a safe no-op.
      tiny.step(1 / 60);
      expect(tiny.positions, isEmpty);

      final blocked = BubblePhysics(
        count: 3,
        size: kSize,
        diameter: kDiameter,
        random: Random(6),
        obstacles: const [Rect.fromLTWH(0, 0, 400, 600)],
      );
      expect(blocked.fits, isFalse);
      expect(blocked.positions, isEmpty);
    });

    test('rejects non-finite and invalid geometry predictably', () {
      expect(
        () => BubblePhysics(
          count: 0,
          size: kSize,
          diameter: kDiameter,
          random: Random(1),
        ),
        throwsArgumentError,
      );
      expect(
        () => BubblePhysics(
          count: 8,
          size: kSize,
          diameter: kDiameter,
          random: Random(1),
        ),
        throwsArgumentError,
      );
      expect(
        () => BubblePhysics(
          count: 2,
          size: kSize,
          diameter: 0,
          random: Random(1),
        ),
        throwsArgumentError,
      );
      expect(
        () => BubblePhysics(
          count: 2,
          size: kSize,
          diameter: double.nan,
          random: Random(1),
        ),
        throwsArgumentError,
      );
      expect(
        () => BubblePhysics(
          count: 2,
          size: const Size(double.infinity, 600),
          diameter: kDiameter,
          random: Random(1),
        ),
        throwsArgumentError,
      );
      expect(
        () => BubblePhysics(
          count: 2,
          size: const Size(0, 600),
          diameter: kDiameter,
          random: Random(1),
        ),
        throwsArgumentError,
      );
      expect(
        () => BubblePhysics(
          count: 2,
          size: kSize,
          diameter: kDiameter,
          random: Random(1),
          obstacles: const [Rect.fromLTWH(double.nan, 0, 10, 10)],
        ),
        throwsArgumentError,
      );

      final p = BubblePhysics(
        count: 2,
        size: kSize,
        diameter: kDiameter,
        random: Random(1),
      );
      expect(() => p.step(double.nan), throwsArgumentError);
      expect(() => p.step(double.infinity), throwsArgumentError);
      // Non-positive dt is a safe no-op, not an error.
      final before = p.positions;
      p.step(0);
      p.step(-1);
      expect(p.positions, equals(before));
    });
  });
}
