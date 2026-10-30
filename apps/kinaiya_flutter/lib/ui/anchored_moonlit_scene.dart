import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'visual_theme.dart';

/// Trusted local artwork geometry. The surface anchor is normalized to its
/// source image; it is not inferred from an image or from conversation state.
class MoonlitArtwork {
  const MoonlitArtwork({
    required this.asset,
    required this.sourceSize,
    required this.surfaceAnchor,
  });

  final String asset;
  final Size sourceSize;
  final Offset surfaceAnchor;

  bool get _valid =>
      asset.isNotEmpty &&
      sourceSize.width.isFinite &&
      sourceSize.height.isFinite &&
      sourceSize.width > 0 &&
      sourceSize.height > 0 &&
      surfaceAnchor.dx.isFinite &&
      surfaceAnchor.dy.isFinite &&
      surfaceAnchor.dx > 0 &&
      surfaceAnchor.dx < 1 &&
      surfaceAnchor.dy > 0 &&
      surfaceAnchor.dy < 1;
}

/// Decorative artwork anchored to the owner's actual seated contact point.
///
/// The child determines the canvas height, including composed scroll content.
/// Artwork keeps its aspect ratio and covers the canvas without owning input,
/// capture, sessions, animation, or inference state.
class AnchoredMoonlitScene extends StatelessWidget {
  const AnchoredMoonlitScene({
    super.key,
    required this.child,
    required this.artwork,
    required this.surfaceY,
    this.primary = kinaiyaIndigo,
    this.background = kinaiyaWhite,
    this.fallback,
  });

  final Widget child;
  final MoonlitArtwork artwork;
  final double surfaceY;
  final Color primary, background;
  final Widget? fallback;

  @override
  Widget build(BuildContext context) {
    assert(
      artwork._valid,
      'Artwork needs positive finite source bounds and an interior anchor.',
    );
    assert(surfaceY.isFinite && surfaceY >= 0);
    return ClipRect(
      child: Stack(
        children: [
          Positioned.fill(
            child: ExcludeSemantics(
              child: IgnorePointer(
                child: RepaintBoundary(
                  child: LayoutBuilder(
                    builder: (_, bounds) {
                      assert(bounds.hasBoundedWidth && bounds.hasBoundedHeight);
                      final source = artwork.sourceSize;
                      final anchor = artwork.surfaceAnchor;
                      // Both horizontal terms are needed for off-center source
                      // anchors. At x=.5 they reduce to ordinary width coverage.
                      final horizontal = math.max(
                        bounds.maxWidth * .5 / (source.width * anchor.dx),
                        bounds.maxWidth * .5 / (source.width * (1 - anchor.dx)),
                      );
                      final vertical = math.max(
                        surfaceY / (source.height * anchor.dy),
                        (bounds.maxHeight - surfaceY) /
                            (source.height * (1 - anchor.dy)),
                      );
                      final scale = math.max(horizontal, vertical);
                      final width = source.width * scale;
                      final height = source.height * scale;
                      return Stack(
                        children: [
                          Positioned(
                            left: bounds.maxWidth * .5 - width * anchor.dx,
                            top: surfaceY - height * anchor.dy,
                            width: width,
                            height: height,
                            child: Image.asset(
                              artwork.asset,
                              key: const ValueKey('moonlit-illustration'),
                              fit: BoxFit.contain,
                              excludeFromSemantics: true,
                              errorBuilder: (_, _, _) =>
                                  fallback ??
                                  DecoratedBox(
                                    decoration: BoxDecoration(
                                      gradient: LinearGradient(
                                        begin: Alignment.topCenter,
                                        end: Alignment.bottomCenter,
                                        colors: [primary, background],
                                      ),
                                    ),
                                  ),
                            ),
                          ),
                        ],
                      );
                    },
                  ),
                ),
              ),
            ),
          ),
          child,
        ],
      ),
    );
  }
}
