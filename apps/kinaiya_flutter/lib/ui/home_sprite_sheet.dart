import 'package:flutter/material.dart';

/// A local 2×2 Home sprite sheet. Each frame is a 3:2 landscape cell.
class HomeSpriteSheet extends StatefulWidget {
  const HomeSpriteSheet({
    super.key,
    required this.asset,
    this.frame,
    this.animate = true,
    this.frameOffsets = const [],
  });

  final String asset;
  final int? frame;
  final bool animate;

  /// Per-frame placement correction in fractions of a cell's width and height.
  final List<Offset> frameOffsets;

  @override
  State<HomeSpriteSheet> createState() => _HomeSpriteSheetState();
}

class _HomeSpriteSheetState extends State<HomeSpriteSheet>
    with SingleTickerProviderStateMixin {
  late final AnimationController _clock;

  @override
  void initState() {
    super.initState();
    _clock = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 4),
    );
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _sync();
  }

  @override
  void didUpdateWidget(covariant HomeSpriteSheet oldWidget) {
    super.didUpdateWidget(oldWidget);
    _sync();
  }

  void _sync() {
    final playing =
        widget.animate &&
        widget.frame == null &&
        !MediaQuery.of(context).disableAnimations &&
        TickerMode.valuesOf(context).enabled;
    if (playing && !_clock.isAnimating) {
      _clock.repeat();
    } else if (!playing && _clock.isAnimating) {
      _clock.stop();
    }
  }

  @override
  void dispose() {
    _clock.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) => AnimatedBuilder(
      animation: _clock,
      builder: (context, _) {
        const cycle = [0, 1, 2, 3, 2, 1];
        final index =
            (widget.frame ??
                    (widget.animate && !MediaQuery.of(context).disableAnimations
                        ? cycle[(_clock.value * cycle.length).floor().clamp(
                            0,
                            cycle.length - 1,
                          )]
                        : 0))
                .clamp(0, 3);
        final width = constraints.maxWidth, height = constraints.maxHeight;
        final offset = index < widget.frameOffsets.length
            ? widget.frameOffsets[index]
            : Offset.zero;
        return RepaintBoundary(
          child: ClipRect(
            child: Transform.translate(
              offset: Offset(offset.dx * width, offset.dy * height),
              child: ClipRect(
                child: OverflowBox(
                  alignment: Alignment.topLeft,
                  minWidth: width * 2,
                  maxWidth: width * 2,
                  minHeight: height * 2,
                  maxHeight: height * 2,
                  child: Transform.translate(
                    offset: Offset(
                      -(index % 2) * width,
                      -(index ~/ 2) * height,
                    ),
                    child: Image.asset(
                      widget.asset,
                      width: width * 2,
                      height: height * 2,
                      fit: BoxFit.fill,
                      excludeFromSemantics: true,
                      errorBuilder: (_, _, _) =>
                          const Icon(Icons.bedtime_outlined),
                    ),
                  ),
                ),
              ),
            ),
          ),
        );
      },
    ),
  );
}
