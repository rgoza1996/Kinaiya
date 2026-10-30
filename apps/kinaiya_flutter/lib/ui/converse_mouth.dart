import 'package:flutter/material.dart';

/// Mouth cues only affect the bundled Converse portrait. Imported character
/// packs keep their own animation contract and are not painted over.
enum ConverseMouthPose { closed, halfOpen, open, rounded, pursed, teeth }

ConverseMouthPose converseMouthPose(double openness, String? viseme) {
  if (!openness.isFinite || openness < .08) return ConverseMouthPose.closed;
  switch (viseme) {
    case 'sil':
    case 'PP':
      return ConverseMouthPose.closed;
    case 'oh':
      return ConverseMouthPose.rounded;
    case 'ou':
    case 'RR':
      return ConverseMouthPose.pursed;
    case 'FF':
    case 'TH':
      return ConverseMouthPose.teeth;
    case 'DD':
    case 'kk':
    case 'CH':
    case 'SS':
    case 'nn':
    case 'E':
    case 'ih':
      return ConverseMouthPose.halfOpen;
    default:
      return openness < .6
          ? ConverseMouthPose.halfOpen
          : ConverseMouthPose.open;
  }
}

/// The atlas' neutral cell is 768×512. Drawing inside that same coordinate
/// system lets the portrait's single FittedBox crop/scale face and mouth
/// together, including in the narrow floating Converse panel.
class ConverseMouthPainter extends CustomPainter {
  const ConverseMouthPainter(this.pose);

  final ConverseMouthPose pose;
  static const canvasSize = Size(768, 512);
  static const mouthBounds = Rect.fromLTWH(450, 247, 37, 25);

  @override
  void paint(Canvas canvas, Size size) {
    if (pose == ConverseMouthPose.closed) return;
    canvas.save();
    canvas.scale(
      size.width / canvasSize.width,
      size.height / canvasSize.height,
    );

    // Hide only the neutral lips and lower-lip accent. This skin patch follows
    // the small, nearly white area inside the approved atlas' face outline.
    // The head, eyes, hair and body never switch sprites on a speech frame.
    final skin = Path()
      ..moveTo(451, 249)
      ..quadraticBezierTo(466, 246, 486, 249)
      ..quadraticBezierTo(487, 257, 480, 269)
      ..quadraticBezierTo(463, 272, 451, 260)
      ..close();
    canvas.drawPath(skin, Paint()..color = const Color(0xfffcfcfe));

    final cavity = Paint()..color = const Color(0xff062775);
    final outline = Paint()
      ..color = const Color(0xff3563bd)
      ..style = PaintingStyle.stroke
      ..strokeWidth = .8
      ..strokeJoin = StrokeJoin.round;

    if (pose == ConverseMouthPose.rounded || pose == ConverseMouthPose.pursed) {
      final round = pose == ConverseMouthPose.rounded;
      final mouth = Rect.fromCenter(
        center: const Offset(471, 258),
        width: round ? 16 : 10,
        height: round ? 18 : 13,
      );
      canvas.drawOval(mouth, cavity);
      canvas.drawOval(mouth, outline);
      canvas.drawArc(
        mouth.deflate(3),
        .2,
        1.7,
        false,
        Paint()
          ..color = const Color(0xff7a9be2)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2,
      );
    } else {
      final open = pose == ConverseMouthPose.open;
      final teeth = pose == ConverseMouthPose.teeth;
      final depth = open ? 18.0 : (teeth ? 6.0 : 9.0);
      final mouth = Path()
        ..moveTo(453, 252)
        ..cubicTo(461, 254, 474, 249, 484, 251)
        ..cubicTo(479, 252 + depth, 469, 254 + depth, 463, 254 + depth / 2)
        ..quadraticBezierTo(456, 257, 453, 252)
        ..close();
      canvas.drawPath(mouth, cavity);
      canvas.save();
      canvas.clipPath(mouth);
      final upperTeeth = Path()
        ..moveTo(454, 252)
        ..quadraticBezierTo(470, 251, 484, 251)
        ..lineTo(480, teeth ? 256 : 254)
        ..quadraticBezierTo(465, 256, 456, 254)
        ..close();
      canvas.drawPath(upperTeeth, Paint()..color = const Color(0xfff8fbff));
      if (!teeth) {
        canvas.drawOval(
          Rect.fromLTWH(464, 252 + depth * .7, 14, 5),
          Paint()..color = const Color(0xff7a9be2),
        );
      }
      canvas.restore();
      canvas.drawPath(mouth, outline);
    }
    canvas.restore();
  }

  @override
  bool shouldRepaint(ConverseMouthPainter oldDelegate) =>
      oldDelegate.pose != pose;
}
