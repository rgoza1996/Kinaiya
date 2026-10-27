import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../character/pack.dart';

/// Mascot with deterministic state-based motion. Decorative only: it never
/// delays speech, and it freezes under reduced motion.
class MascotWidget extends StatefulWidget {
  const MascotWidget({
    super.key,
    required this.pack,
    required this.mood,
    this.size = 160,
  });

  final CharacterPack pack;
  final String mood; // idle | listening | speaking | thinking
  final double size;

  @override
  State<MascotWidget> createState() => _MascotWidgetState();
}

class _MascotWidgetState extends State<MascotWidget>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 3),
    )..repeat();
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final reduced = MediaQuery.of(context).disableAnimations;
    final phase = CharacterPack.motionPhase(widget.mood);
    final image = Image.asset(
      widget.pack.fallbackAsset,
      width: widget.size,
      height: widget.size,
      semanticLabel: 'Kinaiya mascot, ${widget.mood}',
      errorBuilder: (_, _, _) => Icon(Icons.face, size: widget.size),
    );
    if (reduced) return image;
    return AnimatedBuilder(
      animation: _ctrl,
      builder: (_, child) {
        final t = _ctrl.value * 2 * math.pi + phase * math.pi / 180;
        final amp = widget.mood == 'speaking' ? 0.05 : 0.02;
        return Transform.scale(
          scale: 1 + amp * math.sin(t),
          child: Transform.rotate(
            angle: 0.02 * math.sin(t * 0.7),
            child: child,
          ),
        );
      },
      child: image,
    );
  }
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
