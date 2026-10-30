import 'dart:math';
import 'dart:ui';

/// Slow-floating bubble physics with no widget or ticker lifecycle.
///
/// Positions are exposed as TOP-LEFT coordinates of each circle's bounding
/// box. Internally the simulation tracks circle centers and velocities.
///
/// Guarantees:
/// * Constructor places [count] circles fully inside [size] and outside
///   [obstacles] (with a small gap) using bounded random retries plus a
///   bounded shuffled-grid fallback. Never loops unboundedly.
/// * When placement is impossible, [fits] is false and [positions] is empty
///   so the UI can use its accessible flow fallback instead of overlapping.
/// * [step] advances with bounded fixed substeps and clamps elapsed time to
///   at most 0.1s, so background resumes cannot teleport bubbles.
class BubblePhysics {
  /// Maximum bodies the engine is tuned for (see [step] bounded work).
  static const int maxCount = 5;

  /// Upper bound on simulated time per [step] call (seconds).
  static const double maxDt = 0.1;

  /// Substep length target; keeps per-substep travel well below one radius
  /// at drift speeds so circles cannot tunnel through walls/obstacles.
  static const double _subDt = 1 / 120;

  /// Cap on random spawn attempts (bounded search, never unbounded).
  static const int _maxSpawnAttempts = 400;

  /// Cap on fallback grid candidates (keeps spawn bounded for tiny
  /// diameters in large areas).
  static const int _maxGridCandidates = 2000;

  final int count;
  final Size size;
  final double diameter;
  final List<Rect> obstacles;

  final double _radius;
  final double _gap;
  final List<Offset> _centers = <Offset>[];
  final List<Offset> _velocities = <Offset>[];
  bool _fits = false;

  /// Creates randomized non-overlapping bubble positions.
  ///
  /// Throws [ArgumentError] for non-finite or invalid geometry
  /// (bad [count], non-positive or non-finite [size]/[diameter],
  /// non-finite obstacle coordinates, or non-finite [size]).
  BubblePhysics({
    required this.count,
    required this.size,
    required this.diameter,
    required Random random,
    List<Rect> obstacles = const <Rect>[],
  }) : obstacles = List<Rect>.unmodifiable(obstacles),
       _radius = diameter / 2,
       _gap = _spawnGap(diameter) {
    _validateGeometry(count, size, diameter, obstacles);
    _spawn(random);
  }

  /// Top-left coordinates of each bubble's bounding box.
  ///
  /// Returns an immutable copy on every access so callers cannot mutate
  /// simulation state.
  List<Offset> get positions => List<Offset>.unmodifiable(
    _centers.map((c) => Offset(c.dx - _radius, c.dy - _radius)),
  );

  /// Whether all [count] bubbles were placed safely.
  ///
  /// When false, [positions] is empty and [step] is a no-op; the UI should
  /// use its accessible flow fallback.
  bool get fits => _fits;

  /// Advances the simulation by [seconds] (clamped to [maxDt]).
  ///
  /// Throws [ArgumentError] for non-finite [seconds]. Non-positive values
  /// are a no-op. Work per call is bounded: at most 12 substeps over at
  /// most 5 bodies (10 pairs) and the obstacle list.
  void step(double seconds) {
    if (!seconds.isFinite) {
      throw ArgumentError.value(seconds, 'seconds', 'must be finite');
    }
    if (seconds <= 0 || !_fits || _centers.isEmpty) return;
    double dt = seconds;
    if (dt > maxDt) dt = maxDt;
    int n = (dt / _subDt).ceil().clamp(1, 12);
    final double h = dt / n;
    for (int s = 0; s < n; s++) {
      final previous = List<Offset>.of(_centers);
      final velocities = List<Offset>.of(_velocities);
      _integrate(h);
      for (int i = 0; i < _centers.length; i++) {
        _resolveWalls(i);
      }
      for (int i = 0; i < _centers.length; i++) {
        _resolveObstacles(i);
      }
      _resolvePairs();
      for (int i = 0; i < _centers.length; i++) {
        _clampInside(i);
      }
      // Pair separation can push a body back into a wall or obstacle. Reject
      // that substep atomically rather than exposing an overlapping control.
      final blocked = _invalidBodies();
      if (blocked.isNotEmpty) {
        _centers
          ..clear()
          ..addAll(previous);
        _velocities
          ..clear()
          ..addAll(velocities);
        for (final i in blocked) {
          _velocities[i] = -velocities[i];
        }
      }
    }
  }

  // ----- spawn -----

  static double _spawnGap(double diameter) {
    final double g = diameter * 0.25;
    return g < 2.0 ? g : 2.0;
  }

  static void _validateGeometry(
    int count,
    Size size,
    double diameter,
    List<Rect> obstacles,
  ) {
    if (count < 1 || count > maxCount) {
      throw ArgumentError.value(count, 'count', 'must be in 1..$maxCount');
    }
    if (!size.width.isFinite || !size.height.isFinite) {
      throw ArgumentError.value(size, 'size', 'must be finite');
    }
    if (size.width <= 0 || size.height <= 0) {
      throw ArgumentError.value(size, 'size', 'must be positive');
    }
    if (!diameter.isFinite) {
      throw ArgumentError.value(diameter, 'diameter', 'must be finite');
    }
    if (diameter <= 0) {
      throw ArgumentError.value(diameter, 'diameter', 'must be positive');
    }
    for (int i = 0; i < obstacles.length; i++) {
      final Rect r = obstacles[i];
      if (!r.left.isFinite ||
          !r.top.isFinite ||
          !r.right.isFinite ||
          !r.bottom.isFinite) {
        throw ArgumentError.value(r, 'obstacles[$i]', 'must be finite');
      }
    }
  }

  bool _centerClearOfObstacles(Offset c) {
    final double keepOut = _radius + _gap;
    for (final Rect r in obstacles) {
      final double qx = c.dx.clamp(r.left, r.right);
      final double qy = c.dy.clamp(r.top, r.bottom);
      final double dx = c.dx - qx;
      final double dy = c.dy - qy;
      if (dx * dx + dy * dy < keepOut * keepOut) return false;
    }
    return true;
  }

  bool _centerClearOfPlaced(Offset c) {
    final double minDist = diameter + _gap;
    for (final Offset p in _centers) {
      final double dx = c.dx - p.dx;
      final double dy = c.dy - p.dy;
      if (dx * dx + dy * dy < minDist * minDist) return false;
    }
    return true;
  }

  void _spawn(Random random) {
    if (size.width < diameter || size.height < diameter) {
      _fits = false;
      return;
    }
    final double rangeX = size.width - diameter;
    final double rangeY = size.height - diameter;

    int attempts = 0;
    while (_centers.length < count && attempts < _maxSpawnAttempts) {
      attempts++;
      final double left = rangeX <= 0 ? 0 : random.nextDouble() * rangeX;
      final double top = rangeY <= 0 ? 0 : random.nextDouble() * rangeY;
      final Offset c = Offset(left + _radius, top + _radius);
      if (!_centerClearOfObstacles(c)) continue;
      if (!_centerClearOfPlaced(c)) continue;
      _centers.add(c);
    }

    if (_centers.length < count) {
      _centers.clear();
      _spawnGridFallback(random);
    }

    if (_centers.length < count) {
      // Impossible geometry: expose no unsafe positions.
      _centers.clear();
      _velocities.clear();
      _fits = false;
      return;
    }

    for (int i = 0; i < _centers.length; i++) {
      final double angle = random.nextDouble() * 2 * pi;
      final double speed = 10 + random.nextDouble() * 10;
      _velocities.add(Offset(cos(angle) * speed, sin(angle) * speed));
    }
    _fits = true;
  }

  /// Bounded shuffled-grid fallback: enumerates grid centers (coarsened
  /// until the candidate list is small), shuffles with [random] so distinct
  /// seeds still differ, then greedily keeps valid cells.
  void _spawnGridFallback(Random random) {
    // A full-diameter grid avoids inheriting a jammed partial random packing,
    // particularly in one-column, large-text layouts.
    double stepLen = diameter + _gap;
    if (!stepLen.isFinite || stepLen <= 0) return;
    if (stepLen < 2.0) stepLen = 2.0;
    List<Offset> cells = <Offset>[];
    // Coarsen until bounded.
    while (true) {
      cells = _gridCells(stepLen);
      if (cells.length <= _maxGridCandidates || stepLen > diameter + _gap) {
        break;
      }
      stepLen *= 2;
    }
    // Fisher-Yates shuffle driven by the seeded random.
    for (int i = cells.length - 1; i > 0; i--) {
      final int j = random.nextInt(i + 1);
      final Offset t = cells[i];
      cells[i] = cells[j];
      cells[j] = t;
    }
    for (final Offset c in cells) {
      if (_centers.length >= count) break;
      if (!_centerClearOfObstacles(c)) continue;
      if (!_centerClearOfPlaced(c)) continue;
      _centers.add(c);
    }
  }

  List<Offset> _gridCells(double stepLen) {
    final List<Offset> cells = <Offset>[];
    final double x0 = _radius;
    final double x1 = size.width - _radius;
    final double y0 = _radius;
    final double y1 = size.height - _radius;
    if (x1 < x0 || y1 < y0) return cells;
    for (double y = y0; y <= y1 + 1e-9; y += stepLen) {
      final double yy = y > y1 ? y1 : y;
      for (double x = x0; x <= x1 + 1e-9; x += stepLen) {
        final double xx = x > x1 ? x1 : x;
        cells.add(Offset(xx, yy));
        if (cells.length > _maxGridCandidates * 2) return cells;
      }
    }
    return cells;
  }

  // ----- integration & collisions -----

  Set<int> _invalidBodies() {
    final invalid = <int>{};
    for (var i = 0; i < count; i++) {
      final c = _centers[i];
      if (c.dx < _radius - 1e-7 ||
          c.dy < _radius - 1e-7 ||
          c.dx > size.width - _radius + 1e-7 ||
          c.dy > size.height - _radius + 1e-7) {
        invalid.add(i);
      }
      for (final r in obstacles) {
        final closest = Offset(
          c.dx.clamp(r.left, r.right),
          c.dy.clamp(r.top, r.bottom),
        );
        if ((c - closest).distanceSquared < _radius * _radius - 1e-6) {
          invalid.add(i);
        }
      }
      for (var j = i + 1; j < count; j++) {
        if ((c - _centers[j]).distanceSquared < diameter * diameter - 1e-6) {
          invalid.addAll([i, j]);
        }
      }
    }
    return invalid;
  }

  void _integrate(double h) {
    for (int i = 0; i < _centers.length; i++) {
      final Offset c = _centers[i];
      final Offset v = _velocities[i];
      _centers[i] = Offset(c.dx + v.dx * h, c.dy + v.dy * h);
    }
  }

  void _resolveWalls(int i) {
    Offset c = _centers[i];
    Offset v = _velocities[i];
    double x = c.dx;
    double y = c.dy;
    double vx = v.dx;
    double vy = v.dy;
    if (x - _radius < 0) {
      x = _radius;
      vx = vx.abs();
    } else if (x + _radius > size.width) {
      x = size.width - _radius;
      vx = -vx.abs();
    }
    if (y - _radius < 0) {
      y = _radius;
      vy = vy.abs();
    } else if (y + _radius > size.height) {
      y = size.height - _radius;
      vy = -vy.abs();
    }
    _centers[i] = Offset(x, y);
    _velocities[i] = Offset(vx, vy);
  }

  void _resolveObstacles(int i) {
    for (final Rect r in obstacles) {
      Offset c = _centers[i];
      final double qx = c.dx.clamp(r.left, r.right);
      final double qy = c.dy.clamp(r.top, r.bottom);
      double dx = c.dx - qx;
      double dy = c.dy - qy;
      final double distSq = dx * dx + dy * dy;
      if (distSq >= _radius * _radius) continue;
      Offset v = _velocities[i];
      if (distSq > 1e-12) {
        final double dist = sqrt(distSq);
        final double nx = dx / dist;
        final double ny = dy / dist;
        c = Offset(qx + nx * _radius, qy + ny * _radius);
        final double vn = v.dx * nx + v.dy * ny;
        if (vn < 0) {
          v = Offset(v.dx - 2 * vn * nx, v.dy - 2 * vn * ny);
        }
      } else {
        // Center inside (or exactly on) the rect: exit via nearest edge.
        final double toLeft = c.dx - r.left;
        final double toRight = r.right - c.dx;
        final double toTop = c.dy - r.top;
        final double toBottom = r.bottom - c.dy;
        double m = toLeft;
        int side = 0;
        if (toRight < m) {
          m = toRight;
          side = 1;
        }
        if (toTop < m) {
          m = toTop;
          side = 2;
        }
        if (toBottom < m) {
          m = toBottom;
          side = 3;
        }
        switch (side) {
          case 0:
            c = Offset(r.left - _radius, c.dy);
            v = Offset(-v.dx.abs(), v.dy);
            break;
          case 1:
            c = Offset(r.right + _radius, c.dy);
            v = Offset(v.dx.abs(), v.dy);
            break;
          case 2:
            c = Offset(c.dx, r.top - _radius);
            v = Offset(v.dx, -v.dy.abs());
            break;
          default:
            c = Offset(c.dx, r.bottom + _radius);
            v = Offset(v.dx, v.dy.abs());
            break;
        }
      }
      _centers[i] = c;
      _velocities[i] = v;
    }
    _clampInside(i);
  }

  void _resolvePairs() {
    // Two relaxation passes keep small stacks stable; still bounded
    // (<= 10 pairs x 2 passes for 5 bodies).
    for (int pass = 0; pass < 2; pass++) {
      for (int i = 0; i < _centers.length; i++) {
        for (int j = i + 1; j < _centers.length; j++) {
          _resolvePair(i, j);
        }
      }
    }
  }

  void _resolvePair(int i, int j) {
    final Offset a = _centers[i];
    final Offset b = _centers[j];
    double dx = b.dx - a.dx;
    double dy = b.dy - a.dy;
    final double distSq = dx * dx + dy * dy;
    if (distSq >= diameter * diameter) return;
    double nx;
    double ny;
    double dist;
    if (distSq > 1e-12) {
      dist = sqrt(distSq);
      nx = dx / dist;
      ny = dy / dist;
    } else {
      nx = 1;
      ny = 0;
      dist = 0;
    }
    // Minimal positional separation: no teleportation.
    final double overlap = diameter - dist;
    final double push = overlap / 2;
    _centers[i] = Offset(a.dx - nx * push, a.dy - ny * push);
    _centers[j] = Offset(b.dx + nx * push, b.dy + ny * push);
    // Equal-mass elastic exchange of the normal velocity components.
    final Offset va = _velocities[i];
    final Offset vb = _velocities[j];
    final double rel = (va.dx - vb.dx) * nx + (va.dy - vb.dy) * ny;
    if (rel > 0) {
      _velocities[i] = Offset(va.dx - rel * nx, va.dy - rel * ny);
      _velocities[j] = Offset(vb.dx + rel * nx, vb.dy + rel * ny);
    }
  }

  /// Final containment without teleporting: position-only clamp so pair
  /// separation can never leave a circle outside the area.
  void _clampInside(int i) {
    final Offset c = _centers[i];
    final double x = c.dx.clamp(_radius, size.width - _radius);
    final double y = c.dy.clamp(_radius, size.height - _radius);
    if (x != c.dx || y != c.dy) {
      _centers[i] = Offset(x, y);
    }
  }
}
