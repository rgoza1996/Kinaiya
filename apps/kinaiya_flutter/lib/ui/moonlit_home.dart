import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'anchored_moonlit_scene.dart';
import 'converse_mouth.dart';
import 'design_tokens.dart';
import 'home_sprite_sheet.dart';

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
    this.setupOpen = false,
    this.caption,
    this.banner,
    this.useDesignScene = true,
    this.sleeping = false,
    this.offlineResting = false,
    this.animateCharacter = true,
    this.ownerControl,
  });
  final Widget character;
  final String status, callLabel, agentStatus, callTooltip;
  final String? sessionName;
  final VoidCallback onSetup;
  final VoidCallback? onCall, onAgent;
  final bool showConverseView,
      useDesignScene,
      sleeping,
      offlineResting,
      animateCharacter,
      setupOpen;
  final Widget? caption, banner, ownerControl;
  @override
  State<MoonlitHome> createState() => _MoonlitHomeState();
}

class _MoonlitHomeState extends State<MoonlitHome>
    with SingleTickerProviderStateMixin {
  Timer? _clock;
  late final AnimationController _sleepTransition;
  final _sceneKey = GlobalKey();
  final _panelKey = GlobalKey();
  Offset _panelOffset = Offset.zero;
  bool _panelBoundsCheckPending = false;
  // The generated sheet's lower row is 129 source pixels higher than its
  // upper row. Register all frames to the same contact point on the water.
  static const _sleepFrameOffsets = [
    Offset.zero,
    Offset(7 / 768, 0),
    Offset(0, 129 / 512),
    Offset(7 / 768, 129 / 512),
  ];
  static const _transitionFrameOffsets = [
    Offset(-11.5 / 768, 21 / 512),
    Offset(-19.5 / 768, 16 / 512),
    Offset(1.5 / 768, 70 / 512),
  ];
  bool _resting(MoonlitHome home) => home.sleeping || home.offlineResting;

  void _movePanel(Offset delta) {
    final scene = _sceneKey.currentContext?.findRenderObject() as RenderBox?;
    final panel = _panelKey.currentContext?.findRenderObject() as RenderBox?;
    if (scene == null || panel == null || !scene.hasSize || !panel.hasSize) {
      return;
    }
    final origin = scene.globalToLocal(panel.localToGlobal(Offset.zero));
    double bounded(double change, double start, double length, double extent) {
      final lower = 8 - start;
      final upper = extent - 8 - start - length;
      return lower <= upper ? change.clamp(lower, upper) : (lower + upper) / 2;
    }

    final adjustment = Offset(
      bounded(delta.dx, origin.dx, panel.size.width, scene.size.width),
      bounded(delta.dy, origin.dy, panel.size.height, scene.size.height),
    );
    if (adjustment.distance > .01) {
      setState(() => _panelOffset += adjustment);
    }
  }

  void _schedulePanelBoundsCheck() {
    if (_panelBoundsCheckPending) return;
    _panelBoundsCheckPending = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _panelBoundsCheckPending = false;
      if (mounted && !widget.setupOpen) _movePanel(Offset.zero);
    });
  }

  @override
  void initState() {
    super.initState();
    _sleepTransition = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1600),
      value: _resting(widget) ? 1 : 0,
    );
    _sleepTransition.addStatusListener((status) {
      if (mounted &&
          (status == AnimationStatus.completed ||
              status == AnimationStatus.dismissed)) {
        setState(() {});
      }
    });
    _clock = Timer.periodic(const Duration(minutes: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void didUpdateWidget(covariant MoonlitHome oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (_resting(oldWidget) != _resting(widget)) {
      final target = _resting(widget) ? 1.0 : 0.0;
      if (!widget.animateCharacter ||
          MediaQuery.of(context).disableAnimations) {
        _sleepTransition.value = target;
      } else {
        _sleepTransition.animateTo(target);
      }
    }
    if (!widget.animateCharacter && _sleepTransition.isAnimating) {
      _sleepTransition.value = _resting(widget) ? 1 : 0;
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (MediaQuery.of(context).disableAnimations &&
        _sleepTransition.isAnimating) {
      _sleepTransition.value = _resting(widget) ? 1 : 0;
    }
  }

  @override
  void dispose() {
    _sleepTransition.dispose();
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

  Widget _homePanel() => Center(
    child: ConstrainedBox(
      key: const ValueKey('home-status-panel'),
      constraints: BoxConstraints(
        maxWidth: 440,
        minWidth: widget.sleeping ? 440 : 0,
      ),
      child: KeyedSubtree(
        key: _panelKey,
        child: GestureDetector(
          onPanUpdate: (details) => _movePanel(details.delta),
          child: _card(
            Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Tooltip(
                  message: 'Drag to move panel',
                  child: MouseRegion(
                    cursor: SystemMouseCursors.move,
                    child: Icon(Icons.drag_handle, size: 20),
                  ),
                ),
                Text(
                  widget.sleeping ? 'Kinaiya is sleeping' : widget.status,
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
                if (!widget.sleeping && widget.caption != null) widget.caption!,
                if (!widget.sleeping) const SizedBox(height: 10),
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
  );

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, box) {
      _schedulePanelBoundsCheck();
      final scale = MediaQuery.textScalerOf(context).scale(1).clamp(1.0, 2.0);
      final sleepingDesign =
          (widget.sleeping || widget.offlineResting) && widget.useDesignScene;
      final height = math.max(box.maxHeight, 640.0 * scale);
      // The water contact is independent of changing panel text and controls.
      final surfaceY = height - 256 * scale - 48;
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
        key: _sceneKey,
        height: height,
        width: box.maxWidth,
        child: Stack(
          children: [
            if (sleepingDesign ||
                (widget.useDesignScene && _sleepTransition.isAnimating))
              Positioned(
                top:
                    surfaceY - math.min(box.maxWidth - 24, 740.0) * 2 / 3 * .84,
                left: (box.maxWidth - math.min(box.maxWidth - 24, 740.0)) / 2,
                width: math.min(box.maxWidth - 24, 740.0),
                height: math.min(box.maxWidth - 24, 740.0) * 2 / 3,
                child: Semantics(
                  button: true,
                  label: 'Open Kinaiya setup',
                  child: InkWell(
                    onTap: widget.onSetup,
                    child: AnimatedBuilder(
                      animation: _sleepTransition,
                      builder: (context, _) {
                        final frame = (_sleepTransition.value * 4)
                            .floor()
                            .clamp(0, 3);
                        return _sleepTransition.isAnimating
                            ? HomeSpriteSheet(
                                key: const ValueKey('home-sleep-transition'),
                                asset: frame == 3
                                    ? 'assets/character/kinaiya-sleeping-idle-v3.png'
                                    : 'assets/character/kinaiya-sleep-transition-v3.png',
                                frame: frame == 3 ? 0 : frame,
                                frameOffsets: frame == 3
                                    ? _sleepFrameOffsets
                                    : _transitionFrameOffsets,
                              )
                            : HomeSpriteSheet(
                                key: const ValueKey('home-sleeping-artwork'),
                                asset:
                                    'assets/character/kinaiya-sleeping-idle-v3.png',
                                animate: widget.animateCharacter,
                                frameOffsets: _sleepFrameOffsets,
                              );
                      },
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
            if (!widget.setupOpen)
              Positioned(
                left: 16 + _panelOffset.dx,
                right: 16 - _panelOffset.dx,
                bottom: (sleepingDesign ? 8 : 16) - _panelOffset.dy,
                child: _homePanel(),
              ),
            if (widget.banner != null)
              Positioned(top: 0, left: 0, right: 0, child: widget.banner!),
          ],
        ),
      );
      return SingleChildScrollView(
        key: const ValueKey('home-scene'),
        child: widget.useDesignScene
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

/// The approved atlas supplies a stable face while playback cues animate only
/// the mouth. Thinking still selects the atlas' original thinking pose.
class ConversePortrait extends StatelessWidget {
  const ConversePortrait({
    super.key,
    this.speaking = false,
    this.thinking = false,
    this.mouthOpenness = 0,
    this.viseme,
  });
  final bool speaking, thinking;
  final double mouthOpenness;
  final String? viseme;
  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, bounds) {
      final mouth = speaking && !MediaQuery.disableAnimationsOf(context)
          ? converseMouthPose(mouthOpenness, viseme)
          : ConverseMouthPose.closed;
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
                      top: 0,
                      width: 1536,
                      height: 1024,
                      child: Image.asset(
                        'assets/design/kinaiya-expression-reference-v2.png',
                        fit: BoxFit.fill,
                        excludeFromSemantics: true,
                      ),
                    ),
                    Positioned.fill(
                      child: ExcludeSemantics(
                        child: ConverseMouthOverlay(pose: mouth),
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
