import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'bubble_physics.dart';

/// Owns presentation frames only. The setup controls keep their own identities.
class FloatingBubbles extends StatefulWidget {
  const FloatingBubbles({
    super.key,
    required this.children,
    required this.diameter,
    required this.enabled,
    required this.paused,
    this.heldKeys = const {},
    this.hiddenKeys = const {},
    this.onDragStarted,
    this.onDragEnded,
    this.obstacles = const [],
    required this.fallback,
    this.random,
  });

  final Map<String, Widget> children;
  final double diameter;
  final bool enabled, paused;
  final Set<String> heldKeys, hiddenKeys;
  final ValueChanged<String>? onDragStarted, onDragEnded;
  final List<Rect> obstacles;
  final Widget fallback;

  /// Optional entropy for reproducible simulations, captured once at mount.
  /// Production callers omit this and keep fresh randomized layouts.
  final Random? random;

  @override
  State<FloatingBubbles> createState() => _FloatingBubblesState();
}

class _BodyGesture {
  const _BodyGesture(this.physics, this.generation, this.index, this.pointer);

  final BubblePhysics physics;
  final int generation, index, pointer;
}

class _FloatingBubblesState extends State<FloatingBubbles>
    with SingleTickerProviderStateMixin, WidgetsBindingObserver {
  late final Random _random;
  late final Ticker _ticker;
  BubblePhysics? _physics;
  Size? _size;
  Duration? _lastTick;
  bool _foreground = true;
  int _generation = 0;
  final _pending = <String, _BodyGesture>{};
  final _drags = <String, _BodyGesture>{};
  final _ended = <String>{};
  bool _endScheduled = false;

  bool get _motionAllowed =>
      widget.enabled &&
      !widget.paused &&
      _foreground &&
      TickerMode.valuesOf(context).enabled &&
      !MediaQuery.disableAnimationsOf(context);

  Set<int> get _held => {
    for (final (index, key) in widget.children.keys.indexed)
      if (widget.heldKeys.contains(key) || _drags.containsKey(key)) index,
  };

  bool _bodyCurrent(String key, int index, BubblePhysics physics) =>
      mounted &&
      _foreground &&
      identical(_physics, physics) &&
      physics.fits &&
      index < widget.children.length &&
      widget.children.keys.elementAt(index) == key &&
      !widget.hiddenKeys.contains(key);

  bool _owns(String key, _BodyGesture owner) =>
      owner.generation == _generation &&
      _bodyCurrent(key, owner.index, owner.physics);

  void _pointerDown(
    String key,
    int index,
    BubblePhysics physics,
    PointerDownEvent event,
  ) {
    if (!_bodyCurrent(key, index, physics) || _pending.containsKey(key)) return;
    _pending[key] = _BodyGesture(physics, _generation, index, event.pointer);
  }

  void _pointerFinished(String key, BubblePhysics physics, PointerEvent event) {
    final pending = _pending[key];
    if (pending?.pointer == event.pointer &&
        identical(pending?.physics, physics)) {
      _pending.remove(key);
    }
  }

  void _startDrag(String key, BubblePhysics physics) {
    final owner = _pending[key];
    if (owner == null ||
        !identical(owner.physics, physics) ||
        !_owns(key, owner)) {
      return;
    }
    if (_drags.containsKey(key)) return;
    setState(() => _drags[key] = owner);
    widget.onDragStarted?.call(key);
  }

  void _updateDrag(
    String key,
    BubblePhysics physics,
    DragUpdateDetails details,
  ) {
    final owner = _drags[key];
    if (owner == null ||
        !identical(owner.physics, physics) ||
        !_owns(key, owner)) {
      return;
    }
    setState(() {
      physics.moveBody(
        owner.index,
        physics.positions[owner.index] + details.delta,
        held: _held,
      );
    });
  }

  void _finishDrag(String key, BubblePhysics physics, Offset velocity) {
    final owner = _drags[key];
    if (owner == null ||
        !identical(owner.physics, physics) ||
        !_owns(key, owner)) {
      return;
    }
    setState(() {
      _drags.remove(key);
      physics.launch(owner.index, _motionAllowed ? velocity : Offset.zero);
    });
    widget.onDragEnded?.call(key);
  }

  void _queueEnded(Iterable<String> keys) {
    _ended.addAll(keys);
    if (_ended.isEmpty || _endScheduled) return;
    _endScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _endScheduled = false;
      if (!mounted) return;
      final ended = Set<String>.of(_ended);
      _ended.clear();
      for (final key in ended) {
        if (!_drags.containsKey(key)) widget.onDragEnded?.call(key);
      }
    });
  }

  void _cancelGestures({bool notify = true}) {
    final cancelled = _drags.keys.toList();
    for (final owner in _drags.values) {
      owner.physics.launch(owner.index, Offset.zero);
    }
    _drags.clear();
    _pending.clear();
    _generation++;
    // Geometry may be replaced from build/didUpdateWidget. Parent callbacks
    // can rebuild the setup, so deliver cancellation only after that frame.
    if (notify) _queueEnded(cancelled);
  }

  void _resetLaunches() {
    final physics = _physics;
    if (physics?.fits != true) return;
    for (var i = 0; i < widget.children.length; i++) {
      physics!.launch(i, Offset.zero);
    }
  }

  @override
  void initState() {
    super.initState();
    _random = widget.random ?? Random();
    _foreground =
        WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
    WidgetsBinding.instance.addObserver(this);
    _ticker = createTicker(_tick);
  }

  void _tick(Duration elapsed) {
    final previous = _lastTick;
    _lastTick = elapsed;
    if (previous == null || _physics?.fits != true) return;
    setState(() {
      _physics!.step(
        (elapsed - previous).inMicroseconds / 1000000,
        held: _held,
      );
    });
  }

  void _syncTicker() {
    final run = _motionAllowed && _physics?.fits == true;
    if (run == _ticker.isActive) return;
    _lastTick = null;
    if (run) {
      _ticker.start();
    } else {
      _resetLaunches();
      _ticker.stop();
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _syncTicker();
  }

  @override
  void didUpdateWidget(covariant FloatingBubbles oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.diameter != oldWidget.diameter ||
        !listEquals(
          widget.children.keys.toList(),
          oldWidget.children.keys.toList(),
        ) ||
        !listEquals(widget.obstacles, oldWidget.obstacles)) {
      _cancelGestures();
      _physics = null;
    }
    for (final key in _drags.keys.where(widget.hiddenKeys.contains).toList()) {
      final owner = _drags.remove(key)!;
      owner.physics.launch(owner.index, Offset.zero);
      _pending.remove(key);
      _queueEnded([key]);
    }
    _syncTicker();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    if (!_foreground) _cancelGestures();
    _syncTicker();
  }

  @override
  void dispose() {
    _cancelGestures(notify: false);
    WidgetsBinding.instance.removeObserver(this);
    _ticker.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, box) {
      final size = box.biggest;
      if (_size != size || _physics == null) {
        _cancelGestures();
        _size = size;
        _physics = BubblePhysics(
          count: widget.children.length,
          size: size,
          diameter: widget.diameter,
          random: _random,
          obstacles: widget.obstacles,
        );
        _lastTick = null;
      }
      _syncTicker();
      if (!_physics!.fits) return widget.fallback;
      final positions = _physics!.positions;
      final physics = _physics!;
      final entries = widget.children.entries.toList();
      return Stack(
        clipBehavior: Clip.hardEdge,
        children: [
          for (var i = 0; i < entries.length; i++)
            Positioned(
              key: ValueKey('floating-${entries[i].key}'),
              left: positions[i].dx,
              top: positions[i].dy,
              width: widget.diameter,
              height: widget.diameter,
              child: FocusTraversalOrder(
                order: NumericFocusOrder(i.toDouble()),
                child: Opacity(
                  opacity: widget.hiddenKeys.contains(entries[i].key) ? 0 : 1,
                  child: IgnorePointer(
                    ignoring: widget.hiddenKeys.contains(entries[i].key),
                    child: ExcludeFocus(
                      excluding: widget.hiddenKeys.contains(entries[i].key),
                      child: ExcludeSemantics(
                        excluding: widget.hiddenKeys.contains(entries[i].key),
                        child: Listener(
                          onPointerDown: (event) =>
                              _pointerDown(entries[i].key, i, physics, event),
                          onPointerUp: (event) =>
                              _pointerFinished(entries[i].key, physics, event),
                          onPointerCancel: (event) =>
                              _pointerFinished(entries[i].key, physics, event),
                          child: GestureDetector(
                            behavior: HitTestBehavior.deferToChild,
                            onPanStart: (_) =>
                                _startDrag(entries[i].key, physics),
                            onPanUpdate: (details) =>
                                _updateDrag(entries[i].key, physics, details),
                            onPanEnd: (details) => _finishDrag(
                              entries[i].key,
                              physics,
                              details.velocity.pixelsPerSecond,
                            ),
                            onPanCancel: () => _finishDrag(
                              entries[i].key,
                              physics,
                              Offset.zero,
                            ),
                            child: entries[i].value,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
        ],
      );
    },
  );
}
