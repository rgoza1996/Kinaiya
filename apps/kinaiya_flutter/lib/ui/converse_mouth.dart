import 'dart:ui' as ui;

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

/// Resolves the bundled static sprite atlas through Flutter's image cache.
/// A listener owns its ImageInfo; the painter only borrows its decoded image.
/// No asset loading or decoding runs inside paint or a playback sample callback.
class ConverseMouthOverlay extends StatefulWidget {
  const ConverseMouthOverlay({super.key, required this.pose});

  final ConverseMouthPose pose;

  @override
  State<ConverseMouthOverlay> createState() => _ConverseMouthOverlayState();
}

class _ConverseMouthOverlayState extends State<ConverseMouthOverlay> {
  static const _asset = AssetImage(ConverseMouthPainter.asset);
  late final ImageStreamListener _listener;
  ImageStream? _stream;
  ImageInfo? _imageInfo;

  @override
  void initState() {
    super.initState();
    _listener = ImageStreamListener(_onImage, onError: _onError);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final stream = _asset.resolve(createLocalImageConfiguration(context));
    if (stream.key == _stream?.key) return;
    _stream?.removeListener(_listener);
    _stream = stream;
    _replaceImage(null);
    stream.addListener(_listener);
  }

  void _replaceImage(ImageInfo? info) {
    final previous = _imageInfo;
    _imageInfo = info;
    // A previous painter may still be retained until this frame finishes.
    // Release its image handle after the new widget/painter has been installed.
    if (previous != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) => previous.dispose());
    }
  }

  void _onImage(ImageInfo info, bool synchronousCall) {
    if (!mounted) {
      info.dispose();
      return;
    }
    if (info.image.width != ConverseMouthPainter.atlasWidth ||
        info.image.height != ConverseMouthPainter.atlasHeight) {
      info.dispose();
      setState(() => _replaceImage(null));
      return;
    }
    setState(() => _replaceImage(info));
  }

  void _onError(Object error, StackTrace? stackTrace) {
    if (mounted) setState(() => _replaceImage(null));
  }

  @override
  void dispose() {
    _stream?.removeListener(_listener);
    _stream = null;
    _replaceImage(null);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.pose == ConverseMouthPose.closed
      ? const SizedBox.expand()
      : CustomPaint(
          key: const ValueKey('converse-speech-mouth'),
          painter: ConverseMouthPainter(widget.pose, atlas: _imageInfo?.image),
        );
}

/// The neutral portrait is 768×512. Mouth source regions share one fixed scale
/// and upper-lip anchor, so changing poses never moves the head or hair.
class ConverseMouthPainter extends CustomPainter {
  const ConverseMouthPainter(this.pose, {this.atlas});

  final ConverseMouthPose pose;
  final ui.Image? atlas;
  static const asset = 'assets/design/kinaiya-speech-mouth-portraits-v1.png';
  static const atlasWidth = 1881, atlasHeight = 836;
  static const canvasSize = Size(768, 512);
  static const mouthBounds = Rect.fromLTWH(450, 247, 37, 25);
  static const _targetAnchor = Offset(469, 253);
  static const _spriteScale = 1.1;
  static const _sourceAnchors = <ConverseMouthPose, Offset>{
    ConverseMouthPose.halfOpen: Offset(1008, 205),
    ConverseMouthPose.open: Offset(1633, 202),
    ConverseMouthPose.rounded: Offset(386, 621),
    ConverseMouthPose.pursed: Offset(1016, 622),
    ConverseMouthPose.teeth: Offset(1633.5, 619),
  };
  // Tight lip rectangles exclude each generated face's jaw and lower accents.
  // They retain the same source scale instead of stretching to the mask bounds.
  static const _sourceLips = <ConverseMouthPose, Rect>{
    ConverseMouthPose.halfOpen: Rect.fromLTWH(993, 201, 30, 14),
    ConverseMouthPose.open: Rect.fromLTWH(1616, 199, 34, 17),
    ConverseMouthPose.rounded: Rect.fromLTWH(379, 617, 17, 16),
    ConverseMouthPose.pursed: Rect.fromLTWH(1010, 618, 12, 13),
    ConverseMouthPose.teeth: Rect.fromLTWH(1616, 615, 36, 11),
  };

  @override
  void paint(Canvas canvas, Size size) {
    final image = atlas;
    final anchor = _sourceAnchors[pose];
    final source = _sourceLips[pose];
    if (image == null ||
        anchor == null ||
        source == null ||
        size.isEmpty ||
        image.width != atlasWidth ||
        image.height != atlasHeight) {
      // Missing or unexpected artwork leaves the original closed mouth intact.
      return;
    }
    final destination = Rect.fromLTWH(
      _targetAnchor.dx + (source.left - anchor.dx) * _spriteScale,
      _targetAnchor.dy + (source.top - anchor.dy) * _spriteScale,
      source.width * _spriteScale,
      source.height * _spriteScale,
    );
    canvas.save();
    canvas.scale(
      size.width / canvasSize.width,
      size.height / canvasSize.height,
    );
    canvas.clipRect(mouthBounds);

    // This small path stays inside the approved neutral face. Only a registered
    // mouth region is sampled from the generated sheet; its jaw, eyes and hair
    // are never displayed. Clipping also excludes the source jaw at the edge.
    final skin = Path()
      ..moveTo(451, 249)
      ..quadraticBezierTo(466, 246, 486, 249)
      ..quadraticBezierTo(487, 257, 480, 269)
      ..quadraticBezierTo(463, 272, 451, 260)
      ..close();
    canvas.clipPath(skin);
    canvas.drawPath(skin, Paint()..color = const Color(0xfffcfcfe));
    canvas.drawImageRect(
      image,
      source,
      destination,
      Paint()..filterQuality = FilterQuality.high,
    );
    canvas.restore();
  }

  @override
  bool shouldRepaint(ConverseMouthPainter oldDelegate) =>
      oldDelegate.pose != pose || !identical(oldDelegate.atlas, atlas);
}
