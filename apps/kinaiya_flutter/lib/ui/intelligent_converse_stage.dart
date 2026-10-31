/// Presentation only: the existing CharacterView retains audio/session ownership.
library;

import 'dart:async';
import 'package:flutter/material.dart';

import '../artifacts/models.dart';
import '../artifacts/renderer.dart';
import '../state/app_state.dart';
import 'design_tokens.dart';

class IntelligentConverseStage extends StatefulWidget {
  const IntelligentConverseStage({
    super.key,
    required this.state,
    required this.character,
    required this.transcript,
    required this.composer,
    required this.controls,
    required this.normalBuilder,
    required this.onHistory,
    required this.onAsk,
    required this.richEnabled,
    required this.onRichEnabled,
    required this.animate,
  });
  final AppState state;
  final Widget character, transcript, composer, controls;
  final Widget Function(Widget character, Widget transcript, Widget composer)
  normalBuilder;
  final VoidCallback onHistory;
  final ValueChanged<String> onAsk;
  final bool richEnabled, animate;
  final ValueChanged<bool>? onRichEnabled;

  @override
  State<IntelligentConverseStage> createState() =>
      _IntelligentConverseStageState();
}

class _IntelligentConverseStageState extends State<IntelligentConverseStage> {
  final _portraitKey = GlobalKey(),
      _transcriptKey = GlobalKey(),
      _composerKey = GlobalKey(),
      _hostKey = GlobalKey(),
      _artifactStackKey = GlobalKey(),
      _artifactBodyKey = GlobalKey();
  IntelligentArtifact? _displayed;
  Rect? _normalGlobalRect, _cornerRect;
  Offset? _hostOrigin;
  Size? _normalViewport;
  Timer? _returnTimer;
  bool _corner = false,
      _collapsed = false,
      _measurementScheduled = false,
      _returning = false;
  int _transition = 0;
  int? _ownerGeneration, _ownerEpoch;
  String? _ownerSession, _ownerThread;

  bool _matchesOwner(IntelligentArtifact artifact) {
    final session = widget.state.converseSession;
    return session != null &&
        artifact.sessionId == session.sessionId &&
        artifact.threadId == session.threadId &&
        artifact.epoch == session.epoch;
  }

  IntelligentArtifact? get _currentArtifact {
    final artifact = widget.state.activeArtifact;
    return artifact != null && _matchesOwner(artifact) ? artifact : null;
  }

  List<IntelligentArtifact> get _artifacts =>
      widget.state.converseArtifacts.where(_matchesOwner).toList();

  bool get _sameOwner {
    final session = widget.state.converseSession;
    return _ownerGeneration == widget.state.connectionGeneration &&
        _ownerSession == session?.sessionId &&
        _ownerThread == session?.threadId &&
        _ownerEpoch == session?.epoch;
  }

  void _captureOwner() {
    final session = widget.state.converseSession;
    _ownerGeneration = widget.state.connectionGeneration;
    _ownerSession = session?.sessionId;
    _ownerThread = session?.threadId;
    _ownerEpoch = session?.epoch;
  }

  Widget get _portrait => Semantics(
    key: _portraitKey,
    container: true,
    explicitChildNodes: true,
    child: RepaintBoundary(child: widget.character),
  );
  Widget get _transcript => Semantics(
    key: _transcriptKey,
    container: true,
    explicitChildNodes: true,
    child: widget.transcript,
  );
  Widget get _composer => Semantics(
    key: _composerKey,
    container: true,
    explicitChildNodes: true,
    child: widget.composer,
  );
  bool get _blocked =>
      widget.state.artifactEditing ||
      widget.state.artifactActionPending ||
      widget.state.artifactActionUncertain;

  @override
  void initState() {
    super.initState();
    _captureOwner();
    _displayed = _currentArtifact;
  }

  void _scheduleGeometry({double? rail}) {
    if (_measurementScheduled) return;
    _measurementScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _measurementScheduled = false;
      if (!mounted) return;
      if (_displayed == null) {
        _captureNormalRect();
        return;
      }
      final host = _hostKey.currentContext?.findRenderObject();
      final stage = _artifactStackKey.currentContext?.findRenderObject();
      if (rail != null &&
          host is RenderBox &&
          stage is RenderBox &&
          host.hasSize &&
          stage.hasSize) {
        final origin = host.localToGlobal(Offset.zero);
        final local = stage.localToGlobal(Offset.zero) - origin;
        final width = rail.clamp(0.0, stage.size.width);
        final height = (width * 2 / 3).clamp(0.0, stage.size.height * .55);
        final corner = Rect.fromLTWH(
          local.dx + stage.size.width - width,
          local.dy + stage.size.height - height,
          width,
          height,
        );
        final changed =
            _hostOrigin != origin ||
            _cornerRect != corner ||
            (!_corner && _currentArtifact != null);
        _hostOrigin = origin;
        _cornerRect = corner;
        if (changed) {
          setState(() {
            if (_currentArtifact != null) _corner = true;
          });
        }
      } else if (!_corner && _currentArtifact != null) {
        setState(() => _corner = true);
      }
    });
  }

  void _captureNormalRect() {
    final box = _portraitKey.currentContext?.findRenderObject();
    final host = _hostKey.currentContext?.findRenderObject();
    if (box is RenderBox && host is RenderBox && box.hasSize && host.hasSize) {
      _normalGlobalRect = box.localToGlobal(Offset.zero) & box.size;
      _hostOrigin = host.localToGlobal(Offset.zero);
      _normalViewport = host.size;
    }
  }

  @override
  void didUpdateWidget(IntelligentConverseStage oldWidget) {
    super.didUpdateWidget(oldWidget);
    final next = _currentArtifact;
    if (!_sameOwner) {
      // Owner invalidation is not the user's Close gesture. Private content
      // leaves the tree immediately while the existing portrait is reparented.
      _captureOwner();
      _returnTimer?.cancel();
      _transition++;
      _returning = _corner = _collapsed = false;
      _displayed = next;
      _normalGlobalRect = _cornerRect = null;
      _hostOrigin = null;
      _normalViewport = null;
      return;
    }
    if (next != null) {
      _returnTimer?.cancel();
      _returning = false;
      if (_displayed == null) {
        _captureNormalRect();
        _corner = false;
        _cornerRect = null;
      }
      _displayed = next;
      _transition++;
    } else if (_displayed != null && !_returning) {
      _returnTimer?.cancel();
      _returning = true;
      _corner = false;
      final transition = ++_transition;
      _returnTimer = Timer(_duration, () {
        if (mounted && transition == _transition) _finishReturn();
      });
    }
  }

  @override
  void dispose() {
    _returnTimer?.cancel();
    super.dispose();
  }

  void _finishReturn() {
    if (mounted && _currentArtifact == null && !_corner) {
      setState(() {
        _displayed = null;
        _returning = false;
      });
    }
  }

  Duration get _duration =>
      widget.animate && !MediaQuery.disableAnimationsOf(context)
      ? const Duration(milliseconds: 320)
      : Duration.zero;

  Rect _returnRect(Size viewport) {
    final captured = _normalGlobalRect;
    final origin = _hostOrigin;
    if (captured != null && origin != null) {
      final local = captured.shift(-origin);
      if (_normalViewport == viewport) return local;
      // A resized viewport has a different normal composition. Keep the
      // transition within its actual new bounds until that composition returns.
      final width = local.width.clamp(0.0, viewport.width);
      final height = local.height.clamp(0.0, viewport.height);
      return Rect.fromLTWH(
        local.left.clamp(0.0, viewport.width - width),
        local.top.clamp(0.0, viewport.height - height),
        width,
        height,
      );
    }
    return Offset.zero & viewport;
  }

  Widget _toolbar({bool heading = true}) => Wrap(
    crossAxisAlignment: WrapCrossAlignment.center,
    spacing: 12,
    runSpacing: 8,
    children: [
      if (heading)
        Text('Converse', style: Theme.of(context).textTheme.headlineSmall),
      if (_artifacts.isNotEmpty)
        MenuAnchor(
          key: const ValueKey('converse-artifact-picker'),
          style: const MenuStyle(
            maximumSize: WidgetStatePropertyAll(Size(320, 480)),
          ),
          menuChildren: [
            for (final artifact in _artifacts)
              MenuItemButton(
                onPressed: _blocked
                    ? null
                    : () => widget.state.chooseArtifact(artifact.artifactId),
                child: SizedBox(
                  width: 240,
                  child: Text(
                    artifact.title,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ),
          ],
          builder: (context, controller, _) => Tooltip(
            message: 'Open an Agent result',
            child: TextButton.icon(
              onPressed: _blocked
                  ? null
                  : () => controller.isOpen
                        ? controller.close()
                        : controller.open(),
              icon: const Icon(Icons.auto_awesome_outlined),
              label: const Text('Results'),
            ),
          ),
        ),
      FilterChip(
        key: const ValueKey('converse-rich-artifacts'),
        selected: widget.richEnabled,
        chipAnimationStyle: MediaQuery.disableAnimationsOf(context)
            ? ChipAnimationStyle(
                selectAnimation: AnimationStyle.noAnimation,
                avatarDrawerAnimation: AnimationStyle.noAnimation,
              )
            : null,
        label: const Text('Interactive views'),
        onSelected: _blocked ? null : widget.onRichEnabled,
      ),
      if (_displayed != null) ...[
        IconButton(
          key: const ValueKey('converse-artifact-pin'),
          tooltip: widget.state.artifactPinned ? 'Unpin result' : 'Pin result',
          onPressed: () =>
              widget.state.pinArtifact(!widget.state.artifactPinned),
          isSelected: widget.state.artifactPinned,
          icon: const Icon(Icons.push_pin_outlined),
          selectedIcon: const Icon(Icons.push_pin),
        ),
        IconButton(
          key: const ValueKey('converse-artifact-close'),
          tooltip: _blocked
              ? 'Finish or reconcile this interaction before closing'
              : 'Close result',
          onPressed: _blocked ? null : widget.state.closeArtifact,
          icon: const Icon(Icons.close),
        ),
      ],
    ],
  );

  Widget _transcriptRail({required double height}) => DesignPanel(
    padding: const EdgeInsets.all(12),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            const Expanded(
              child: Text(
                'Live transcript',
                style: TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            IconButton(
              tooltip: 'Conversation history',
              onPressed: widget.onHistory,
              icon: const Icon(Icons.history),
            ),
            IconButton(
              key: const ValueKey('artifact-transcript-collapse'),
              tooltip: _collapsed ? 'Show transcript' : 'Collapse transcript',
              onPressed: () => setState(() => _collapsed = !_collapsed),
              icon: Icon(_collapsed ? Icons.expand_more : Icons.expand_less),
            ),
          ],
        ),
        Offstage(
          offstage: _collapsed,
          child: ExcludeFocus(
            excluding: _collapsed,
            child: SizedBox(height: height, child: _transcript),
          ),
        ),
        if (_collapsed)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              widget.state.converseHistory.isEmpty
                  ? 'No conversation yet.'
                  : widget.state.converseHistory.last.text,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
            ),
          ),
      ],
    ),
  );

  Widget _artifactBody() {
    final artifact = _displayed!;
    final owner = widget.state, api = owner.client;
    final generation = owner.connectionGeneration;
    bool current() {
      final active = owner.activeArtifact;
      return mounted &&
          identical(widget.state, owner) &&
          owner.ownsConnection(generation, api) &&
          _matchesOwner(artifact) &&
          active?.artifactId == artifact.artifactId &&
          active?.revision == artifact.revision &&
          active?.sessionId == artifact.sessionId &&
          active?.threadId == artifact.threadId &&
          active?.epoch == artifact.epoch;
    }

    return KeyedSubtree(
      key: _artifactBodyKey,
      child: DesignPanel(
        key: const ValueKey('converse-intelligent-artifact'),
        padding: const EdgeInsets.all(16),
        child: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  const Icon(Icons.auto_awesome, size: 16),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'From Agent · revision ${artifact.revision}',
                      style: Theme.of(context).textTheme.labelMedium,
                    ),
                  ),
                  Tooltip(
                    message: artifact.taskId,
                    child: const Icon(Icons.link, size: 16),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              AbsorbPointer(
                absorbing:
                    widget.state.artifactActionPending ||
                    _currentArtifact == null,
                child: IntelligentArtifactRenderer(
                  key: ValueKey(artifact.artifactId),
                  artifact: artifact,
                  enabled: widget.richEnabled,
                  onAction: (component, value) {
                    if (!current()) return Future<void>.value();
                    return owner.interactWithArtifact(component, value);
                  },
                  onAsk: (message) {
                    if (current()) widget.onAsk(message);
                  },
                  onEditingChanged: (value) {
                    if (current()) owner.editArtifact(value);
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _feedback() => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      if (widget.state.artifactUpdateWaiting)
        const Text(
          'A newer version is waiting. Finish this interaction to show it.',
        ),
      if (widget.state.artifactError != null)
        Text(
          widget.state.artifactError!,
          key: const ValueKey('artifact-error'),
          style: TextStyle(color: Theme.of(context).colorScheme.error),
        ),
      if (widget.state.artifactActionPending) const LinearProgressIndicator(),
      if (widget.state.artifactActionUncertain)
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            onPressed: widget.state.retryArtifactInteraction,
            icon: const Icon(Icons.refresh),
            label: const Text('Retry unconfirmed interaction'),
          ),
        ),
    ],
  );

  @override
  Widget build(BuildContext context) => ColoredBox(
    key: const ValueKey('converse-stage-background'),
    color: Theme.of(context).colorScheme.surface,
    child: LayoutBuilder(
      builder: (context, bounds) {
        final duration = _duration;
        if (_displayed == null) {
          _scheduleGeometry();
          return Stack(
            key: _hostKey,
            fit: StackFit.expand,
            children: [
              Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(24, 12, 24, 0),
                    child: _toolbar(heading: false),
                  ),
                  Expanded(
                    child: widget.normalBuilder(
                      _portrait,
                      _transcript,
                      _composer,
                    ),
                  ),
                ],
              ),
            ],
          );
        }
        if (duration == Duration.zero && !_corner && _currentArtifact == null) {
          WidgetsBinding.instance.addPostFrameCallback((_) => _finishReturn());
        }
        final wide =
            bounds.maxWidth >= 980 &&
            bounds.maxHeight >= 760 &&
            MediaQuery.textScalerOf(context).scale(16) <= 24;
        if (!wide) {
          _scheduleGeometry();
          // A scrollable stack keeps both the native artifact and ordinary controls
          // reachable under large text, narrow screens and the on-screen keyboard.
          return Stack(
            key: _hostKey,
            fit: StackFit.expand,
            children: [
              SingleChildScrollView(
                key: const ValueKey('converse-artifact-scroll'),
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    _toolbar(),
                    const SizedBox(height: 12),
                    _feedback(),
                    _artifactBody(),
                    const SizedBox(height: 12),
                    _transcriptRail(height: 180),
                    const SizedBox(height: 12),
                    SizedBox(height: 180, child: _portrait),
                    const SizedBox(height: 12),
                    widget.controls,
                    const SizedBox(height: 12),
                    _composer,
                  ],
                ),
              ),
            ],
          );
        }
        final width = bounds.maxWidth - 48;
        final rail = (width * .26).clamp(260.0, 360.0);
        _scheduleGeometry(rail: rail);
        final target = _corner && _cornerRect != null
            ? _cornerRect!
            : _returnRect(bounds.biggest);
        return Stack(
          key: _hostKey,
          fit: StackFit.expand,
          children: [
            Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  _toolbar(),
                  const SizedBox(height: 8),
                  _feedback(),
                  const SizedBox(height: 8),
                  Expanded(
                    child: LayoutBuilder(
                      builder: (context, stage) {
                        final portraitHeight = (rail * 2 / 3).clamp(
                          0.0,
                          stage.maxHeight * .55,
                        );
                        return Stack(
                          key: _artifactStackKey,
                          children: [
                            Positioned(
                              left: 0,
                              top: 0,
                              bottom: 0,
                              right: rail + 16,
                              child: _artifactBody(),
                            ),
                            Positioned(
                              top: 0,
                              right: 0,
                              width: rail,
                              bottom: portraitHeight + 12,
                              child: SingleChildScrollView(
                                child: _transcriptRail(
                                  height:
                                      (stage.maxHeight - portraitHeight - 90)
                                          .clamp(80, 600),
                                ),
                              ),
                            ),
                          ],
                        );
                      },
                    ),
                  ),
                  const SizedBox(height: 12),
                  widget.controls,
                  const SizedBox(height: 10),
                  _composer,
                ],
              ),
            ),
            AnimatedPositioned(
              key: const ValueKey('artifact-portrait-position'),
              duration: duration,
              curve: Curves.easeInOutCubic,
              left: target.left,
              top: target.top,
              width: target.width,
              height: target.height,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(16),
                child: _portrait,
              ),
            ),
          ],
        );
      },
    ),
  );
}
