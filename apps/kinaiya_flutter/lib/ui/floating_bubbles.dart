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
    this.obstacles = const [],
    required this.fallback,
  });

  final Map<String, Widget> children;
  final double diameter;
  final bool enabled, paused;
  final List<Rect> obstacles;
  final Widget fallback;

  @override
  State<FloatingBubbles> createState() => _FloatingBubblesState();
}

class _FloatingBubblesState extends State<FloatingBubbles>
    with SingleTickerProviderStateMixin, WidgetsBindingObserver {
  final _random = Random();
  late final Ticker _ticker;
  BubblePhysics? _physics;
  Size? _size;
  Duration? _lastTick;
  bool _foreground = true;

  @override
  void initState() {
    super.initState();
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
      _physics!.step((elapsed - previous).inMicroseconds / 1000000);
    });
  }

  void _syncTicker() {
    final run =
        widget.enabled &&
        !widget.paused &&
        _foreground &&
        TickerMode.valuesOf(context).enabled &&
        !MediaQuery.disableAnimationsOf(context) &&
        _physics?.fits == true;
    if (run == _ticker.isActive) return;
    _lastTick = null;
    if (run) {
      _ticker.start();
    } else {
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
        widget.children.keys.join('|') != oldWidget.children.keys.join('|') ||
        !listEquals(widget.obstacles, oldWidget.obstacles)) {
      _physics = null;
    }
    _syncTicker();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _syncTicker();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _ticker.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, box) {
      final size = box.biggest;
      if (_size != size || _physics == null) {
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
                child: entries[i].value,
              ),
            ),
        ],
      );
    },
  );
}
