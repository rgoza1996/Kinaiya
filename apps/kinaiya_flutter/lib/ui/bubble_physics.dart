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
/// * [held] bodies stay exactly stationary during [step] and keep their
///   stored velocity for resume; other bodies treat them as immovable
///   surfaces. Pair correction never translates a held body, and only a
///   blocked collision component reverts so unrelated bodies keep progress.
/// * [moveBody] drags along a swept, clamped path (bounded work) and never
///   pushes or moves any other body (held or not).
/// * [launch] clamps to [maxLaunchSpeed]; high speeds decay smoothly with
///   time-based damping back toward ordinary drift (~10..20 px/sec).
class BubblePhysics {
  /// Maximum bodies the engine is tuned for (see [step] bounded work).
  static const int maxCount = 7;

  /// Upper bound on simulated time per [step] call (seconds).
  static const double maxDt = 0.1;

  /// Upper bound on a fling/release velocity (logical px/sec).
  static const double maxLaunchSpeed = 600;

  /// Substep length target; keeps per-substep travel well below one radius
  /// at drift speeds, and below ~5px even at [maxLaunchSpeed], so circles
  /// cannot tunnel through walls/obstacles at UI diameters >= 48.
  static const double _subDt = 1 / 120;

  /// Cap on random spawn attempts (bounded search, never unbounded).
  static const int _maxSpawnAttempts = 400;

  /// Cap on fallback grid candidates (keeps spawn bounded for tiny
  /// diameters in large areas).
  static const int _maxGridCandidates = 2000;

  /// Drag sweep bound: at most this many path samples plus a small fixed
  /// binary refinement, so huge pointer jumps cannot teleport.
  static const int _maxDragSteps = 128;

  /// Damping rate (1/sec) applied only above [_driftCeil]; time-based via
  /// `exp(-rate * h)` so decay is frame-rate independent.
  static const double _dampingRate = 1.5;

  /// Speeds at or below this need no damping (ordinary slow drift).
  static const double _driftCeil = 20;

  /// Floor for ordinary drift; zero releases resume within 10..20.
  static const double _driftFloor = 10;

  /// Nominal resume speed for a zero release when retained speed is
  /// unavailable or out of range.
  static const double _resumeSpeed = 15;

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
  /// Bodies in [held] remain exactly stationary and keep their stored
  /// velocity for resume; all other bodies integrate, damp, and collide
  /// with held circles as immovable surfaces. Throws [ArgumentError] for
  /// non-finite [seconds] or out-of-range [held] indices. Non-positive
  /// values are a no-op. Work per call is bounded: at most 12 substeps over
  /// at most 7 bodies (21 pairs) and the obstacle list.
  void step(double seconds, {Set<int> held = const <int>{}}) {
    if (!seconds.isFinite) {
      throw ArgumentError.value(seconds, 'seconds', 'must be finite');
    }
    _validateHeld(held);
    if (seconds <= 0 || !_fits || _centers.isEmpty) return;
    double dt = seconds;
    if (dt > maxDt) dt = maxDt;
    int n = (dt / _subDt).ceil().clamp(1, 12);
    final double h = dt / n;
    for (int s = 0; s < n; s++) {
      for (int i = 0; i < _centers.length; i++) {
        if (held.contains(i)) continue;
        _velocities[i] = _damped(_velocities[i], h);
      }
      final previous = List<Offset>.of(_centers);
      final prevVel = List<Offset>.of(_velocities);
      for (int i = 0; i < _centers.length; i++) {
        if (held.contains(i)) continue;
        final Offset c = _centers[i];
        final Offset v = _velocities[i];
        _centers[i] = Offset(c.dx + v.dx * h, c.dy + v.dy * h);
      }
      for (int i = 0; i < _centers.length; i++) {
        if (held.contains(i)) continue;
        _resolveWalls(i);
      }
      for (int i = 0; i < _centers.length; i++) {
        if (held.contains(i)) continue;
        _resolveObstacles(i);
      }
      final contacts = <int, Set<int>>{};
      _resolvePairs(held, contacts);
      for (int i = 0; i < _centers.length; i++) {
        if (held.contains(i)) continue;
        _clampInside(i);
      }
      // Revert an invalid collision component, then include any neighbour
      // whose current pose conflicts with that rollback. Never push a new
      // pose into an unchecked obstacle or held body. Each pass adds at
      // least one free body, so at most count passes restore a valid layout.
      // Restoring all component velocities also avoids duplicating energy
      // already transferred by an equal-mass collision. Unrelated bodies
      // keep their progress, and held positions/velocities never change.
      final reverted = <int>{};
      for (var pass = 0; pass < count; pass++) {
        final invalid = _invalidBodies();
        if (invalid.isEmpty) break;
        final restore = <int>{
          for (final i in invalid)
            if (!held.contains(i) && !reverted.contains(i)) i,
        };
        for (var expansion = 0; expansion < count; expansion++) {
          final related = <int>{for (final i in restore) ...?contacts[i]}
            ..removeWhere((i) => held.contains(i) || reverted.contains(i));
          final oldLength = restore.length;
          restore.addAll(related);
          if (restore.length == oldLength) break;
        }
        if (restore.isEmpty) break;
        final poses = List<Offset>.of(_centers);
        final reflections = <int, Offset>{
          for (final i in restore)
            if (invalid.contains(i))
              i: _reflectForInvalid(i, poses[i], poses, prevVel[i]),
        };
        for (final i in restore) {
          _centers[i] = previous[i];
          _velocities[i] = reflections[i] ?? prevVel[i];
        }
        reverted.addAll(restore);
      }
    }
  }

  /// Drags body [index] toward [topLeft] (top-left of its bounding box).
  ///
  /// Moves along a swept path with at most [_maxDragSteps] samples plus a
  /// bounded binary refinement, stopping at the last valid pose before a
  /// wall, obstacle, or neighboring circle. Never moves or changes the
  /// velocity of any other body (including held ones). Works even when
  /// [index] itself is in [held]. Throws [ArgumentError]/[RangeError] for
  /// bad [index], non-finite [topLeft], or out-of-range [held].
  void moveBody(int index, Offset topLeft, {Set<int> held = const <int>{}}) {
    _validateIndex(index);
    _validateOffset(topLeft, 'topLeft');
    _validateHeld(held);
    if (!_fits || _centers.isEmpty) return;
    final Offset start = _centers[index];
    Offset want = Offset(topLeft.dx + _radius, topLeft.dy + _radius);
    want = Offset(
      want.dx.clamp(_radius, size.width - _radius),
      want.dy.clamp(_radius, size.height - _radius),
    );
    // Degenerate area (smaller than a diameter) cannot host a drag.
    if (!_centerInside(want) && !_centerInside(start)) return;
    final delta = want - start;
    final double dist = _magnitude(delta);
    if (dist < 1e-12) return;
    final double maxStepLen = min(6, _radius / 2);
    final travel = min(dist, maxStepLen * _maxDragSteps);
    if (travel < dist) want = start + _unit(delta) * travel;
    int steps = (travel / maxStepLen).ceil().clamp(1, _maxDragSteps);
    Offset last = start;
    Offset? firstBad;
    for (int k = 1; k <= steps; k++) {
      final double t = k / steps;
      final Offset cand = Offset(
        start.dx + (want.dx - start.dx) * t,
        start.dy + (want.dy - start.dy) * t,
      );
      if (_dragSegmentValid(index, last, cand)) {
        last = cand;
      } else {
        firstBad = cand;
        break;
      }
    }
    // Bounded refinement toward contact without crossing.
    if (firstBad != null) {
      Offset lo = last;
      Offset hi = firstBad;
      for (int r = 0; r < 8; r++) {
        final Offset mid = Offset((lo.dx + hi.dx) / 2, (lo.dy + hi.dy) / 2);
        if (_dragSegmentValid(index, lo, mid)) {
          lo = mid;
        } else {
          hi = mid;
        }
      }
      last = lo;
    }
    _centers[index] = last;
  }

  /// Sets the release velocity for body [index], clamped to
  /// [maxLaunchSpeed].
  ///
  /// A zero release resumes a retained slow-drift direction (~10..20
  /// px/sec) so cancel/drop never strands a bubble. Throws for bad [index]
  /// or non-finite [velocity].
  void launch(int index, Offset velocity) {
    _validateIndex(index);
    _validateOffset(velocity, 'velocity');
    if (!_fits || _centers.isEmpty) return;
    final double speed = _magnitude(velocity);
    if (speed < 1e-9) {
      final Offset retained = _velocities[index];
      final double rs = retained.distance;
      if (rs.isFinite && rs > 1e-9) {
        final double mag = rs.clamp(_driftFloor, _driftCeil);
        _velocities[index] = Offset(
          retained.dx / rs * mag,
          retained.dy / rs * mag,
        );
      } else {
        _velocities[index] = const Offset(_resumeSpeed, 0);
      }
      return;
    }
    if (speed > maxLaunchSpeed) {
      _velocities[index] = _unit(velocity) * maxLaunchSpeed;
    } else {
      _velocities[index] = velocity;
    }
  }

  // ----- validation -----

  void _validateIndex(int index) {
    if (index < 0 || index >= count) {
      throw RangeError.range(index, 0, count - 1, 'index');
    }
    if (_fits && index >= _centers.length) {
      throw RangeError.range(index, 0, _centers.length - 1, 'index');
    }
  }

  void _validateHeld(Set<int> held) {
    for (final i in held) {
      if (i < 0 || i >= count) {
        throw RangeError.range(i, 0, count - 1, 'held');
      }
    }
  }

  static void _validateOffset(Offset o, String name) {
    if (!o.dx.isFinite || !o.dy.isFinite) {
      throw ArgumentError.value(o, name, 'must be finite');
    }
  }

  // Scaling before squaring preserves direction when finite components are
  // large enough that Offset.distance would overflow to infinity.
  static Offset _unit(Offset value) {
    final scale = max(value.dx.abs(), value.dy.abs());
    if (scale == 0) return Offset.zero;
    final x = value.dx / scale, y = value.dy / scale;
    final length = sqrt(x * x + y * y);
    return Offset(x / length, y / length);
  }

  static double _magnitude(Offset value) {
    final scale = max(value.dx.abs(), value.dy.abs());
    if (scale == 0) return 0;
    final x = value.dx / scale, y = value.dy / scale;
    return scale * sqrt(x * x + y * y);
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
    for (var i = 0; i < _centers.length; i++) {
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
      for (var j = i + 1; j < _centers.length; j++) {
        if ((c - _centers[j]).distanceSquared < diameter * diameter - 1e-6) {
          invalid.addAll([i, j]);
        }
      }
    }
    return invalid;
  }

  /// Time-based damping toward ordinary drift: speeds above [_driftCeil]
  /// decay as `ceil + (speed - ceil) * exp(-rate * h)`. Never increases
  /// speed and never drops below the ceiling here.
  Offset _damped(Offset v, double h) {
    final double speed = v.distance;
    if (!speed.isFinite || speed <= _driftCeil) return v;
    final double capped = speed > maxLaunchSpeed ? maxLaunchSpeed : speed;
    final double target =
        _driftCeil + (capped - _driftCeil) * exp(-_dampingRate * h);
    final double s = target / speed;
    return Offset(v.dx * s, v.dy * s);
  }

  Offset _capped(Offset v) {
    final double speed = _magnitude(v);
    if (speed <= maxLaunchSpeed) return v;
    return _unit(v) * maxLaunchSpeed;
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
    _velocities[i] = _capped(Offset(vx, vy));
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
      _velocities[i] = _capped(v);
    }
    _clampInside(i);
  }

  void _resolvePairs(Set<int> held, Map<int, Set<int>> contacts) {
    // Two relaxation passes keep small stacks stable; still bounded
    // (<= 21 pairs x 2 passes for 7 bodies).
    for (int pass = 0; pass < 2; pass++) {
      for (int i = 0; i < _centers.length; i++) {
        for (int j = i + 1; j < _centers.length; j++) {
          _resolvePair(i, j, held, contacts);
        }
      }
    }
  }

  void _resolvePair(int i, int j, Set<int> held, Map<int, Set<int>> contacts) {
    final bool hi = held.contains(i);
    final bool hj = held.contains(j);
    if (hi && hj) return;
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
    if (hi || hj) {
      // Immovable held surface: move only the free body by the full
      // overlap and reflect its approaching normal component. Held
      // position and stored velocity never change.
      final int m = hi ? j : i;
      // Normal points from i to j; for the mover, outward from the fixed
      // body.
      final double sign = (m == j) ? 1 : -1;
      final double overlap = diameter - dist;
      final Offset mc = _centers[m];
      _centers[m] = Offset(
        mc.dx + nx * sign * overlap,
        mc.dy + ny * sign * overlap,
      );
      final Offset vm = _velocities[m];
      // Velocity of the surface is zero (stationary); reflect approach.
      final double vn = vm.dx * (nx * sign) + vm.dy * (ny * sign);
      if (vn < 0) {
        _velocities[m] = _capped(
          Offset(vm.dx - 2 * vn * (nx * sign), vm.dy - 2 * vn * (ny * sign)),
        );
      }
      return;
    }
    contacts.putIfAbsent(i, () => <int>{}).add(j);
    contacts.putIfAbsent(j, () => <int>{}).add(i);
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
      final Offset na = Offset(va.dx - rel * nx, va.dy - rel * ny);
      final Offset nb = Offset(vb.dx + rel * nx, vb.dy + rel * ny);
      _velocities[i] = _capped(na);
      _velocities[j] = _capped(nb);
    }
  }

  /// Energy-safe fallback for a still-invalid body: position reverts to its
  /// pre-substep pose (valid by induction); velocity reflects only the
  /// offending normal component and never exceeds the pre-substep speed or
  /// [maxLaunchSpeed]. Fixes the old whole-velocity negation.
  Offset _reflectForInvalid(
    int i,
    Offset bad,
    List<Offset> poses,
    Offset prevVel,
  ) {
    double vx = prevVel.dx;
    double vy = prevVel.dy;
    final double prevSpeed = prevVel.distance;
    // Walls from the invalid pose.
    if (bad.dx - _radius < 0 || bad.dx + _radius > size.width) vx = -vx;
    if (bad.dy - _radius < 0 || bad.dy + _radius > size.height) vy = -vy;
    // Obstacles from the invalid pose.
    for (final Rect r in obstacles) {
      final double qx = bad.dx.clamp(r.left, r.right);
      final double qy = bad.dy.clamp(r.top, r.bottom);
      double dx = bad.dx - qx;
      double dy = bad.dy - qy;
      final double d2 = dx * dx + dy * dy;
      if (d2 >= _radius * _radius) continue;
      if (d2 > 1e-12) {
        final double d = sqrt(d2);
        final double nx = dx / d;
        final double ny = dy / d;
        final double vn = vx * nx + vy * ny;
        if (vn < 0) {
          vx -= 2 * vn * nx;
          vy -= 2 * vn * ny;
        }
      } else {
        vx = -vx;
      }
    }
    // Reflect an approaching component along the invalid pair's normal.
    final Offset self = bad;
    for (int j = 0; j < _centers.length; j++) {
      if (j == i) continue;
      final Offset o = poses[j];
      double dx = self.dx - o.dx;
      double dy = self.dy - o.dy;
      final double d2 = dx * dx + dy * dy;
      if (d2 >= diameter * diameter || d2 <= 1e-12) continue;
      final double d = sqrt(d2);
      final double nx = dx / d;
      final double ny = dy / d;
      final double vn = vx * nx + vy * ny;
      if (vn < 0) {
        vx -= 2 * vn * nx;
        vy -= 2 * vn * ny;
      }
    }
    Offset out = Offset(vx, vy);
    final double s = out.distance;
    if (!s.isFinite) return Offset(0, 0);
    double allow = prevSpeed.isFinite ? prevSpeed : maxLaunchSpeed;
    if (allow > maxLaunchSpeed) allow = maxLaunchSpeed;
    if (s > allow && s > 1e-12) {
      out = Offset(out.dx / s * allow, out.dy / s * allow);
    }
    return _capped(out);
  }

  bool _centerInside(Offset c) {
    return c.dx >= _radius &&
        c.dy >= _radius &&
        c.dx <= size.width - _radius &&
        c.dy <= size.height - _radius;
  }

  bool _dragSegmentValid(int index, Offset from, Offset to) {
    if (!_dragPoseValid(index, to)) return false;
    for (var j = 0; j < _centers.length; j++) {
      if (j == index) continue;
      if (_pointSegmentDistanceSquared(_centers[j], from, to) <
          diameter * diameter - 1e-6) {
        return false;
      }
    }
    for (final r in obstacles) {
      if (_segmentRectDistanceSquared(from, to, r) < _radius * _radius - 1e-6) {
        return false;
      }
    }
    return true;
  }

  static double _pointSegmentDistanceSquared(Offset p, Offset a, Offset b) {
    final d = b - a;
    final length = d.distanceSquared;
    if (length < 1e-24) return (p - a).distanceSquared;
    final projection = ((p.dx - a.dx) * d.dx + (p.dy - a.dy) * d.dy) / length;
    // An indeterminate projection on extreme geometry must refuse motion.
    if (projection.isNaN) return 0;
    final nearest = a + d * projection.clamp(0.0, 1.0);
    return (p - nearest).distanceSquared;
  }

  static double _segmentRectDistanceSquared(Offset a, Offset b, Rect r) {
    var enter = 0.0, exit = 1.0;
    final delta = b - a;
    for (var axis = 0; axis < 2; axis++) {
      final start = axis == 0 ? a.dx : a.dy;
      final direction = axis == 0 ? delta.dx : delta.dy;
      final low = axis == 0 ? r.left : r.top;
      final high = axis == 0 ? r.right : r.bottom;
      if (direction.abs() < 1e-12) {
        if (start < low || start > high) {
          enter = 2;
          break;
        }
      } else {
        final first = (low - start) / direction;
        final second = (high - start) / direction;
        enter = max(enter, min(first, second));
        exit = min(exit, max(first, second));
      }
    }
    if (enter <= exit) return 0;
    double pointRect(Offset p) =>
        (p - Offset(p.dx.clamp(r.left, r.right), p.dy.clamp(r.top, r.bottom)))
            .distanceSquared;
    var distance = min(pointRect(a), pointRect(b));
    for (final corner in [r.topLeft, r.topRight, r.bottomLeft, r.bottomRight]) {
      distance = min(distance, _pointSegmentDistanceSquared(corner, a, b));
    }
    return distance;
  }

  bool _dragPoseValid(int index, Offset cand) {
    if (cand.dx < _radius - 1e-9 ||
        cand.dy < _radius - 1e-9 ||
        cand.dx > size.width - _radius + 1e-9 ||
        cand.dy > size.height - _radius + 1e-9) {
      return false;
    }
    final double keep = _radius * _radius - 1e-6;
    for (final Rect r in obstacles) {
      final double qx = cand.dx.clamp(r.left, r.right);
      final double qy = cand.dy.clamp(r.top, r.bottom);
      final double dx = cand.dx - qx;
      final double dy = cand.dy - qy;
      if (dx * dx + dy * dy < keep) return false;
    }
    for (int j = 0; j < _centers.length; j++) {
      if (j == index) continue;
      final Offset o = _centers[j];
      final double dx = cand.dx - o.dx;
      final double dy = cand.dy - o.dy;
      if (dx * dx + dy * dy < diameter * diameter - 1e-6) return false;
    }
    return true;
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
