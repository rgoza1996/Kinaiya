import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:flutter/services.dart' show AssetBundle, rootBundle;

import '../character/pack.dart';

/// Local decorative frame playback. It never gates speech or runtime work.
class MascotWidget extends StatefulWidget {
  const MascotWidget({
    super.key,
    required this.pack,
    required this.mood,
    this.size = 220,
    this.bundle,
  });
  final CharacterPack pack;
  final String mood;
  final double size;
  final AssetBundle? bundle;
  @override
  State<MascotWidget> createState() => _MascotWidgetState();
}

class _MascotWidgetState extends State<MascotWidget>
    with SingleTickerProviderStateMixin {
  late final Ticker _ticker;
  Duration _latest = Duration.zero, _animStart = Duration.zero;
  late String _animId;
  int _framePos = 0, _loadGen = 0;
  ui.Image? _image;
  String? _imageSheetId;
  bool _decodeFailed = false;

  @override
  void initState() {
    super.initState();
    _animId = widget.pack.resolveAnimationId(widget.mood);
    _ticker = createTicker(_onTick);
    _loadCurrentSheet();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _syncTicker();
  }

  @override
  void didUpdateWidget(covariant MascotWidget oldWidget) {
    super.didUpdateWidget(oldWidget);
    final replaced =
        !identical(oldWidget.pack, widget.pack) ||
        oldWidget.bundle != widget.bundle;
    final next = widget.pack.resolveAnimationId(widget.mood);
    final previousSheet = widget.pack.animations[_animId]?.sheet;
    final nextSheet = widget.pack.animations[next]?.sheet;
    if (replaced || next != _animId) {
      _framePos = 0;
      _animStart = _latest;
      _animId = next;
    }
    if (replaced) {
      _loadGen++;
      _image?.dispose();
      _image = null;
      _imageSheetId = null;
      _decodeFailed = false;
      _loadCurrentSheet();
    } else if (previousSheet != nextSheet) {
      _loadGen++;
      _decodeFailed = false;
      if (_imageSheetId != nextSheet) _loadCurrentSheet();
    }
    _syncTicker();
  }

  void _syncTicker() {
    final reduced = MediaQuery.of(context).disableAnimations;
    if (reduced || _decodeFailed) {
      _ticker.stop();
      _latest = _animStart = Duration.zero;
      _framePos = 0;
    } else if (!_ticker.isActive) {
      // Ticker.start resets elapsed time. Its animation origin must match.
      _latest = _animStart = Duration.zero;
      _ticker.start();
    }
  }

  void _onTick(Duration elapsed) {
    _latest = elapsed;
    if (!widget.pack.isSpritesheet) {
      setState(() {});
      return;
    }
    final anim = widget.pack.animations[_animId];
    if (anim == null) return;
    final pos = CharacterPack.frameIndexFor(anim, elapsed - _animStart);
    if (pos != _framePos) setState(() => _framePos = pos);
  }

  Future<void> _loadCurrentSheet() async {
    if (!widget.pack.isSpritesheet) return;
    final sheet = widget.pack.sheetForAnimation(_animId);
    if (sheet == null) return;
    final gen = ++_loadGen;
    try {
      final data = await (widget.bundle ?? rootBundle).load(sheet.asset);
      if (!mounted || gen != _loadGen) return;
      final bytes = data.buffer.asUint8List(
        data.offsetInBytes,
        data.lengthInBytes,
      );
      final dims = pngDimensions(bytes);
      if (bytes.lengthInBytes > (8 << 20) ||
          dims == null ||
          dims[0] != sheet.width ||
          dims[1] != sheet.height ||
          sheet.width > 4096 ||
          sheet.height > 4096 ||
          sheet.width * sheet.height > 16 * 1024 * 1024 ||
          sha256.convert(bytes).toString() != sheet.sha256.toLowerCase()) {
        throw const FormatException('Invalid sprite texture');
      }
      final codec = await ui.instantiateImageCodec(
        bytes,
        allowUpscaling: false,
      );
      late final ui.FrameInfo frame;
      try {
        if (!mounted || gen != _loadGen) return;
        if (codec.frameCount != 1) {
          throw const FormatException('Static PNG required');
        }
        frame = await codec.getNextFrame();
      } finally {
        codec.dispose();
      }
      if (!mounted ||
          gen != _loadGen ||
          widget.pack.sheetForAnimation(_animId)?.id != sheet.id ||
          frame.image.width != sheet.width ||
          frame.image.height != sheet.height) {
        frame.image.dispose();
        return;
      }
      _image?.dispose();
      _image = frame.image;
      _imageSheetId = sheet.id;
      setState(() {});
    } catch (_) {
      if (!mounted || gen != _loadGen) return;
      setState(() => _decodeFailed = true);
      _syncTicker();
    }
  }

  @override
  void dispose() {
    _loadGen++;
    _ticker.dispose();
    _image?.dispose();
    super.dispose();
  }

  Widget _fallback(String label) => Image.asset(
    widget.pack.fallbackAsset,
    width: widget.size,
    height: widget.size,
    semanticLabel: label,
    errorBuilder: (_, _, _) => Icon(Icons.face, size: widget.size),
  );

  @override
  Widget build(BuildContext context) {
    final reduced = MediaQuery.of(context).disableAnimations;
    final label = 'Kinaiya mascot, ${widget.mood}';
    if (!widget.pack.isSpritesheet) {
      final image = _fallback(label);
      if (reduced) return image;
      final phase = CharacterPack.motionPhase(widget.mood);
      final t =
          _latest.inMilliseconds / 3000 * 2 * math.pi + phase * math.pi / 180;
      return Transform.scale(
        scale: 1 + (widget.mood == 'speaking' ? .05 : .02) * math.sin(t),
        child: Transform.rotate(angle: .02 * math.sin(t * .7), child: image),
      );
    }
    final anim = widget.pack.animations[_animId];
    final sheet = widget.pack.sheetForAnimation(_animId);
    if (_decodeFailed) {
      return Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          _fallback(label),
          const Text('Animation unavailable; showing original mascot.'),
        ],
      );
    }
    if (anim == null ||
        sheet == null ||
        _image == null ||
        _imageSheetId != sheet.id) {
      return _fallback(label);
    }
    final pos = reduced ? 0 : _framePos.clamp(0, anim.frames.length - 1);
    return Semantics(
      label: label,
      image: true,
      child: CustomPaint(
        size: Size(widget.size, widget.size),
        painter: MascotFramePainter(
          image: _image!,
          src: sheet.frameRect(anim.frames[pos]),
          anchor: Offset(widget.pack.anchorX, widget.pack.anchorY),
        ),
      ),
    );
  }
}

/// Exposes the actual drawn source rectangle for deterministic renderer checks.
class MascotFramePainter extends CustomPainter {
  MascotFramePainter({
    required this.image,
    required this.src,
    this.anchor = const Offset(.5, .9),
  });
  final ui.Image image;
  final ui.Rect src;
  final Offset anchor;
  @override
  void paint(ui.Canvas canvas, ui.Size size) {
    final scale = math.min(size.width / src.width, size.height / src.height);
    final width = src.width * scale, height = src.height * scale;
    final dst = ui.Rect.fromLTWH(
      (size.width - width) * anchor.dx,
      (size.height - height) * anchor.dy,
      width,
      height,
    );
    canvas.drawImageRect(
      image,
      src,
      dst,
      ui.Paint()..filterQuality = FilterQuality.medium,
    );
  }

  @override
  bool shouldRepaint(covariant MascotFramePainter old) =>
      !identical(old.image, image) || old.src != src || old.anchor != anchor;
}

/// Compact mascot send button with a clear accessible label.
class SendMascotButton extends StatelessWidget {
  const SendMascotButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.enabled = true,
  });

  final String label;
  final VoidCallback? onPressed;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: label,
      child: Semantics(
        button: true,
        label: label,
        enabled: enabled,
        child: InkWell(
          onTap: enabled ? onPressed : null,
          borderRadius: BorderRadius.circular(24),
          child: Opacity(
            opacity: enabled ? 1 : 0.4,
            child: ConstrainedBox(
              constraints: const BoxConstraints(minWidth: 48, minHeight: 48),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Image.asset(
                    'assets/mascot/kinaiya-mascot.png',
                    width: 36,
                    height: 36,
                    semanticLabel: 'Kinaiya mascot',
                    errorBuilder: (_, _, _) => const Icon(Icons.send),
                  ),
                  const SizedBox(width: 6),
                  Flexible(child: Text(label)),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class StatusChip extends StatelessWidget {
  const StatusChip({super.key, required this.text, this.tone = Colors.grey});
  final String text;
  final Color tone;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: 'Status: $text',
      child: Chip(
        label: Text(text),
        backgroundColor: tone.withValues(alpha: 0.15),
        side: BorderSide(color: tone),
      ),
    );
  }
}

class MessageBubble extends StatelessWidget {
  const MessageBubble({
    super.key,
    required this.role,
    required this.text,
    this.note,
    this.speaker = 'Kinaiya',
  });

  final String role;
  final String text;
  final String? note;
  final String speaker;

  @override
  Widget build(BuildContext context) {
    final mine = role == 'user';
    final colors = Theme.of(context).colorScheme;
    return Align(
      alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
      child: Semantics(
        label: mine ? 'You said: $text' : '$speaker said: $text',
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 8),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: mine ? const Color(0xFF141091) : colors.surfaceContainerHigh,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: colors.outlineVariant),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                text,
                style: TextStyle(
                  color: mine ? Colors.white : colors.onSurface,
                  height: 1.5,
                ),
              ),
              if (note != null && note!.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    note!,
                    style: TextStyle(
                      fontSize: 12,
                      color: mine ? Colors.white70 : colors.onSurfaceVariant,
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
