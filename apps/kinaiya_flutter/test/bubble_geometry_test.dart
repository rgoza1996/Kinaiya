import 'dart:math';
import 'dart:ui';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/ui/bubble_physics.dart';

void _valid(BubblePhysics physics, List<Rect> obstacles) {
  expect(physics.fits, isTrue);
  final positions = physics.positions;
  final radius = physics.diameter / 2;
  for (var i = 0; i < positions.length; i++) {
    final p = positions[i];
    expect(p.dx, greaterThanOrEqualTo(-1e-7));
    expect(p.dy, greaterThanOrEqualTo(-1e-7));
    expect(
      p.dx + physics.diameter,
      lessThanOrEqualTo(physics.size.width + 1e-7),
    );
    expect(
      p.dy + physics.diameter,
      lessThanOrEqualTo(physics.size.height + 1e-7),
    );
    final center = p + Offset(radius, radius);
    for (final obstacle in obstacles) {
      final nearest = Offset(
        center.dx.clamp(obstacle.left, obstacle.right),
        center.dy.clamp(obstacle.top, obstacle.bottom),
      );
      expect((center - nearest).distance, greaterThanOrEqualTo(radius - 1e-6));
    }
    for (var j = i + 1; j < positions.length; j++) {
      expect(
        (positions[j] - p).distance,
        greaterThanOrEqualTo(physics.diameter - 1e-6),
      );
    }
  }
}

void main() {
  test(
    'narrow large-text fields always fit after partial random packing jams',
    () {
      for (var seed = 0; seed < 100; seed++) {
        final physics = BubblePhysics(
          count: 5,
          size: const Size(288, 1472),
          diameter: 256,
          random: Random(seed),
        );
        _valid(physics, const []);
      }
    },
  );

  for (final size in [const Size(1050, 650), const Size(1504, 992)]) {
    test(
      'wide $size keeps fixed controls and circles clear through collisions',
      () {
        final obstacles = [
          const Rect.fromLTWH(0, 0, 280, 236),
          Rect.fromLTWH(0, size.height - 300, 440, 300),
          Rect.fromLTWH(
            size.width / 2 - 130,
            size.height * .3,
            260,
            size.height * .45,
          ),
        ];
        for (var seed = 0; seed < 16; seed++) {
          final physics = BubblePhysics(
            count: 5,
            size: size,
            diameter: 178,
            random: Random(seed),
            obstacles: obstacles,
          );
          _valid(physics, obstacles);
          for (var frame = 0; frame < 1800; frame++) {
            physics.step(1 / 60);
            _valid(physics, obstacles);
          }
        }
      },
    );
  }
}
