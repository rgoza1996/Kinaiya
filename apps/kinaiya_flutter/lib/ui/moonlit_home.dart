import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'anchored_moonlit_scene.dart';
import 'design_tokens.dart';

/// Home presentation shares CharacterView's voice owner with optional Converse.
class MoonlitHome extends StatefulWidget {
  const MoonlitHome({
    super.key,
    required this.character,
    required this.status,
    required this.callLabel,
    this.callTooltip = 'Start Live listen (armed mic)',
    required this.onSetup,
    this.onCall,
    this.onAgent,
    this.agentStatus = 'Not connected',
    this.sessionName,
    this.showConverseView = true,
    this.caption,
    this.banner,
    this.useDesignScene = true,
    this.sleeping = false,
    this.ownerControl,
  });
  final Widget character;
  final String status, callLabel, agentStatus, callTooltip;
  final String? sessionName;
  final VoidCallback onSetup;
  final VoidCallback? onCall, onAgent;
  final bool showConverseView, useDesignScene, sleeping;
  final Widget? caption, banner, ownerControl;
  @override
  State<MoonlitHome> createState() => _MoonlitHomeState();
}

class _MoonlitHomeState extends State<MoonlitHome> {
  Timer? _clock;
  @override
  void initState() {
    super.initState();
    _clock = Timer.periodic(const Duration(minutes: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _clock?.cancel();
    super.dispose();
  }

  Widget _card(Widget child) => Container(
    padding: const EdgeInsets.all(16),
    decoration: BoxDecoration(
      color: Theme.of(context).colorScheme.surface,
      borderRadius: BorderRadius.circular(20),
      border: Border.all(color: Theme.of(context).colorScheme.outlineVariant),
    ),
    child: child,
  );

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, box) {
      final scale = MediaQuery.textScalerOf(context).scale(1).clamp(1.0, 2.0);
      final height = math.max(box.maxHeight, 640.0 * scale);
      final cardHeight =
          (176 +
              (widget.ownerControl == null ? 0 : 80) +
              (widget.caption == null || widget.sleeping ? 0 : 80) +
              (widget.showConverseView ? 0 : 64)) *
          scale;
      final surfaceY = height - cardHeight - 48;
      final size = math.min(
        math.min(box.maxWidth - 24, height * .60),
        math.min(580.0, (surfaceY - 80).clamp(96, double.infinity) / .95),
      );
      final now = DateTime.now();
      const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      const months = [
        'Jan',
        'Feb',
        'Mar',
        'Apr',
        'May',
        'Jun',
        'Jul',
        'Aug',
        'Sep',
        'Oct',
        'Nov',
        'Dec',
      ];
      final scene = SizedBox(
        height: height,
        width: box.maxWidth,
        child: Stack(
          children: [
            if (widget.sleeping && widget.useDesignScene)
              Positioned(
                top: 0,
                left: 0,
                right: 0,
                height: math.max(0, surfaceY - 16),
                child: Semantics(
                  label: 'Kinaiya sleeping',
                  child: Image.asset(
                    KinaiyaDesign.sleepingHome,
                    key: const ValueKey('home-sleeping-artwork'),
                    fit:
                        box.maxWidth / scale < 950 ||
                            surfaceY - 16 < box.maxWidth * .20
                        ? BoxFit.contain
                        : BoxFit.cover,
                    alignment: const Alignment(0, .35),
                    excludeFromSemantics: true,
                    errorBuilder: (_, _, _) => const Center(
                      child: Icon(Icons.bedtime_outlined, size: 64),
                    ),
                  ),
                ),
              )
            else
              Positioned(
                top: surfaceY - size * .95,
                left: (box.maxWidth - size) / 2,
                width: size,
                height: size,
                child: widget.character,
              ),
            if (box.maxWidth / scale >= 950)
              Positioned(
                top: 16,
                right: 16,
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    _card(
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}',
                            style: Theme.of(context).textTheme.headlineMedium,
                          ),
                          Text(
                            '${days[now.weekday - 1]}, ${months[now.month - 1]} ${now.day}',
                          ),
                          const SizedBox(height: 8),
                          const Text('Local device time'),
                        ],
                      ),
                    ),
                    const SizedBox(width: 12),
                    SizedBox(
                      width: 248,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          _card(
                            Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                const Text('CURRENT SESSION'),
                                const SizedBox(height: 8),
                                Text(
                                  widget.sessionName?.isNotEmpty == true
                                      ? widget.sessionName!
                                      : 'No Agent session selected',
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                ),
                              ],
                            ),
                          ),
                          const SizedBox(height: 12),
                          _card(
                            Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                const Text('AGENT STATUS'),
                                const SizedBox(height: 4),
                                TextButton.icon(
                                  onPressed: widget.onAgent,
                                  icon: const Icon(
                                    Icons.chat_bubble_outline,
                                    size: 18,
                                  ),
                                  label: Text(widget.agentStatus),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            Positioned(
              left: 16,
              right: 16,
              bottom: 16,
              child: Center(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 440),
                  child: _card(
                    Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          widget.sleeping
                              ? 'Kinaiya is sleeping'
                              : widget.status,
                          textAlign: TextAlign.center,
                          style: Theme.of(context).textTheme.titleMedium,
                        ),
                        if (widget.sleeping) const Text('Converse is offline'),
                        const SizedBox(height: 10),
                        if (!widget.sleeping)
                          ExcludeSemantics(
                            child: Icon(
                              Icons.graphic_eq_rounded,
                              size: 48,
                              color: Theme.of(context).colorScheme.primary,
                            ),
                          ),
                        if (!widget.sleeping && widget.caption != null)
                          widget.caption!,
                        const SizedBox(height: 10),
                        Wrap(
                          alignment: WrapAlignment.center,
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            if (!widget.sleeping)
                              Tooltip(
                                message: widget.callTooltip,
                                child: Semantics(
                                  key: const ValueKey('home-talk'),
                                  child: FilledButton.icon(
                                    key: const ValueKey('call-kinaiya'),
                                    onPressed: widget.onCall,
                                    icon: Icon(
                                      widget.callLabel == 'End conversation'
                                          ? Icons.call_end_rounded
                                          : Icons.mic_none,
                                    ),
                                    label: Text(widget.callLabel),
                                  ),
                                ),
                              ),
                            OutlinedButton(
                              key: const ValueKey('home-setup'),
                              onPressed: widget.onSetup,
                              child: const Text('Setup'),
                            ),
                          ],
                        ),
                        if (widget.ownerControl != null)
                          Padding(
                            padding: const EdgeInsets.only(top: 10),
                            child: widget.ownerControl!,
                          ),
                        if (!widget.showConverseView)
                          const Padding(
                            padding: EdgeInsets.only(top: 12),
                            child: Text(
                              'Converse view is hidden · Enable it in Setup',
                              textAlign: TextAlign.center,
                            ),
                          ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
            if (widget.banner != null)
              Positioned(top: 0, left: 0, right: 0, child: widget.banner!),
          ],
        ),
      );
      return SingleChildScrollView(
        key: const ValueKey('home-scene'),
        child: widget.useDesignScene && !widget.sleeping
            ? AnchoredMoonlitScene(
                artwork: box.maxWidth < height
                    ? KinaiyaDesign.moonlitPhone
                    : KinaiyaDesign.moonlitDesktop,
                surfaceY: surfaceY,
                child: scene,
              )
            : scene,
      );
    },
  );
}

/// Unmodified native Penpot expression atlas. Cropping happens in the renderer;
/// poses communicate observed listening/thinking/playback, not invented emotion.
class ConversePortrait extends StatelessWidget {
  const ConversePortrait({
    super.key,
    this.speaking = false,
    this.thinking = false,
  });
  final bool speaking, thinking;
  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, bounds) {
      return Semantics(
        label: speaking
            ? 'Kinaiya speaking'
            : thinking
            ? 'Kinaiya thinking'
            : 'Kinaiya',
        child: ClipRect(
          child: FittedBox(
            fit: BoxFit.cover,
            child: SizedBox(
              width: 768,
              height: 512,
              child: ClipRect(
                child: Stack(
                  children: [
                    Positioned(
                      left: thinking && !speaking ? -768 : 0,
                      top: speaking ? -512 : 0,
                      width: 1536,
                      height: 1024,
                      child: Image.asset(
                        'assets/design/kinaiya-expression-reference.png',
                        fit: BoxFit.fill,
                        excludeFromSemantics: true,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      );
    },
  );
}
