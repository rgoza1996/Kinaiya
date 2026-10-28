import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'visual_theme.dart';

// Decorative lunar backdrop. Presentation only: no state, timers, images,
// network, audio, input, or animation. The lead-supplied child (mascot,
// transcript, controls) paints above this scenery and stays interactive.
class MoonlitScene extends StatelessWidget {
  const MoonlitScene({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return ClipRect(
      child: Stack(
        children: <Widget>[
          const Positioned.fill(
            child: ExcludeSemantics(
              child: IgnorePointer(
                child: RepaintBoundary(
                  child: CustomPaint(painter: _MoonlitPainter()),
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

// Deterministic painter: fixed primitive counts, no per-frame randomness.
// Size-driven repaint happens automatically, so shouldRepaint is false.
class _MoonlitPainter extends CustomPainter {
  const _MoonlitPainter();

  static double _frac(double x) => x - x.floor();

  @override
  void paint(Canvas canvas, Size size) {
    if (size.width <= 0 || size.height <= 0) return;
    canvas.clipRect(Offset.zero & size);

    final double w = size.width;
    final double h = size.height;
    final double span = math.max(1.0, math.min(w, h));
    final double strokeScale = math.min(2.0, math.max(0.8, span / 400.0));

    // Indigo sky fading into deep navy.
    final Paint sky = Paint()
      ..shader = const LinearGradient(
        begin: Alignment.topCenter,
        end: Alignment.bottomCenter,
        colors: <Color>[kinaiyaIndigo, Color(0xFF1A2160), kinaiyaNavy],
        stops: <double>[0.0, 0.52, 1.0],
      ).createShader(Rect.fromLTWH(0, 0, w, h));
    canvas.drawRect(Offset.zero & size, sky);

    // Deterministic upper-sky stars.
    const int starCount = 36;
    for (int i = 0; i < starCount; i++) {
      final double fx = _frac(i * 0.7173 + 0.13);
      final double fy = _frac(i * 0.3731 + 0.07);
      final double x = fx * w;
      final double y = fy * h * 0.56;
      final double r = (0.8 + (i % 3) * 0.5) * strokeScale;
      final double alpha = 0.28 + (_frac(i * 0.41) * 0.5);
      canvas.drawCircle(
        Offset(x, y),
        math.max(0.5, r),
        Paint()..color = kinaiyaWhite.withValues(alpha: alpha),
      );
    }

    // Oversized white moon entering from the top, centered.
    final double moonR = math.min(w * 0.48, math.max(100.0, h * 0.78));
    final Offset moonCenter = Offset(w * 0.5, -moonR * 0.36);
    canvas.drawCircle(
      moonCenter,
      math.max(0.0, moonR * 1.26),
      Paint()..color = kinaiyaWhite.withValues(alpha: 0.06),
    );
    canvas.drawCircle(
      moonCenter,
      math.max(0.0, moonR * 1.12),
      Paint()..color = kinaiyaWhite.withValues(alpha: 0.07),
    );
    canvas.drawCircle(
      moonCenter,
      math.max(0.0, moonR),
      Paint()..color = kinaiyaWhite,
    );

    // Deterministic craters kept inside the lunar disc.
    const int craterCount = 12;
    for (int i = 0; i < craterCount; i++) {
      final double angle = i * 2.399963 + 0.5;
      final double spread = math.sqrt((i + 0.7) / 12.7) * moonR * 0.72;
      final Offset c =
          moonCenter +
          Offset(math.cos(angle) * spread, math.sin(angle) * spread * 0.85);
      final double f = _frac(i * 0.377 + 0.2);
      final double cr = moonR * (0.045 + 0.055 * f);
      canvas.drawCircle(
        c,
        math.max(0.0, cr),
        Paint()..color = const Color(0xFFDCE2F8),
      );
    }

    // Quiet curving orbit arcs around the moon, upper area only.
    const List<double> orbitAlphas = <double>[0.30, 0.18, 0.11];
    for (int o = 0; o < orbitAlphas.length; o++) {
      final Rect orbit = Rect.fromCenter(
        center: moonCenter,
        width: math.max(0.0, moonR * 2 * (1.45 + o * 0.38)),
        height: math.max(0.0, moonR * 2 * (0.50 + o * 0.20)),
      );
      canvas.drawArc(
        orbit,
        0.15 * math.pi,
        0.70 * math.pi,
        false,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.2 * strokeScale
          ..color = kinaiyaWhite.withValues(alpha: orbitAlphas[o]),
      );
    }

    // Small crescent accent, kept clear of the center content column.
    final double crescentR = math.max(6.0, span * 0.028);
    final Offset crescent = Offset(w * 0.84, h * 0.16);
    canvas.drawArc(
      Rect.fromCircle(center: crescent, radius: crescentR),
      -1.1,
      3.6,
      false,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.6 * strokeScale
        ..color = kinaiyaWhite.withValues(alpha: 0.55),
    );

    // Soft readability veil: open sky at top, deeper navy where the
    // 220-320px mascot, transcript, and controls sit.
    final Paint veil = Paint()
      ..shader = LinearGradient(
        begin: Alignment.topCenter,
        end: Alignment.bottomCenter,
        colors: <Color>[
          kinaiyaNavy.withValues(alpha: 0.0),
          kinaiyaNavy.withValues(alpha: 0.0),
          kinaiyaNavy.withValues(alpha: 0.34),
          kinaiyaNavy.withValues(alpha: 0.55),
        ],
        stops: const <double>[0.0, 0.40, 0.68, 1.0],
      ).createShader(Rect.fromLTWH(0, 0, w, h));
    canvas.drawRect(Offset.zero & size, veil);

    // Broad moonlight on water, widening into the foreground. Fixed samples
    // keep the painted edges stable across frames and viewport sizes.
    final double waterTop = h * 0.66;
    final double waterH = h - waterTop;
    final double cx = w * 0.5;
    final Path reflection = Path();
    const int edgeSamples = 48;
    for (int side = 0; side < 2; side++) {
      for (int sample = 0; sample <= edgeSamples; sample++) {
        final int i = side == 0 ? sample : edgeSamples - sample;
        final double t = i / edgeSamples;
        final double halfWidth =
            w * (0.12 + 0.30 * t + 0.055 * math.sin(t * math.pi * 11));
        final double x = cx + (side == 0 ? -halfWidth : halfWidth);
        final double y = waterTop + t * waterH;
        if (side == 0 && sample == 0) {
          reflection.moveTo(x, y);
        } else {
          reflection.lineTo(x, y);
        }
      }
    }
    reflection.close();
    canvas.drawPath(
      reflection,
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: <Color>[Color(0xFFCDD7FF), kinaiyaWhite],
        ).createShader(Rect.fromLTWH(0, waterTop, w, waterH)),
    );

    // Elliptical ripples surround the mascot's real sprite, without baking
    // the mascot or any status/control text into the decoration.
    for (int i = 0; i < 4; i++) {
      canvas.drawOval(
        Rect.fromCenter(
          center: Offset(cx, waterTop + 12 + i * 6),
          width: w * (0.32 + i * 0.18),
          height: 12 + i * 12,
        ),
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2.0 + i
          ..color = kinaiyaWhite.withValues(alpha: 0.86 - i * 0.10),
      );
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
