import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:flutter/services.dart' show AssetBundle, rootBundle;

import '../character/pack.dart';

/// Mascot with real spritesheet body/mouth frames when a valid v2 pack is
/// present, otherwise the original single-frame artwork with deterministic
/// state-based motion. Decorative only: it never delays speech, network or
/// runtime, it freezes under reduced motion, and its ticker respects
/// [TickerMode] and disposal.
class MascotWidget extends StatefulWidget {
  const MascotWidget({
    super.key,
    required this.pack,
    required this.mood,
    this.size = 160,
    this.bundle,
  });

  final CharacterPack pack;
  final String
  mood; // idle | listening | speaking | thinking (+presentation map)
  final double size;
  final AssetBundle? bundle;

  @override
  State<MascotWidget> createState() => _MascotWidgetState();
}

class _MascotWidgetState extends State<MascotWidget>
    with SingleTickerProviderStateMixin {
  late final Ticker _ticker;
  Duration _latest = Duration.zero;
  Duration _animStart = Duration.zero;
  late String _animId;
  int _framePos = 0;
  ui.Image? _image;
  String? _imageSheetId;
  int _loadGen = 0;

  bool get _isSprite => widget.pack.isSpritesheet;

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
    if (!identical(oldWidget.pack, widget.pack) ||
        oldWidget.mood != widget.mood ||
        oldWidget.bundle != widget.bundle) {
      final nextId = widget.pack.resolveAnimationId(widget.mood);
      final sheetChanged =
          _imageSheetId != _sheetIdFor(nextId) ||
          !identical(oldWidget.pack, widget.pack);
      if (!identical(oldWidget.pack, widget.pack)) {
        // New pack owns its decode: drop the old image and invalidate
        // any in-flight load so stale completions cannot win.
        _loadGen++;
        _image?.dispose();
        _image = null;
        _imageSheetId = null;
        _framePos = 0;
        _animStart = _latest;
      } else if (_animId != nextId) {
        _framePos = 0;
        _animStart = _latest;
      }
      _animId = nextId;
      if (sheetChanged || _image == null) {
        if (_isSprite) _loadCurrentSheet();
      }
      _syncTicker();
    }
  }

  String? _sheetIdFor(String animId) {
    final anim = widget.pack.animations[animId];
    return anim?.sheet;
  }

  void _syncTicker() {
    final reduced = MediaQuery.of(context).disableAnimations;
    if (reduced || !_isSprite) {
      if (_ticker.isActive) _ticker.stop();
      if (reduced && _framePos != 0) _framePos = 0;
    } else {
      if (!_ticker.isActive) _ticker.start();
    }
    // Fallback single-frame motion also needs continuous ticks unless
    // reduced motion freezes it.
    if (!_isSprite && !reduced && !_ticker.isActive) _ticker.start();
  }

  void _onTick(Duration elapsed) {
    _latest = elapsed;
    if (!_isSprite) {
      // Drive the subtle v1 transform motion.
      setState(() {});
      return;
    }
    final anim = widget.pack.animations[_animId];
    if (anim == null) return;
    final pos = CharacterPack.frameIndexFor(anim, elapsed - _animStart);
    if (pos != _framePos) {
      setState(() => _framePos = pos);
    }
  }

  Future<void> _loadCurrentSheet() async {
    if (!_isSprite) return;
    final animId = _animId;
    final anim = widget.pack.animations[animId];
    final sheet = anim == null ? null : widget.pack.sheets[anim.sheet];
    if (anim == null || sheet == null) return;
    final gen = ++_loadGen;
    final targetSheetId = sheet.id;
    try {
      final effective = widget.bundle ?? rootBundle;
      final data = await effective.load(sheet.asset);
      if (gen != _loadGen || !mounted) return;
      final bytes = data.buffer.asUint8List();
      // Bounded, untrusted input: size cap before any decode.
      if (bytes.lengthInBytes > (8 << 20)) return;
      final digest = sha256.convert(bytes).toString();
      if (digest != sheet.sha256.toLowerCase()) return;
      // Rectangle bounds from explicit manifest geometry BEFORE decoding.
      final dims = pngDimensions(bytes);
      if (dims == null || dims[0] != sheet.width || dims[1] != sheet.height) {
        return;
      }
      final codec = await ui.instantiateImageCodec(
        bytes,
        allowUpscaling: false,
      );
      if (gen != _loadGen || !mounted) {
        codec.dispose();
        return;
      }
      final frame = await codec.getNextFrame();
      codec.dispose();
      if (gen != _loadGen || !mounted) {
        frame.image.dispose();
        return;
      }
      // Actual decoded dimensions must match the manifest.
      if (frame.image.width != sheet.width ||
          frame.image.height != sheet.height) {
        frame.image.dispose();
        return;
      }
      // Stale ownership: a newer pack/sheet load wins.
      if (gen != _loadGen || !mounted) {
        frame.image.dispose();
        return;
      }
      final stillCurrent =
          _animId == animId &&
          widget.pack.isSpritesheet &&
          _sheetIdFor(_animId) == targetSheetId;
      if (!stillCurrent) {
        frame.image.dispose();
        // The animation moved on but the pack is the same: keep the
        // decoded sheet if it is still the needed one.
        if (widget.pack.sheets[targetSheetId]?.asset == sheet.asset) {
          _image?.dispose();
          _image = frame.image;
          _imageSheetId = targetSheetId;
          if (mounted) setState(() {});
        } else {
          frame.image.dispose();
        }
        return;
      }
      _image?.dispose();
      _image = frame.image;
      _imageSheetId = targetSheetId;
      if (mounted) setState(() {});
    } catch (_) {
      // Untrusted image failure stays on the shipped frame; never throws
      // into the build and never gates speech/runtime.
    }
  }

  @override
  void dispose() {
    _loadGen++;
    _ticker.dispose();
    _image?.dispose();
    _image = null;
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final reduced = MediaQuery.of(context).disableAnimations;
    if (!_isSprite) {
      final phase = CharacterPack.motionPhase(widget.mood);
      final image = Image.asset(
        widget.pack.fallbackAsset,
        width: widget.size,
        height: widget.size,
        semanticLabel: 'Kinaiya mascot, ${widget.mood}',
        errorBuilder: (_, _, _) => Icon(Icons.face, size: widget.size),
      );
      if (reduced) return image;
      // Subtle transform motion driven by the Ticker (respects TickerMode:
      // a muted ticker stops calling _onTick so _latest freezes).
      final t =
          _latest.inMilliseconds / 3000 * 2 * math.pi + phase * math.pi / 180;
      final amp = widget.mood == 'speaking' ? 0.05 : 0.02;
      return Transform.scale(
        scale: 1 + amp * math.sin(t),
        child: Transform.rotate(angle: 0.02 * math.sin(t * 0.7), child: image),
      );
    }
    final anim = widget.pack.animations[_animId];
    final sheet = anim == null ? null : widget.pack.sheets[anim.sheet];
    if (anim == null || sheet == null) {
      return Image.asset(
        widget.pack.fallbackAsset,
        width: widget.size,
        height: widget.size,
        semanticLabel: 'Kinaiya mascot, ${widget.mood}',
        errorBuilder: (_, _, _) => Icon(Icons.face, size: widget.size),
      );
    }
    final pos = reduced ? 0 : _framePos.clamp(0, anim.frames.length - 1);
    final frameId = anim.frames[pos];
    final label =
        'Kinaiya mascot, ${widget.mood} ($_animId) frame $pos of ${anim.frames.length}';
    if (_image == null) {
      // Never gate on the decode: show the shipped frame until the sheet
      // image is ready, then swap to real bitmap frames.
      return Image.asset(
        widget.pack.fallbackAsset,
        width: widget.size,
        height: widget.size,
        semanticLabel: label,
        errorBuilder: (_, _, _) => Icon(Icons.face, size: widget.size),
      );
    }
    final src = sheet.frameRect(frameId);
    return Semantics(
      label: label,
      image: true,
      child: CustomPaint(
        size: Size(widget.size, widget.size),
        painter: _SpritePainter(image: _image!, src: src),
      ),
    );
  }
}

class _SpritePainter extends CustomPainter {
  _SpritePainter({required this.image, required this.src});
  final ui.Image image;
  final ui.Rect src;

  @override
  void paint(ui.Canvas canvas, ui.Size size) {
    final dst = ui.Rect.fromLTWH(0, 0, size.width, size.height);
    canvas.drawImageRect(image, src, dst, ui.Paint());
  }

  @override
  bool shouldRepaint(covariant _SpritePainter old) =>
      !identical(old.image, image) || old.src != src;
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
                Text(label),
              ],
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
  });

  final String role;
  final String text;
  final String? note;

  @override
  Widget build(BuildContext context) {
    final mine = role == 'user';
    return Align(
      alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
      child: Semantics(
        label: mine ? 'You said: $text' : 'Kinaiya said: $text',
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 8),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: mine ? const Color(0xFF141091) : Colors.grey.shade100,
            borderRadius: BorderRadius.circular(12),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                text,
                style: TextStyle(color: mine ? Colors.white : Colors.black87),
              ),
              if (note != null && note!.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    note!,
                    style: TextStyle(
                      fontSize: 12,
                      color: mine ? Colors.white70 : Colors.black54,
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
