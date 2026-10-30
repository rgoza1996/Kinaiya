import 'dart:ui' show lerpDouble;

import 'package:flutter/material.dart';

/// Expands the selected circle's actual bounds into a clipped options panel.
/// The panel retains its final layout throughout, so tiny intermediate bounds
/// never reflow or overflow its controls. The field keeps the hidden source
/// body alive and held until the reverse animation has completed.
class BubbleOptionsMorph extends StatelessWidget {
  const BubbleOptionsMorph({
    super.key,
    required this.origin,
    required this.target,
    required this.progress,
    required this.diameter,
    required this.face,
    required this.child,
  });

  final Rect origin, target;
  final double progress, diameter;
  final Widget face, child;

  @override
  Widget build(BuildContext context) {
    final t = Curves.easeInOutCubic.transform(progress.clamp(0.0, 1.0));
    final rect = Rect.lerp(origin, target, t)!;
    return Positioned.fromRect(
      rect: rect,
      child: Material(
        key: const ValueKey('setup-morph-surface'),
        color: Theme.of(context).colorScheme.surface.withValues(alpha: .98),
        elevation: 12,
        borderRadius: BorderRadius.circular(lerpDouble(diameter / 2, 20, t)!),
        clipBehavior: Clip.antiAlias,
        child: Stack(
          fit: StackFit.expand,
          children: [
            ExcludeSemantics(
              child: IgnorePointer(
                child: OverflowBox(
                  minWidth: diameter,
                  maxWidth: diameter,
                  minHeight: diameter,
                  maxHeight: diameter,
                  child: Opacity(
                    opacity: (1 - t * 3).clamp(0.0, 1.0),
                    child: face,
                  ),
                ),
              ),
            ),
            OverflowBox(
              alignment: Alignment.topLeft,
              minWidth: target.width,
              maxWidth: target.width,
              minHeight: target.height,
              maxHeight: target.height,
              child: IgnorePointer(
                ignoring: progress < 1,
                child: ExcludeSemantics(
                  excluding: progress < 1,
                  child: ExcludeFocus(
                    excluding: progress < 1,
                    child: Opacity(
                      opacity: ((t - .45) / .55).clamp(0.0, 1.0),
                      child: child,
                    ),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
