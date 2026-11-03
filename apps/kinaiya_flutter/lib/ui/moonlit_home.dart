import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

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
    this.setupLabel = 'Setup',
    this.helperText,
    required this.onSetup,
    this.onConverseSetup,
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
    this.sleepingCharacterWrapper,
  });
  final Widget character;
  final String status, callLabel, agentStatus, callTooltip, setupLabel;
  final String? sessionName, helperText;
  final VoidCallback onSetup;
  final VoidCallback? onConverseSetup, onCall, onAgent;
  final bool showConverseView,
      useDesignScene,
      sleeping,
      offlineResting,
      animateCharacter,
      setupOpen;
  final Widget? caption, banner, ownerControl;
  final Widget Function(Widget child)? sleepingCharacterWrapper;
  @override
  State<MoonlitHome> createState() => _MoonlitHomeState();
}

class _MoonlitHomeState extends State<MoonlitHome>
    with SingleTickerProviderStateMixin {
  Timer? _clock;
  late final AnimationController _sleepTransition;
  final _sceneKey = GlobalKey();
  final _panelKey = GlobalKey();
  final _resizeFocus = FocusNode(
    debugLabel: 'Resize connection panel',
    skipTraversal: true,
  );
  Offset _panelOffset = Offset.zero;
  double? _panelWidth;
  double? _panelHeight;
  bool _resizeFromLeft = false;
  bool _resizeFromBottom = false;
  bool _panelBoundsCheckPending = false;
  static const double _minPanelWidth = 320.0;
  static const double _minPanelHeight = 120.0;
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

  void _focusResize({bool? fromLeft, bool? fromBottom}) {
    if (fromLeft != null) _resizeFromLeft = fromLeft;
    if (fromBottom != null) _resizeFromBottom = fromBottom;
    _resizeFocus.requestFocus();
  }

  void _resizePanelEdges({
    double? left,
    double? right,
    double? top,
    double? bottom,
  }) {
    final scene = _sceneKey.currentContext?.findRenderObject() as RenderBox?;
    final panel = _panelKey.currentContext?.findRenderObject() as RenderBox?;
    if (scene == null || panel == null || !scene.hasSize || !panel.hasSize) {
      return;
    }
    final maxSceneWidth = math.max(0.0, scene.size.width - 32);
    final minWidth = math.min(_minPanelWidth, maxSceneWidth);
    final currentWidth = (_panelWidth ?? panel.size.width).clamp(
      minWidth,
      maxSceneWidth,
    );
    final currentCenterX = scene.size.width / 2 + _panelOffset.dx;
    final currentLeft = currentCenterX - currentWidth / 2;
    final currentRight = currentCenterX + currentWidth / 2;

    var newWidth = currentWidth;
    var dxOffset = 0.0;
    if (left != null) {
      final maxLeftWidth = math.max(
        minWidth,
        math.min(maxSceneWidth, currentRight - 8),
      );
      newWidth = (currentWidth - left).clamp(minWidth, maxLeftWidth);
      dxOffset = -(newWidth - currentWidth) / 2;
    } else if (right != null) {
      final maxRightWidth = math.max(
        minWidth,
        math.min(maxSceneWidth, scene.size.width - 8 - currentLeft),
      );
      newWidth = (currentWidth + right).clamp(minWidth, maxRightWidth);
      dxOffset = (newWidth - currentWidth) / 2;
    }

    final maxSceneHeight = math.max(0.0, scene.size.height - 16);
    final minHeight = math.min(_minPanelHeight, maxSceneHeight);
    final currentHeight = (_panelHeight ?? panel.size.height).clamp(
      minHeight,
      maxSceneHeight,
    );
    final sleepingDesign =
        (widget.sleeping || widget.offlineResting) && widget.useDesignScene;
    final baseBottom = sleepingDesign ? 8.0 : 16.0;
    final currentBottom = scene.size.height - baseBottom + _panelOffset.dy;
    final currentTop = currentBottom - currentHeight;

    var newHeight = currentHeight;
    var dyOffset = 0.0;
    if (top != null) {
      final maxTopHeight = math.max(
        minHeight,
        math.min(maxSceneHeight, currentBottom - 8),
      );
      newHeight = (currentHeight - top).clamp(minHeight, maxTopHeight);
    } else if (bottom != null) {
      final maxBottomHeight = math.max(
        minHeight,
        math.min(maxSceneHeight, scene.size.height - 8 - currentTop),
      );
      newHeight = (currentHeight + bottom).clamp(minHeight, maxBottomHeight);
      dyOffset = newHeight - currentHeight;
    }

    final widthChanged =
        (left != null || right != null) &&
        ((newWidth - currentWidth).abs() > .01 || _panelWidth == null);
    final heightChanged =
        (top != null || bottom != null) &&
        ((newHeight - currentHeight).abs() > .01 || _panelHeight == null);

    if (widthChanged || heightChanged) {
      setState(() {
        if (left != null || right != null) _panelWidth = newWidth;
        if (top != null || bottom != null) _panelHeight = newHeight;
        _panelOffset += Offset(dxOffset, dyOffset);
      });
      _schedulePanelBoundsCheck();
    }
  }

  List<Widget> _windowResizeHandles() => [
    Positioned(
      left: 0,
      top: 14,
      bottom: 14,
      width: 14,
      child: Semantics(
        label: 'Resize panel left edge',
        onIncrease: () => _resizePanelEdges(left: -24),
        onDecrease: () => _resizePanelEdges(left: 24),
        child: MouseRegion(
          cursor: SystemMouseCursors.resizeLeftRight,
          child: GestureDetector(
            key: const ValueKey('home-panel-resize-left'),
            behavior: HitTestBehavior.opaque,
            onTap: () => _focusResize(fromLeft: true),
            onHorizontalDragStart: (_) => _focusResize(fromLeft: true),
            onHorizontalDragUpdate: (details) =>
                _resizePanelEdges(left: details.delta.dx),
            child: const SizedBox.expand(),
          ),
        ),
      ),
    ),
    Positioned(
      right: 0,
      top: 14,
      bottom: 14,
      width: 14,
      child: Semantics(
        label: 'Resize panel right edge',
        onIncrease: () => _resizePanelEdges(right: 24),
        onDecrease: () => _resizePanelEdges(right: -24),
        child: MouseRegion(
          cursor: SystemMouseCursors.resizeLeftRight,
          child: GestureDetector(
            key: const ValueKey('home-panel-resize-right'),
            behavior: HitTestBehavior.opaque,
            onTap: () => _focusResize(fromLeft: false),
            onHorizontalDragStart: (_) => _focusResize(fromLeft: false),
            onHorizontalDragUpdate: (details) =>
                _resizePanelEdges(right: details.delta.dx),
            child: const SizedBox.expand(),
          ),
        ),
      ),
    ),
    Positioned(
      top: 0,
      left: 14,
      right: 14,
      height: 12,
      child: Semantics(
        label: 'Resize panel top edge',
        onIncrease: () => _resizePanelEdges(top: -24),
        onDecrease: () => _resizePanelEdges(top: 24),
        child: MouseRegion(
          cursor: SystemMouseCursors.resizeUpDown,
          child: GestureDetector(
            key: const ValueKey('home-panel-resize-top'),
            behavior: HitTestBehavior.opaque,
            onTap: () => _focusResize(fromBottom: false),
            onVerticalDragStart: (_) => _focusResize(fromBottom: false),
            onVerticalDragUpdate: (details) =>
                _resizePanelEdges(top: details.delta.dy),
            child: const SizedBox.expand(),
          ),
        ),
      ),
    ),
    Positioned(
      bottom: 0,
      left: 14,
      right: 14,
      height: 12,
      child: Semantics(
        label: 'Resize panel bottom edge',
        onIncrease: () => _resizePanelEdges(bottom: 24),
        onDecrease: () => _resizePanelEdges(bottom: -24),
        child: MouseRegion(
          cursor: SystemMouseCursors.resizeUpDown,
          child: GestureDetector(
            key: const ValueKey('home-panel-resize-bottom'),
            behavior: HitTestBehavior.opaque,
            onTap: () => _focusResize(fromBottom: true),
            onVerticalDragStart: (_) => _focusResize(fromBottom: true),
            onVerticalDragUpdate: (details) =>
                _resizePanelEdges(bottom: details.delta.dy),
            child: const SizedBox.expand(),
          ),
        ),
      ),
    ),
    Positioned(
      top: 0,
      left: 0,
      width: 14,
      height: 14,
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeUpLeftDownRight,
        child: GestureDetector(
          key: const ValueKey('home-panel-resize-top-left'),
          behavior: HitTestBehavior.opaque,
          onTap: () => _focusResize(fromLeft: true, fromBottom: false),
          onPanStart: (_) => _focusResize(fromLeft: true, fromBottom: false),
          onPanUpdate: (details) =>
              _resizePanelEdges(left: details.delta.dx, top: details.delta.dy),
          child: const SizedBox.expand(),
        ),
      ),
    ),
    Positioned(
      top: 0,
      right: 0,
      width: 14,
      height: 14,
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeUpRightDownLeft,
        child: GestureDetector(
          key: const ValueKey('home-panel-resize-top-right'),
          behavior: HitTestBehavior.opaque,
          onTap: () => _focusResize(fromLeft: false, fromBottom: false),
          onPanStart: (_) => _focusResize(fromLeft: false, fromBottom: false),
          onPanUpdate: (details) =>
              _resizePanelEdges(right: details.delta.dx, top: details.delta.dy),
          child: const SizedBox.expand(),
        ),
      ),
    ),
    Positioned(
      bottom: 0,
      left: 0,
      width: 14,
      height: 14,
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeUpRightDownLeft,
        child: GestureDetector(
          key: const ValueKey('home-panel-resize-bottom-left'),
          behavior: HitTestBehavior.opaque,
          onTap: () => _focusResize(fromLeft: true, fromBottom: true),
          onPanStart: (_) => _focusResize(fromLeft: true, fromBottom: true),
          onPanUpdate: (details) => _resizePanelEdges(
            left: details.delta.dx,
            bottom: details.delta.dy,
          ),
          child: const SizedBox.expand(),
        ),
      ),
    ),
    Positioned(
      bottom: 0,
      right: 0,
      width: 14,
      height: 14,
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeUpLeftDownRight,
        child: GestureDetector(
          key: const ValueKey('home-panel-resize-bottom-right'),
          behavior: HitTestBehavior.opaque,
          onTap: () => _focusResize(fromLeft: false, fromBottom: true),
          onPanStart: (_) => _focusResize(fromLeft: false, fromBottom: true),
          onPanUpdate: (details) => _resizePanelEdges(
            right: details.delta.dx,
            bottom: details.delta.dy,
          ),
          child: const SizedBox.expand(),
        ),
      ),
    ),
  ];

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
    _resizeFocus.dispose();
    _sleepTransition.dispose();
    _clock?.cancel();
    super.dispose();
  }

  Widget _card(
    Widget child, {
    Key? key,
    bool prominent = false,
    EdgeInsetsGeometry padding = const EdgeInsets.all(16),
  }) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      key: key,
      padding: padding,
      decoration: BoxDecoration(
        color: prominent ? scheme.surfaceContainer : scheme.surfaceContainerLow,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(
          color: prominent ? scheme.outline : scheme.outlineVariant,
          width: prominent ? 1.5 : 1.0,
        ),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: prominent ? 0.38 : 0.24),
            blurRadius: prominent ? 24 : 16,
            offset: Offset(0, prominent ? 10 : 6),
          ),
        ],
      ),
      child: child,
    );
  }

  Widget _homePanel(double maxSceneWidth, double maxSceneHeight) {
    final maxWidth = math.max(0.0, maxSceneWidth - 32);
    final minWidth = math.min(_minPanelWidth, maxWidth);
    final effectiveWidth = _panelWidth?.clamp(minWidth, maxWidth);
    final maxHeight = math.max(0.0, maxSceneHeight - 16);
    final minHeight = math.min(_minPanelHeight, maxHeight);
    final effectiveHeight = _panelHeight?.clamp(minHeight, maxHeight);
    final content = Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Tooltip(
          message: 'Drag to move panel',
          child: MouseRegion(
            cursor: SystemMouseCursors.move,
            child: Icon(
              Icons.drag_handle,
              size: 20,
              color: Theme.of(context).colorScheme.onSurfaceVariant,
            ),
          ),
        ),
        const SizedBox(height: 2),
        Semantics(
          container: true,
          liveRegion: false,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                widget.sleeping ? 'Kinaiya is sleeping' : widget.status,
                textAlign: TextAlign.center,
                style: Theme.of(
                  context,
                ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
              ),
              if (widget.sleeping) const Text('Converse is offline'),
              if (widget.helperText != null &&
                  widget.helperText!.trim().isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(
                    widget.helperText!,
                    key: const ValueKey('home-helper-copy'),
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: Theme.of(context).colorScheme.onSurfaceVariant,
                      height: 1.35,
                    ),
                  ),
                ),
            ],
          ),
        ),
        const SizedBox(height: 8),
        if (!widget.sleeping)
          ExcludeSemantics(
            child: Icon(
              Icons.graphic_eq_rounded,
              size: 28,
              color: Theme.of(context).colorScheme.primary,
            ),
          ),
        if (!widget.sleeping && widget.caption != null) widget.caption!,
        if (!widget.sleeping) const SizedBox(height: 8),
        Wrap(
          key: const ValueKey('home-primary-actions'),
          alignment: WrapAlignment.center,
          spacing: 8,
          runSpacing: 8,
          children: [
            if (!widget.sleeping && widget.onCall != null) ...[
              Tooltip(
                message: widget.callTooltip,
                child: Semantics(
                  key: const ValueKey('home-talk'),
                  child: FilledButton.icon(
                    key: const ValueKey('call-kinaiya'),
                    onPressed: widget.onCall,
                    icon: Icon(
                      widget.callLabel == 'End conversation' ||
                              widget.callLabel == 'Stop microphone'
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
                child: Text(widget.setupLabel),
              ),
            ] else ...[
              FilledButton.icon(
                key: const ValueKey('home-setup'),
                onPressed: widget.onSetup,
                icon: const Icon(Icons.tune_rounded),
                label: Text(widget.setupLabel),
              ),
              if (!widget.sleeping)
                Tooltip(
                  message: widget.callTooltip,
                  child: Semantics(
                    key: const ValueKey('home-talk'),
                    child: OutlinedButton.icon(
                      key: const ValueKey('call-kinaiya'),
                      onPressed: null,
                      icon: const Icon(Icons.mic_none),
                      label: Text(widget.callLabel),
                    ),
                  ),
                ),
            ],
          ],
        ),
        if (widget.ownerControl != null)
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: widget.ownerControl!,
          ),
        if (!widget.showConverseView)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: TextButton.icon(
              key: const ValueKey('home-converse-hidden-setup'),
              onPressed: widget.onConverseSetup ?? widget.onSetup,
              icon: const Icon(Icons.visibility_outlined, size: 18),
              label: const Text(
                'Converse view is hidden · Enable it in Setup',
                textAlign: TextAlign.center,
              ),
            ),
          ),
      ],
    );

    return Center(
      child: ConstrainedBox(
        key: const ValueKey('home-status-panel'),
        constraints: BoxConstraints(
          maxWidth: effectiveWidth ?? math.min(440, maxWidth),
          minWidth: effectiveWidth ?? math.min(440, maxWidth),
          maxHeight: effectiveHeight ?? double.infinity,
          minHeight: effectiveHeight ?? 0,
        ),
        child: KeyedSubtree(
          key: _panelKey,
          child: CallbackShortcuts(
            bindings: {
              const SingleActivator(LogicalKeyboardKey.arrowLeft): () =>
                  _resizeFromLeft
                  ? _resizePanelEdges(left: -24)
                  : _resizePanelEdges(right: -24),
              const SingleActivator(LogicalKeyboardKey.arrowRight): () =>
                  _resizeFromLeft
                  ? _resizePanelEdges(left: 24)
                  : _resizePanelEdges(right: 24),
              const SingleActivator(LogicalKeyboardKey.arrowUp): () =>
                  _resizeFromBottom
                  ? _resizePanelEdges(bottom: -24)
                  : _resizePanelEdges(top: -24),
              const SingleActivator(LogicalKeyboardKey.arrowDown): () =>
                  _resizeFromBottom
                  ? _resizePanelEdges(bottom: 24)
                  : _resizePanelEdges(top: 24),
            },
            child: Focus(
              focusNode: _resizeFocus,
              child: Stack(
                fit: StackFit.passthrough,
                children: [
                  GestureDetector(
                    onPanUpdate: (details) => _movePanel(details.delta),
                    child: _card(
                      key: const ValueKey('home-action-card'),
                      prominent: true,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 18,
                        vertical: 14,
                      ),
                      effectiveHeight == null
                          ? content
                          : LayoutBuilder(
                              builder: (context, innerBox) => Center(
                                child: FittedBox(
                                  fit: BoxFit.scaleDown,
                                  child: SizedBox(
                                    width: innerBox.maxWidth,
                                    child: content,
                                  ),
                                ),
                              ),
                            ),
                    ),
                  ),
                  ..._windowResizeHandles(),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

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
                child: Builder(
                  builder: (context) {
                    final artwork = AnimatedBuilder(
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
                    );
                    return widget.sleepingCharacterWrapper?.call(artwork) ??
                        Semantics(
                          button: true,
                          label: 'Open Kinaiya setup',
                          child: InkWell(onTap: widget.onSetup, child: artwork),
                        );
                  },
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
                child: _homePanel(box.maxWidth, height),
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
