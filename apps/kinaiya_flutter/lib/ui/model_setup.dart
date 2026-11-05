import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/converse_setup.dart';
import '../presentation/controller.dart';
import '../state/app_state.dart';
import 'floating_bubbles.dart';
import 'bubble_options_morph.dart';
import 'options_converse_model.dart';

/// Read-only host configuration and a separately saved local view preference.
/// Opening this surface never loads a model, captures audio or changes consent.
class ModelSetupDialog extends StatefulWidget {
  const ModelSetupDialog({
    super.key,
    required this.state,
    required this.presentation,
    this.voiceControls,
    this.bubbleRandom,
  });
  final AppState state;
  final PresentationController presentation;
  final Widget? voiceControls;

  /// Optional deterministic simulation input; no provider/runtime setting.
  final math.Random? bubbleRandom;
  @override
  State<ModelSetupDialog> createState() => _ModelSetupDialogState();
}

class _ModelSetupDialogState extends State<ModelSetupDialog>
    with SingleTickerProviderStateMixin, WidgetsBindingObserver {
  ConverseSetup? _setup;
  bool _loading = false, _saving = false;
  late bool _showConverse;
  String? _error, _saveNotice;
  int _request = 0, _connection = -1;
  late final AnimationController _expansion;
  final _stageKey = GlobalKey();
  final _modalCloseFocus = FocusNode();
  final _morphFocus = FocusNode();
  Rect? _modalOrigin;
  bool _closing = false;
  bool _foreground = true;
  int _geometryGeneration = 0;

  static const _sections = <String, IconData>{
    'Setup': Icons.tune_outlined,
    'Model': Icons.settings_outlined,
    'Reasoning': Icons.auto_awesome_outlined,
    'Inputs': Icons.article_outlined,
    'Speech': Icons.record_voice_over_outlined,
    'Decisions': Icons.diamond_outlined,
    'View': Icons.visibility_outlined,
    'Close': Icons.close_rounded,
  };
  final _bubbleKeys = {for (final name in _sections.keys) name: GlobalKey()};

  @override
  void initState() {
    super.initState();
    _foreground =
        WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
    WidgetsBinding.instance.addObserver(this);
    _expansion =
        AnimationController(
            vsync: this,
            duration: const Duration(milliseconds: 260),
          )
          ..addListener(_interactionChanged)
          ..addStatusListener(_morphStatus);
    _showConverse = widget.presentation.preferences.showConverseView;
    widget.state.addListener(_changed);
    widget.presentation.addListener(_presentationChanged);
    for (final node in _bubbleFocus.values) {
      node.addListener(_interactionChanged);
    }
    _refresh();
  }

  void _changed() {
    if (_connection != widget.state.connectionGeneration) _refresh();
  }

  void _presentationChanged() {
    if (!mounted) return;
    _snapMorphIfNeeded();
    setState(() {});
  }

  bool get _motionAllowed =>
      _foreground &&
      widget.presentation.preferences.animateTransitions &&
      !widget.presentation.preferences.reducedMotion &&
      !MediaQuery.disableAnimationsOf(context);

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _snapMorphIfNeeded();
  }

  void _snapMorphIfNeeded() {
    if (_motionAllowed || !_expansion.isAnimating) return;
    _expansion.stop();
    if (_closing) {
      _completeSectionClose();
    } else {
      _expansion.value = 1;
    }
  }

  void _morphStatus(AnimationStatus status) {
    if (status == AnimationStatus.dismissed && _closing) {
      _completeSectionClose();
    } else if (status == AnimationStatus.completed && _section != null) {
      final section = _section;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && !_closing && _section == section) {
          _modalCloseFocus.requestFocus();
        }
      });
    }
  }

  void _interactionChanged() {
    if (mounted) setState(() {});
  }

  final _hovered = <String>{};
  final _releasedUnderPointer = <String>{};
  final _pressed = <int, String>{};
  Size? _bubbleLayoutSize;
  double? _bubbleDiameter;

  void _hover(String section, bool value) {
    if (!mounted) return;
    setState(() {
      if (value) {
        if (!_releasedUnderPointer.contains(section)) _hovered.add(section);
      } else {
        _hovered.remove(section);
        _releasedUnderPointer.remove(section);
      }
    });
  }

  void _press(int pointer, String? section) {
    // Pointer routes can outlive the dialog until the native up/cancel event.
    if (!mounted) return;
    setState(() {
      if (section == null) {
        _pressed.remove(pointer);
      } else {
        _pressed[pointer] = section;
      }
    });
  }

  Set<String> get _heldBubbles => {
    ..._hovered,
    ..._pressed.values,
    for (final item in _bubbleFocus.entries)
      if (item.value.hasFocus) item.key,
    if (_section != null) _section!,
    if (_restoringFocus != null) _restoringFocus!,
  };

  void _dragChanged(String section) {
    if (!mounted) return;
    _bubbleFocus[section]?.unfocus();
    setState(() {
      // The body may enter beneath the stationary release pointer on the
      // next layout. Let it leave once before hover can hold it again.
      _releasedUnderPointer.add(section);
      _hovered.remove(section);
      _pressed.removeWhere((_, value) => value == section);
    });
  }

  void _resumeForPointer() {
    if (_section != null) return;
    // Escape restores keyboard focus. Once the user returns to the pointer
    // outside a bubble, that focus must not latch motion off indefinitely.
    for (final entry in _bubbleFocus.entries) {
      if (entry.value.hasFocus && !_hovered.contains(entry.key)) {
        entry.value.unfocus();
      }
    }
  }

  @override
  void didUpdateWidget(covariant ModelSetupDialog oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.state, widget.state)) {
      oldWidget.state.removeListener(_changed);
      widget.state.addListener(_changed);
      _refresh();
    }
    if (!identical(oldWidget.presentation, widget.presentation)) {
      oldWidget.presentation.removeListener(_presentationChanged);
      widget.presentation.addListener(_presentationChanged);
      _showConverse = widget.presentation.preferences.showConverseView;
      _saving = false;
      _saveNotice = null;
    }
  }

  Future<void> _refresh() async {
    final owner = widget.state;
    final api = owner.client;
    final generation = owner.connectionGeneration;
    final request = ++_request;
    _connection = generation;
    setState(() {
      _setup = null;
      _error = null;
      _loading = api != null;
    });
    if (api == null) {
      setState(
        () => _error = 'Connect to your host to inspect configured models.',
      );
      return;
    }
    try {
      final setup = await api.converseSetup().timeout(
        const Duration(seconds: 8),
      );
      if (!mounted ||
          !identical(owner, widget.state) ||
          request != _request ||
          !owner.ownsConnection(generation, api)) {
        return;
      }
      setState(() {
        _setup = setup;
        _loading = false;
      });
    } catch (_) {
      if (!mounted ||
          !identical(owner, widget.state) ||
          request != _request ||
          !owner.ownsConnection(generation, api)) {
        return;
      }
      setState(() {
        _loading = false;
        _error = 'Model configuration is unavailable from this host. No model settings were changed.';
      });
    }
  }

  Future<void> _save() async {
    if (_saving) return;
    setState(() => _saving = true);
    // Merge against current preferences, not the snapshot from dialog open.
    final presentation = widget.presentation;
    await presentation.setPreferences(
      presentation.preferences.copyWith(showConverseView: _showConverse),
    );
    if (!mounted || !identical(presentation, widget.presentation)) return;
    if (presentation.persistent && presentation.notice != null) {
      setState(() {
        _saving = false;
        _saveNotice = presentation.notice;
      });
      return;
    }
    Navigator.of(context).pop();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    if (!_foreground) {
      _hovered.clear();
      _releasedUnderPointer.clear();
      _pressed.clear();
      _snapMorphIfNeeded();
    }
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _request++;
    _geometryGeneration++;
    _expansion
      ..removeListener(_interactionChanged)
      ..removeStatusListener(_morphStatus)
      ..dispose();
    _modalCloseFocus.dispose();
    _morphFocus.dispose();
    widget.state.removeListener(_changed);
    widget.presentation.removeListener(_presentationChanged);
    for (final node in _bubbleFocus.values) {
      node.removeListener(_interactionChanged);
      node.dispose();
    }
    super.dispose();
  }

  String? _section, _restoringFocus;
  final Map<String, FocusNode> _bubbleFocus = {
    for (final section in _sections.keys) section: FocusNode(),
  };

  Rect? _bubbleRect(String section) {
    final bubble = _bubbleKeys[section]?.currentContext?.findRenderObject();
    final stage = _stageKey.currentContext?.findRenderObject();
    if (bubble is! RenderBox || stage is! RenderBox || !bubble.hasSize) {
      return null;
    }
    return stage.globalToLocal(bubble.localToGlobal(Offset.zero)) & bubble.size;
  }

  void _dismissSetup() {
    if (mounted && !_saving) Navigator.of(context).pop();
  }

  void _openSection(String section) {
    if (_section != null) return;
    final origin = _bubbleRect(section);
    if (origin == null) return;
    setState(() {
      _section = section;
      _closing = false;
      _modalOrigin = origin;
    });
    _morphFocus.requestFocus();
    if (_motionAllowed) {
      _expansion.forward(from: 0);
    } else {
      _expansion.value = 1;
      _morphStatus(AnimationStatus.completed);
    }
  }

  void _closeSection() {
    if (_section == null || _closing) return;
    _closing = true;
    _morphFocus.requestFocus();
    if (_motionAllowed && _expansion.value > 0) {
      _expansion.reverse();
    } else {
      _expansion.value = 0;
      if (_section != null) _completeSectionClose();
    }
  }

  void _completeSectionClose() {
    final section = _section;
    final node = _bubbleFocus[section];
    setState(() {
      _restoringFocus = section;
      _section = null;
      _closing = false;
      _modalOrigin = null;
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || _restoringFocus != section) return;
      node?.requestFocus();
      setState(() => _restoringFocus = null);
    });
  }

  Widget _role(String key, String title) {
    final role = _setup?.roles[key];
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(title, style: Theme.of(context).textTheme.titleSmall),
          const SizedBox(height: 8),
          InputDecorator(
            decoration: const InputDecoration(
              suffixIcon: Icon(Icons.lock_outline, size: 18),
              enabled: false,
              contentPadding: EdgeInsets.all(14),
            ),
            child: Text(
              role?.label ?? 'Not declared by this host',
              key: ValueKey('model-$key'),
            ),
          ),
          const SizedBox(height: 6),
          Text(
            role?.reason ?? 'Host configuration unavailable.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }

  String _value(String section) => switch (section) {
    'Close' => 'Return to Kinaiya',
    'Setup' =>
      _loading
          ? 'Checking host'
          : (_error != null ? 'Unavailable' : 'Host configuration'),
    'View' => _showConverse ? 'Converse visible' : 'Converse hidden',
    'Model' => _setup?.roles['conversation']?.label ?? 'Unavailable',
    'Reasoning' =>
      _setup == null
          ? 'Unavailable'
          : _setup!.reasoning == 'none'
          ? 'Off'
          : _setup!.reasoning,
    'Inputs' => _setup == null ? 'Unavailable' : 'Text',
    'Speech' => _setup == null ? 'Unavailable' : 'Transcription',
    'Decisions' => _setup?.roles['decisions']?.label ?? 'Unavailable',
    _ => '',
  };

  Widget _bubbleFace(String section, IconData icon, double diameter) {
    final colors = Theme.of(context).colorScheme;
    final compact = diameter < 132;
    return Container(
      width: diameter,
      height: diameter,
      padding: EdgeInsets.all(compact ? 12 : 17),
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            colors.surface,
            colors.primaryContainer.withValues(alpha: .65),
          ],
        ),
        border: Border.all(
          color: colors.primary.withValues(alpha: .28),
          width: 5,
        ),
      ),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(icon, color: colors.primary, size: compact ? 22 : 28),
          SizedBox(height: compact ? 4 : 6),
          Text(
            section,
            style: Theme.of(context).textTheme.titleMedium,
            textAlign: TextAlign.center,
          ),
          SizedBox(height: compact ? 2 : 4),
          Flexible(
            child: Text(
              _value(section),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.center,
              style: Theme.of(context).textTheme.bodySmall
                  ?.copyWith(color: colors.primary),
            ),
          ),
        ],
      ),
    );
  }

  Widget _bubble(String section, IconData icon, double diameter) {
    final colors = Theme.of(context).colorScheme;
    return MouseRegion(
      key: _bubbleKeys[section],
      hitTestBehavior: HitTestBehavior.deferToChild,
      onEnter: (_) => _hover(section, true),
      onExit: (_) => _hover(section, false),
      child: Listener(
        onPointerDown: (event) => _press(event.pointer, section),
        onPointerUp: (event) => _press(event.pointer, null),
        onPointerCancel: (event) => _press(event.pointer, null),
        child: Semantics(
          label: section == 'Close' ? 'Close setup' : '$section settings',
          enabled: section != 'Close' || !_saving,
          value: _value(section),
          button: true,
          child: Tooltip(
            message: section == 'Close'
                ? 'Close setup'
                : 'Inspect $section settings',
            child: Material(
              key: ValueKey('setup-bubble-${section.toLowerCase()}'),
              shape: CircleBorder(
                side: BorderSide(color: colors.surface, width: 3),
              ),
              elevation: 4,
              color: colors.surface.withValues(alpha: .94),
              clipBehavior: Clip.antiAlias,
              child: InkWell(
                key: ValueKey('mascot-setup-bubble-${section.toLowerCase()}'),
                customBorder: const CircleBorder(),
                focusNode: _bubbleFocus[section],
                canRequestFocus: _section == null,
                onTap: section == 'Close'
                    ? (_saving ? null : _dismissSetup)
                    : () => _openSection(section),
                child: ExcludeSemantics(
                  child: _bubbleFace(section, icon, diameter),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _hostSetup() => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      const Text('Kinaiya setup'),
      if (_loading) const LinearProgressIndicator(),
      if (_error != null)
        Text(_error!, key: const ValueKey('model-setup-unavailable')),
      TextButton.icon(
        onPressed: _loading ? null : _refresh,
        icon: const Icon(Icons.refresh),
        label: const Text('Refresh host configuration'),
      ),
    ],
  );

  Widget _sectionContent() => switch (_section) {
    'Setup' => _hostSetup(),
    'View' => _preferences(),
    'Model' => _role('conversation', 'Conversation model'),
    'Reasoning' => Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final level in ['Off', 'Auto', 'Low', 'Medium', 'High'])
              ChoiceChip(
                label: Text(level),
                onSelected: null,
                selected:
                    _setup != null &&
                    _setup!.reasoning ==
                        (level == 'Off' ? 'none' : level.toLowerCase()),
              ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          _setup == null
              ? 'Reasoning configuration is unavailable.'
              : 'Configured: ${_value('Reasoning')}. Reasoning changes are managed on the host.',
        ),
      ],
    ),
    'Inputs' => Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          children: [
            for (final input in ['Text', 'Image', 'Video', 'Audio'])
              FilterChip(
                label: Text(input),
                selected: _setup != null && input == 'Text',
                onSelected: null,
              ),
          ],
        ),
        const SizedBox(height: 12),
        const Text(
          'This host supports text input. Microphone audio uses speech-to-text; direct audio, images and video are unavailable.',
        ),
      ],
    ),
    'Speech' => Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _role('stt', 'Speech-to-text'),
        const SwitchListTile.adaptive(
          contentPadding: EdgeInsets.zero,
          value: true,
          onChanged: null,
          title: Text('Transcribe microphone audio'),
          subtitle: Text('Required by this host’s text conversation pipeline.'),
        ),
        const SizedBox(height: 12),
        _role('tts', 'Text-to-speech'),
      ],
    ),
    'Decisions' => _role('decisions', 'Decisions model'),
    _ => const SizedBox.shrink(),
  };

  Widget _popover() {
    final colors = Theme.of(context).colorScheme;
    return Material(
      color: colors.surface.withValues(alpha: .98),
      elevation: 12,
      borderRadius: BorderRadius.circular(20),
      child: CallbackShortcuts(
        bindings: {
          const SingleActivator(LogicalKeyboardKey.escape): _closeSection,
        },
        child: FocusScope(
          autofocus: true,
          child: SingleChildScrollView(
            key: const ValueKey('setup-popover'),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(20, 8, 12, 16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          '$_section settings',
                          style: Theme.of(context).textTheme.titleLarge,
                        ),
                      ),
                      IconButton(
                        tooltip: 'Close settings popover',
                        focusNode: _modalCloseFocus,
                        onPressed: _closeSection,
                        icon: const Icon(Icons.close),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  _sectionContent(),
                  if (_section != 'View')
                    const Text(
                      'Host configuration is read-only. A declaration does not establish model readiness.',
                    ),
                  Align(
                    alignment: Alignment.centerRight,
                    child: TextButton(
                      onPressed: _closeSection,
                      child: const Text('Done'),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _preferences() => Material(
    color: Theme.of(context).colorScheme.surface.withValues(alpha: .97),
    borderRadius: BorderRadius.circular(18),
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (_saveNotice != null)
            Text(_saveNotice!, key: const ValueKey('setup-save-notice')),
          SwitchListTile.adaptive(
            contentPadding: EdgeInsets.zero,
            title: const Text('Converse view'),
            subtitle: const Text(
              'Optional mascot interaction. Home voice stays available when hidden.',
            ),
            key: const ValueKey('show-converse-view'),
            value: _showConverse,
            onChanged: _saving
                ? null
                : (value) => setState(() => _showConverse = value),
          ),
          if (!widget.presentation.persistent)
            const Text(
              'This platform keeps the view preference for this session.',
            ),
          const Text(
            'Save updates only the view on this device. Model choices are configured on the host.',
          ),
          const SizedBox(height: 8),
          Wrap(
            alignment: WrapAlignment.end,
            spacing: 12,
            runSpacing: 8,
            children: [
              TextButton(
                onPressed: _saving ? null : () => Navigator.of(context).pop(),
                child: const Text('Cancel'),
              ),
              FilledButton(
                key: const ValueKey('save-model-setup'),
                onPressed: _saving ? null : _save,
                child: Text(_saving ? 'Saving…' : 'Save setup'),
              ),
            ],
          ),
        ],
      ),
    ),
  );

  void _rebaseModalAfterLayout() {
    final generation = ++_geometryGeneration;
    final section = _section;
    if (section == null) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted ||
          generation != _geometryGeneration ||
          section != _section) {
        return;
      }
      final origin = _bubbleRect(section);
      if (origin != null) setState(() => _modalOrigin = origin);
    });
  }

  @override
  Widget build(BuildContext context) =>
      PopScope(canPop: !_saving, child: _dialog(context));

  Widget _dialog(BuildContext context) => Dialog(
    key: const ValueKey('model-setup-dialog'),
    backgroundColor: Colors.transparent,
    shadowColor: Colors.transparent,
    insetPadding: const EdgeInsets.all(16),
    child: CallbackShortcuts(
      bindings: {
        if (_section != null)
          const SingleActivator(LogicalKeyboardKey.escape): _closeSection,
      },
      child: Focus(
        focusNode: _morphFocus,
        child: Listener(
          behavior: HitTestBehavior.translucent,
          onPointerHover: (_) => _resumeForPointer(),
          child: Column(
            children: [
              if (widget.voiceControls != null) widget.voiceControls!,
              Expanded(
                child: LayoutBuilder(
                  builder: (context, box) {
                    final scale = MediaQuery.textScalerOf(context).scale(1);
                    final wide =
                        box.maxWidth / scale >= 1050 &&
                        box.maxHeight / scale >= 650;
                    final compactTwoRow =
                        !wide &&
                        box.maxHeight <= 380 * scale &&
                        box.maxWidth >= (4 * 116 * scale + 48);
                    double narrowDiameter() {
                      final maxDiameter = (160 * scale).clamp(
                        0.0,
                        box.maxWidth - 16,
                      );
                      final minDiameter = ((compactTwoRow ? 116 : 132) * scale)
                          .clamp(0.0, maxDiameter);
                      final rowExtra = compactTwoRow ? 8.0 : 24.0;
                      final verticalPad = compactTwoRow ? 12.0 : 64.0;
                      for (
                        var candidate = maxDiameter;
                        candidate >= minDiameter;
                        candidate -= 8 * scale
                      ) {
                        final cols = ((box.maxWidth + 16) / (candidate + 16))
                            .floor()
                            .clamp(1, _sections.length);
                        final rows = (_sections.length / cols).ceil();
                        if (rows * (candidate + rowExtra) + verticalPad <=
                            box.maxHeight) {
                          return candidate;
                        }
                      }
                      return compactTwoRow ? minDiameter : maxDiameter;
                    }

                    final diameter = wide ? 178 * scale : narrowDiameter();
                    if (_bubbleLayoutSize != box.biggest ||
                        _bubbleDiameter != diameter) {
                      // Responsive replacement does not deliver MouseRegion.onExit.
                      _hovered.clear();
                      _releasedUnderPointer.clear();
                      _pressed.clear();
                      _bubbleLayoutSize = box.biggest;
                      _bubbleDiameter = diameter;
                      _rebaseModalAfterLayout();
                    }
                    final bubbles = {
                      for (final item in _sections.entries)
                        item.key: _bubble(item.key, item.value, diameter),
                    };
                    Widget flow() => Wrap(
                      alignment: WrapAlignment.center,
                      spacing: 16,
                      runSpacing: 16,
                      children: [
                        for (final item in bubbles.entries)
                          Opacity(
                            opacity: _section == item.key ? 0 : 1,
                            child: item.value,
                          ),
                      ],
                    );
                    final columns = ((box.maxWidth + 16) / (diameter + 16))
                        .floor()
                        .clamp(1, _sections.length);
                    final rows = (_sections.length / columns).ceil();
                    final rowExtra = compactTwoRow ? 8.0 : 24.0;
                    final verticalPad = compactTwoRow ? 12.0 : 64.0;
                    final fieldHeight =
                        wide ||
                            (compactTwoRow &&
                                rows <= 2 &&
                                rows * (diameter + 2) <= box.maxHeight)
                        ? box.maxHeight
                        : math.max(
                            box.maxHeight,
                            rows * (diameter + rowExtra) + verticalPad,
                          );
                    final field = SizedBox(
                      height: fieldHeight,
                      child: FocusTraversalGroup(
                        policy: OrderedTraversalPolicy(),
                        child: FloatingBubbles(
                          key: ValueKey(
                            wide
                                ? 'setup-floating-wide'
                                : 'setup-floating-narrow',
                          ),
                          children: bubbles,
                          diameter: diameter,
                          random: widget.bubbleRandom,
                          enabled: _motionAllowed,
                          paused: false,
                          heldKeys: _heldBubbles,
                          hiddenKeys: {if (_section != null) _section!},
                          onDragStarted: _dragChanged,
                          onDragEnded: _dragChanged,
                          obstacles: [
                            if (wide)
                              Rect.fromLTWH(
                                box.maxWidth / 2 - 110 * scale,
                                box.maxHeight * .3,
                                220 * scale,
                                box.maxHeight * .4,
                              ),
                          ],
                          fallback: SingleChildScrollView(
                            child: Padding(
                              padding: const EdgeInsets.all(8),
                              child: flow(),
                            ),
                          ),
                        ),
                      ),
                    );
                    final modalWidth = math.min(
                      440 * scale,
                      math.max(0.0, box.maxWidth - 24),
                    );
                    final desiredHeight = switch (_section) {
                      'Speech' => 580.0,
                      'View' => 440.0,
                      'Inputs' => 400.0,
                      _ => 350.0,
                    };
                    final modalHeight = math.min(
                      desiredHeight * scale,
                      math.max(0.0, box.maxHeight - 24),
                    );
                    final target = Rect.fromLTWH(
                      (box.maxWidth - modalWidth) / 2,
                      (box.maxHeight - modalHeight) / 2,
                      modalWidth,
                      modalHeight,
                    );
                    return Stack(
                      key: _stageKey,
                      clipBehavior: Clip.hardEdge,
                      children: [
                        Positioned.fill(
                          child: GestureDetector(
                            key: const ValueKey('setup-background-dismiss'),
                            behavior: HitTestBehavior.opaque,
                            excludeFromSemantics: true,
                            onTap: _dismissSetup,
                            child: IgnorePointer(
                              ignoring: _section != null,
                              child: ExcludeFocus(
                                excluding: _section != null,
                                child: ExcludeSemantics(
                                  excluding: _section != null,
                                  child: wide
                                      ? field
                                      : SingleChildScrollView(
                                          key: const ValueKey(
                                            'setup-bubbles-narrow',
                                          ),
                                          child: field,
                                        ),
                                ),
                              ),
                            ),
                          ),
                        ),
                        if (_section != null)
                          Positioned.fill(
                            child: ModalBarrier(
                              color: Colors.black.withValues(alpha: .12),
                              dismissible: true,
                              onDismiss: _closeSection,
                              semanticsLabel: 'Close settings popover',
                              barrierSemanticsDismissible: true,
                            ),
                          ),
                        if (_section != null && _modalOrigin != null)
                          BubbleOptionsMorph(
                            key: const ValueKey('setup-options-morph'),
                            origin: _modalOrigin!,
                            target: target,
                            progress: _expansion.value,
                            diameter: diameter,
                            face: _bubbleFace(
                              _section!,
                              _sections[_section]!,
                              diameter,
                            ),
                            child: _popover(),
                          ),
                      ],
                    );
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}

/// Compact, disclosure-safe Converse model and privacy summary.
///
/// Displays the verified model identity (or explicit unavailable/unknown state)
/// and local/cloud qualification on one compact surface alongside the existing
/// Change / Model settings action, while keeping full technical/profile details
/// available on explicit expansion without mutating preferences or invoking
/// provider endpoints.
class ConverseModelSummary extends StatefulWidget {
  const ConverseModelSummary({
    super.key,
    required this.state,
    this.onModelSettings,
    this.onConnectionSetup,
    this.onOpenModelOptions,
    this.onAddModel,
    this.menuController,
  });
  final AppState state;
  final VoidCallback? onModelSettings;
  final VoidCallback? onConnectionSetup;
  final VoidCallback? onOpenModelOptions;
  final VoidCallback? onAddModel;
  final MenuController? menuController;

  @override
  State<ConverseModelSummary> createState() => _ConverseModelSummaryState();
}

class _ConverseModelSummaryState extends State<ConverseModelSummary> {
  bool _expanded = false;
  final _localMenu = MenuController();
  final _modelFavorites = <String>{};
  int? _favoritesGeneration;
  AppState? _favoritesOwner;

  AppState get _s => widget.state;

  Widget _modelPicker() {
    final owner = _s;
    final client = owner.client;
    final generation = owner.connectionGeneration;
    if (!identical(_favoritesOwner, owner) ||
        _favoritesGeneration != generation) {
      _modelFavorites.clear();
      _favoritesOwner = owner;
      _favoritesGeneration = generation;
    }
    bool current() =>
        mounted &&
        identical(widget.state, owner) &&
        client != null &&
        owner.sessionAuthenticated &&
        owner.ownsConnection(generation, client);
    bool ownsConnection() =>
        client != null &&
        owner.sessionAuthenticated &&
        owner.ownsConnection(generation, client);
    return MenuAnchor(
      controller: widget.menuController ?? _localMenu,
      style: MenuStyle(
        backgroundColor: WidgetStatePropertyAll(
          Theme.of(context).colorScheme.surfaceContainerLow,
        ),
        padding: const WidgetStatePropertyAll(EdgeInsets.zero),
        shape: WidgetStatePropertyAll(
          RoundedRectangleBorder(
            side: BorderSide(
              color: Theme.of(context).colorScheme.outlineVariant,
            ),
            borderRadius: BorderRadius.circular(16),
          ),
        ),
      ),
      menuChildren: [
        Builder(
          builder: (context) {
            final size = MediaQuery.sizeOf(context);
            return SizedBox(
              width: math.min(380, math.max(180, size.width - 32)),
              child: ConstrainedBox(
                constraints: BoxConstraints(
                  maxHeight: math.min(550, math.max(150, size.height - 48)),
                ),
                child: OptionsConverseModel(
                  api: current() ? client?.converseModels : null,
                  active: current,
                  compact: true,
                  favorites: _modelFavorites,
                  onInvalidate: () {
                    if (current()) owner.invalidateConverseInference();
                  },
                  onSettled: () async {
                    if (ownsConnection()) {
                      await owner.revalidateConverseInference(
                        invalidate: false,
                      );
                    }
                  },
                  onOpenModelOptions:
                      (widget.onOpenModelOptions ?? widget.onModelSettings) ==
                          null
                      ? null
                      : () {
                          MenuController.maybeOf(context)?.close();
                          (widget.onOpenModelOptions ??
                              widget.onModelSettings)!();
                        },
                  onAddModel: widget.onAddModel == null
                      ? null
                      : () {
                          MenuController.maybeOf(context)?.close();
                          widget.onAddModel!();
                        },
                ),
              ),
            );
          },
        ),
      ],
      builder: (context, controller, child) => TextButton.icon(
        key: const ValueKey('converse-model-summary-settings'),
        onPressed: () =>
            controller.isOpen ? controller.close() : controller.open(),
        icon: const Icon(Icons.expand_more, size: 18),
        label: const Text('Choose model'),
      ),
    );
  }

  String get _modelLabel {
    final model = (_s.converseProvider?['model'] as String?)?.trim();
    if (model != null && model.isNotEmpty) return model;
    if (!_s.connected) return 'Not connected · Conversation model unknown';
    return 'Conversation model unavailable';
  }

  String get _qualificationLabel {
    final info = _s.converseProvider;
    final provider = (info?['provider'] as String?)?.trim();
    final local = info?['local'];
    if (!_s.connected) {
      return 'Disconnected · Open Connection & privacy to connect';
    }
    if (!_s.sessionAuthenticated) {
      return 'Session unauthenticated · Model readiness unconfirmed';
    }
    if (_s.resourceStatusStale || !_s.converseOnline) {
      return 'Converse unavailable · Model readiness unconfirmed';
    }
    if (local is! bool) {
      return 'Model or provider qualification unverified';
    }
    if (local) {
      return provider != null && provider.isNotEmpty
          ? 'Local inference ($provider)'
          : 'Local inference';
    }
    final providerTag = provider != null && provider.isNotEmpty
        ? provider
        : 'cloud';
    if (!_s.converseInferenceAllowed) {
      return 'Cloud provider ($providerTag) · Privacy disclosure required';
    }
    return 'Cloud provider ($providerTag) · See Connection & privacy for disclosure settings.';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final info = _s.converseProvider;
    final provider = (info?['provider'] as String?)?.trim();
    final isLocal = info?['local'] == true;
    final needsAttention =
        !_s.connected ||
        !_s.sessionAuthenticated ||
        _s.resourceStatusStale ||
        !_s.converseOnline ||
        !_s.converseInferenceAllowed ||
        info?['local'] is! bool;
    final profileLabel =
        _s.connection?.backendProfile.label ?? 'Local inference only';

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          alignment: WrapAlignment.spaceBetween,
          crossAxisAlignment: WrapCrossAlignment.center,
          spacing: 12,
          runSpacing: 6,
          children: [
            InkWell(
              onTap: () => (widget.menuController ?? _localMenu).open(),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    needsAttention
                        ? Icons.info_outline_rounded
                        : isLocal
                        ? Icons.memory_rounded
                        : Icons.cloud_outlined,
                    size: 18,
                    color: needsAttention
                        ? scheme.error
                        : scheme.onSurfaceVariant,
                  ),
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      _modelLabel,
                      key: const ValueKey('converse-model-summary-identity'),
                      style: theme.textTheme.titleSmall?.copyWith(
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
              ),
            ),
            Text(
              _qualificationLabel,
              key: const ValueKey('converse-model-summary-qualification'),
              style: theme.textTheme.bodySmall?.copyWith(
                color: needsAttention ? scheme.error : scheme.onSurfaceVariant,
                fontWeight: needsAttention ? FontWeight.w600 : FontWeight.w500,
              ),
            ),
            Wrap(
              spacing: 4,
              runSpacing: 4,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                TextButton.icon(
                  key: const ValueKey('converse-model-summary-toggle'),
                  onPressed: () => setState(() => _expanded = !_expanded),
                  icon: Icon(
                    _expanded ? Icons.expand_less : Icons.expand_more,
                    size: 18,
                  ),
                  label: Text(_expanded ? 'Hide details' : 'Model details'),
                ),
                _modelPicker(),
                if ((!isLocal || needsAttention) &&
                    widget.onConnectionSetup != null)
                  TextButton(
                    key: const ValueKey('converse-model-summary-privacy'),
                    onPressed: widget.onConnectionSetup,
                    child: const Text('Connection & privacy'),
                  ),
              ],
            ),
          ],
        ),
        if (_expanded) ...[
          const SizedBox(height: 8),
          const Divider(height: 1),
          const SizedBox(height: 8),
          Column(
            key: const ValueKey('converse-model-summary-details'),
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'Provider: ${provider != null && provider.isNotEmpty ? provider : 'unavailable'}',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 4),
              Text(
                'Disclosure profile: $profileLabel',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 4),
              Text(
                isLocal
                    ? 'Local inference'
                    : 'See Connection & privacy for disclosure settings.',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 4),
              Text(
                'Text · Microphone via speech-to-text',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 4),
              Text(
                'Open Model settings to inspect all configured roles.',
                style: theme.textTheme.bodySmall,
              ),
            ],
          ),
        ],
      ],
    );
  }
}
