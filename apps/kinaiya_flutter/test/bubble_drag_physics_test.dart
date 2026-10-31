import 'dart:math';
import 'dart:ui';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/bubble_physics.dart';

class _SequenceRandom implements Random {
  _SequenceRandom(this.values);
  final List<double> values;
  int next = 0;

  @override
  double nextDouble() => values[next++];
  @override
  int nextInt(int max) => 0;
  @override
  bool nextBool() => false;
}

BubblePhysics _at(
  List<Offset> positions, {
  required Size size,
  double diameter = kDiameter,
  List<Rect> obstacles = const [],
}) => BubblePhysics(
  count: positions.length,
  size: size,
  diameter: diameter,
  obstacles: obstacles,
  random: _SequenceRandom([
    for (final p in positions) ...[
      p.dx / (size.width - diameter),
      p.dy / (size.height - diameter),
    ],
    for (final _ in positions) ...[0, 0],
  ]),
);

const Size kSize = Size(400, 600);
const double kDiameter = 48;
const double kRadius = kDiameter / 2;

double centerDistance(Offset a, Offset b) {
  final Offset ca = a + const Offset(kRadius, kRadius);
  final Offset cb = b + const Offset(kRadius, kRadius);
  return (ca - cb).distance;
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
  test('eight floating controls remain bounded with a held Close body', () {
    final p = BubblePhysics(
      count: 8,
      size: kSize,
      diameter: kDiameter,
      random: Random(154),
    );
    final close = p.positions[7];
    p.launch(0, const Offset(600, -600));
    for (var frame = 0; frame < 600; frame++) {
      p.step(1 / 60, held: const {7});
      expect(p.positions[7], close);
      expectValidLayout(p);
    }
  });

  group('seven-body spawn + held stepping', () {
    test('seven seeds spawn valid layouts', () {
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(99),
      );
      expectValidLayout(p);
    });

    test('one held body stays exact while others move', () {
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(100),
      );
      expectValidLayout(p);
      final initial = p.positions;
      final held = p.positions[0];
      bool moved = false;
      for (int i = 0; i < 120; i++) {
        p.step(1 / 60, held: const {0});
        expect(p.positions[0], equals(held));
        expectValidLayout(p);
        for (int k = 1; k < 7; k++) {
          moved |= (p.positions[k] - initial[k]).distance > 1;
        }
      }
      expect(moved, isTrue);
    });

    test('two held bodies stay exact; unheld keep valid + collide', () {
      final obstacles = <Rect>[const Rect.fromLTWH(0, 0, 400, 64)];
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(101),
        obstacles: obstacles,
      );
      expectValidLayout(p, obstacles: obstacles);
      final h0 = p.positions[0];
      final h1 = p.positions[1];
      for (int i = 0; i < 120; i++) {
        p.step(1 / 60, held: const {0, 1});
        expect(p.positions[0], equals(h0));
        expect(p.positions[1], equals(h1));
        expectValidLayout(p, obstacles: obstacles);
      }
    });
  });

  group('drag swept path', () {
    test('huge jump stays valid and never teleports across obstacle', () {
      final obstacles = <Rect>[const Rect.fromLTWH(0, 250, 400, 100)];
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(102),
        obstacles: obstacles,
      );
      expectValidLayout(p, obstacles: obstacles);
      final before = List<Offset>.of(p.positions);
      p.moveBody(0, const Offset(10000, 10000));
      expectValidLayout(p, obstacles: obstacles);
      // Unrelated bodies untouched in position.
      for (int i = 1; i < 7; i++) {
        expect(p.positions[i], equals(before[i]));
      }
      // Dragged body stayed inside and did not jump across the band in one
      // leap: it must be on the same side it started, or stopped at the band.
      expect(
        p.positions[0].dy + kDiameter <= 250 + 1e-6 ||
            p.positions[0].dy >= 350 - 1e-6,
        isTrue,
      );
      expect(
        p.positions[0].dy < 250,
        before[0].dy < 250,
        reason: 'The swept path must remain on its original side of the band.',
      );
    });

    test('bounded sweep cannot skip a thin obstacle on a large field', () {
      const barrier = Rect.fromLTWH(5050, 0, 1, 600);
      final p = _at(
        const [Offset(100, 200)],
        size: const Size(20000, 600),
        obstacles: const [barrier],
      );
      p.moveBody(0, const Offset(19000, 200));
      expect(p.positions.single.dx - 100, lessThanOrEqualTo(128 * 6));
      expect(
        p.positions.single.dx + kDiameter,
        lessThanOrEqualTo(barrier.left),
      );
      for (var i = 0; i < 30; i++) {
        p.moveBody(0, const Offset(19000, 200));
      }
      expect(p.positions.single.dx + kDiameter, closeTo(barrier.left, .1));
    });

    test('a short grazing drag cannot skip a neighbouring circle', () {
      final p = _at(const [Offset(100, 100), Offset(103, 151)], size: kSize);
      p.moveBody(1, const Offset(103, 147.99));
      final neighbour = p.positions[1];
      p.moveBody(0, const Offset(106, 100));
      expect(p.positions[0].dx, lessThan(103));
      expect(p.positions[1], neighbour);
      expectValidLayout(p);
    });

    test('wall clamp + neighbor stop + held partner never moves', () {
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(103),
      );
      expectValidLayout(p);
      final partnerBefore = p.positions[1];
      // Aim into the partner: drag must stop, never push the partner.
      p.moveBody(0, partnerBefore, held: const {0, 1});
      expect(p.positions[1], equals(partnerBefore));
      expectValidLayout(p);
      // Aim outside walls: clamped inside.
      p.moveBody(2, const Offset(-500, -500));
      expectValidLayout(p);
    });
  });

  group('finite + index rejection', () {
    test('rejects bad indices and nonfinite inputs', () {
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(104),
      );
      expect(() => p.step(double.nan), throwsArgumentError);
      expect(() => p.step(1 / 60, held: const {7}), throwsArgumentError);
      expect(() => p.moveBody(7, Offset.zero), throwsArgumentError);
      expect(
        () => p.moveBody(0, const Offset(double.nan, 0)),
        throwsArgumentError,
      );
      expect(() => p.launch(7, Offset.zero), throwsArgumentError);
      expect(
        () => p.launch(0, const Offset(double.infinity, 0)),
        throwsArgumentError,
      );
    });
  });

  group('launch cap + damping + zero release', () {
    test('huge finite release preserves direction and stays capped', () {
      final p = _at(const [Offset(100, 200)], size: kSize);
      p.launch(0, const Offset(1e308, 1e308));
      final before = p.positions.single;
      p.step(1 / 120);
      final delta = p.positions.single - before;
      expect(delta.dx, greaterThan(1));
      expect(delta.dy, greaterThan(1));
      expect(delta.distance, lessThanOrEqualTo(600 / 120));
      expect(delta.dx, closeTo(delta.dy, 1e-8));
    });

    test('rollback does not push a neighbour into the opposite obstacle', () {
      // Two approaching circles occupy a narrow corridor. Pair correction
      // pushes body 0 into the left wall. Reverting it must not push body 1
      // through the right wall, while the distant third body keeps moving.
      const right = 179.3032478077365;
      const obstacles = [
        Rect.fromLTWH(0, 0, 126, 600),
        Rect.fromLTWH(right, 0, 400 - right, 600),
      ];
      final p = _at(
        const [Offset(128, 76), Offset(129.1, 126.01), Offset(128, 426)],
        size: kSize,
        obstacles: obstacles,
      );
      p.moveBody(0, const Offset(126, 76));
      p.moveBody(1, const Offset(131, 126.01));
      expectValidLayout(p, obstacles: obstacles);
      final independent = p.positions[2];
      p.launch(0, const Offset(0, 300));
      p.launch(1, const Offset(0, -600));
      p.step(1 / 120);
      expectValidLayout(p, obstacles: obstacles);
      expect((p.positions[2] - independent).distance, greaterThan(.01));
    });

    test('partial rollback remains valid around held bodies and obstacles', () {
      const obstacles = [
        Rect.fromLTWH(0, 0, 400, 64),
        Rect.fromLTWH(0, 536, 400, 64),
        Rect.fromLTWH(180, 200, 40, 200),
      ];
      for (var seed = 0; seed < 32; seed++) {
        final p = BubblePhysics(
          count: 7,
          size: kSize,
          diameter: kDiameter,
          random: Random(seed),
          obstacles: obstacles,
        );
        final random = Random(seed + 1000);
        for (var i = 0; i < 7; i++) {
          p.launch(
            i,
            Offset(
              random.nextDouble() * 1200 - 600,
              random.nextDouble() * 1200 - 600,
            ),
          );
        }
        final held = p.positions[0];
        for (var frame = 0; frame < 600; frame++) {
          p.step(1 / 60, held: const {0});
          expect(p.positions[0], held);
          expectValidLayout(p, obstacles: obstacles);
        }
      }
    });
    test('launch clamps to 600 and damps back to slow drift', () {
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(105),
      );
      p.launch(0, const Offset(5000, 0));
      // After clamp, one small step moves at most ~600*dt + margin.
      final start = p.positions[0];
      p.step(1 / 60);
      expect((p.positions[0] - start).distance <= 600 / 60 + 5, isTrue);
      // Damp over simulated seconds back toward 10..20 px/sec pace.
      final mid = List<Offset>.of(p.positions);
      for (int i = 0; i < 600; i++) {
        p.step(1 / 60);
      }
      expectValidLayout(p);
      double d = (p.positions[0] - mid[0]).distance;
      // Still drifting slowly, not stuck and not racing.
      expect(d, greaterThan(1.0));
      expect(d, lessThan(400.0));
    });

    test('zero release resumes drift instead of stranding', () {
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(106),
      );
      p.launch(3, Offset.zero);
      final start = p.positions[3];
      for (int i = 0; i < 60; i++) {
        p.step(1 / 60);
      }
      expect((p.positions[3] - start).distance, greaterThan(1.0));
      expectValidLayout(p);
    });

    test('many seconds after high-speed collisions stay valid', () {
      final obstacles = <Rect>[
        const Rect.fromLTWH(0, 0, 400, 64),
        const Rect.fromLTWH(0, 536, 400, 64),
      ];
      final p = BubblePhysics(
        count: 7,
        size: kSize,
        diameter: kDiameter,
        random: Random(107),
        obstacles: obstacles,
      );
      p.launch(0, const Offset(600, 600));
      p.launch(1, const Offset(-600, 300));
      const double dt = 1 / 60;
      for (int i = 0; i < 1860; i++) {
        p.step(dt);
        expectValidLayout(p, obstacles: obstacles);
      }
    });
  });
}
