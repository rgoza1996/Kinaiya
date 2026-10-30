import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/converse_setup.dart';
import '../presentation/controller.dart';
import '../state/app_state.dart';
import 'floating_bubbles.dart';
import 'motion_transition.dart';

/// Read-only host configuration and a separately saved local view preference.
/// Opening this surface never loads a model, captures audio or changes consent.
class ModelSetupDialog extends StatefulWidget {
  const ModelSetupDialog({
    super.key,
    required this.state,
    required this.presentation,
    this.voiceControls,
  });
  final AppState state;
  final PresentationController presentation;
  final Widget? voiceControls;
  @override
  State<ModelSetupDialog> createState() => _ModelSetupDialogState();
}

class _ModelSetupDialogState extends State<ModelSetupDialog> {
  ConverseSetup? _setup;
  bool _loading = false, _saving = false;
  late bool _showConverse;
  String? _error, _saveNotice;
  int _request = 0, _connection = -1;

  @override
  void initState() {
    super.initState();
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
    if (mounted) setState(() {});
  }

  void _interactionChanged() {
    if (mounted) setState(() {});
  }

  final _hovered = <String>{};
  final _pressed = <int>{};
  Size? _bubbleLayoutSize;
  double? _bubbleDiameter;

  void _hover(String section, bool value) {
    setState(() {
      if (value) {
        _hovered.add(section);
      } else {
        _hovered.remove(section);
      }
    });
  }

  bool get _pauseBubbles =>
      _section != null ||
      _hovered.isNotEmpty ||
      _pressed.isNotEmpty ||
      _bubbleFocus.values.any((node) => node.hasFocus);

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
        _error =
            'Model configuration is unavailable from this host. No model settings were changed.';
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
  void dispose() {
    _request++;
    widget.state.removeListener(_changed);
    widget.presentation.removeListener(_presentationChanged);
    for (final node in _bubbleFocus.values) {
      node.removeListener(_interactionChanged);
      node.dispose();
    }
    super.dispose();
  }

  String? _section;
  String _popoverAnchor = 'Model';
  final _popoverKey = GlobalKey();
  final Map<String, FocusNode> _bubbleFocus = {
    for (final section in [
      'Model',
      'Reasoning',
      'Inputs',
      'Speech',
      'Decisions',
    ])
      section: FocusNode(),
  };

  void _openSection(String section) {
    setState(() {
      _section = section;
      _popoverAnchor = section;
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final target = _popoverKey.currentContext;
      if (mounted && target != null) {
        Scrollable.ensureVisible(target, alignment: 0);
      }
    });
  }

  void _closeSection() {
    final node = _bubbleFocus[_section];
    setState(() => _section = null);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) node?.requestFocus();
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

  Widget _bubble(String section, IconData icon, double diameter) {
    final colors = Theme.of(context).colorScheme;
    final active = _section == section;
    return MouseRegion(
      onEnter: (_) => _hover(section, true),
      onExit: (_) => _hover(section, false),
      child: Listener(
        onPointerDown: (event) => setState(() => _pressed.add(event.pointer)),
        onPointerUp: (event) => setState(() => _pressed.remove(event.pointer)),
        onPointerCancel: (event) =>
            setState(() => _pressed.remove(event.pointer)),
        child: Semantics(
          label: '$section settings',
          value: _value(section),
          button: true,
          child: Tooltip(
            message: 'Inspect $section settings',
            child: Material(
              key: ValueKey('setup-bubble-${section.toLowerCase()}'),
              shape: CircleBorder(
                side: BorderSide(
                  color: active ? colors.primary : colors.surface,
                  width: 3,
                ),
              ),
              elevation: active ? 10 : 4,
              color: colors.surface.withValues(alpha: .94),
              clipBehavior: Clip.antiAlias,
              child: InkWell(
                customBorder: const CircleBorder(),
                focusNode: _bubbleFocus[section],
                canRequestFocus: _section == null,
                onTap: () => _openSection(section),
                child: Container(
                  width: diameter,
                  height: diameter,
                  padding: const EdgeInsets.all(17),
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
                  child: ExcludeSemantics(
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Icon(icon, color: colors.primary, size: 28),
                        const SizedBox(height: 6),
                        Text(
                          section,
                          style: Theme.of(context).textTheme.titleMedium,
                          textAlign: TextAlign.center,
                        ),
                        const SizedBox(height: 4),
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
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _sectionContent() => switch (_section) {
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
                        autofocus: true,
                        onPressed: _closeSection,
                        icon: const Icon(Icons.close),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  _sectionContent(),
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

  Widget _popoverSlot() => UiFadeTransition(
    key: _popoverKey,
    enabled:
        widget.presentation.preferences.animateTransitions &&
        !widget.presentation.preferences.reducedMotion,
    transitionKey: _section ?? 'closed',
    child: _section == null ? const SizedBox.shrink() : _popover(),
  );

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

  @override
  Widget build(BuildContext context) => Dialog(
    key: const ValueKey('model-setup-dialog'),
    backgroundColor: Colors.transparent,
    shadowColor: Colors.transparent,
    insetPadding: const EdgeInsets.all(16),
    child: Column(
      children: [
        if (widget.voiceControls != null) widget.voiceControls!,
        Expanded(
          child: LayoutBuilder(
            builder: (context, box) {
              final scale = MediaQuery.textScalerOf(context).scale(1);
              final wide =
                  box.maxWidth / scale >= 1050 && box.maxHeight / scale >= 650;
              const sections = <String, IconData>{
                'Model': Icons.settings_outlined,
                'Reasoning': Icons.auto_awesome_outlined,
                'Inputs': Icons.article_outlined,
                'Speech': Icons.record_voice_over_outlined,
                'Decisions': Icons.diamond_outlined,
              };
              final diameter = wide
                  ? 178 * scale
                  : (160 * scale).clamp(0.0, box.maxWidth - 32);
              if (_bubbleLayoutSize != box.biggest ||
                  _bubbleDiameter != diameter) {
                // MouseRegion does not send onExit when a region unmounts.
                // A responsive replacement must not retain its hover latch.
                _hovered.clear();
                _pressed.clear();
                _bubbleLayoutSize = box.biggest;
                _bubbleDiameter = diameter;
              }
              final bubbles = {
                for (final item in sections.entries)
                  item.key: _bubble(item.key, item.value, diameter),
              };
              final animate =
                  widget.presentation.preferences.animateTransitions &&
                  !widget.presentation.preferences.reducedMotion;
              Widget flow() => Wrap(
                alignment: WrapAlignment.center,
                spacing: 16,
                runSpacing: 16,
                children: bubbles.values.toList(),
              );
              final header = Material(
                color: Theme.of(
                  context,
                ).colorScheme.surface.withValues(alpha: .96),
                borderRadius: BorderRadius.circular(16),
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Row(
                        children: [
                          Expanded(
                            child: Text(
                              'Kinaiya setup',
                              style: Theme.of(context).textTheme.titleLarge,
                            ),
                          ),
                          IconButton(
                            tooltip: 'Close setup',
                            onPressed: _saving
                                ? null
                                : () => Navigator.of(context).pop(),
                            icon: const Icon(Icons.close),
                          ),
                        ],
                      ),
                      if (_loading) const LinearProgressIndicator(),
                      if (_error != null)
                        Text(
                          _error!,
                          key: const ValueKey('model-setup-unavailable'),
                        ),
                      TextButton.icon(
                        onPressed: _loading ? null : _refresh,
                        icon: const Icon(Icons.refresh),
                        label: const Text('Refresh host configuration'),
                      ),
                    ],
                  ),
                ),
              );
              if (!wide) {
                return SingleChildScrollView(
                  key: const ValueKey('setup-bubbles-narrow'),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      header,
                      const SizedBox(height: 16),
                      SizedBox(
                        height:
                            (5 /
                                        (box.maxWidth / (diameter + 32))
                                            .floor()
                                            .clamp(1, 5))
                                    .ceil() *
                                (diameter + 32) +
                            32,
                        child: FocusTraversalGroup(
                          policy: OrderedTraversalPolicy(),
                          child: FloatingBubbles(
                            key: const ValueKey('setup-floating-narrow'),
                            children: bubbles,
                            diameter: diameter,
                            enabled: animate,
                            paused: _pauseBubbles,
                            fallback: flow(),
                          ),
                        ),
                      ),
                      Padding(
                        padding: const EdgeInsets.only(top: 16),
                        child: _popoverSlot(),
                      ),
                      const SizedBox(height: 16),
                      _preferences(),
                    ],
                  ),
                );
              }
              return Stack(
                key: const ValueKey('setup-bubbles-wide'),
                children: [
                  Positioned.fill(
                    child: FocusTraversalGroup(
                      policy: OrderedTraversalPolicy(),
                      child: FloatingBubbles(
                        key: const ValueKey('setup-floating-wide'),
                        children: bubbles,
                        diameter: diameter,
                        enabled: animate,
                        paused: _pauseBubbles,
                        obstacles: [
                          Rect.fromLTWH(0, 0, 280 * scale, 236 * scale),
                          Rect.fromLTWH(
                            0,
                            box.maxHeight - 300 * scale,
                            440 * scale,
                            300 * scale,
                          ),
                          // Keep the character visible between the controls.
                          Rect.fromLTWH(
                            box.maxWidth / 2 - 130 * scale,
                            box.maxHeight * .3,
                            260 * scale,
                            box.maxHeight * .45,
                          ),
                        ],
                        fallback: Padding(
                          padding: EdgeInsets.only(left: 440 * scale),
                          child: SingleChildScrollView(child: flow()),
                        ),
                      ),
                    ),
                  ),
                  Positioned(
                    top: 0,
                    left: 0,
                    width: 260 * scale,
                    height: 220 * scale,
                    child: Align(
                      alignment: Alignment.topLeft,
                      child: SingleChildScrollView(
                        key: const ValueKey('setup-fixed-header'),
                        child: header,
                      ),
                    ),
                  ),
                  Positioned(
                    left: 0,
                    bottom: 0,
                    width: 420 * scale,
                    height: 300 * scale,
                    child: Align(
                      alignment: Alignment.bottomLeft,
                      child: SingleChildScrollView(
                        key: const ValueKey('setup-fixed-preferences'),
                        child: _preferences(),
                      ),
                    ),
                  ),
                  Positioned(
                    top: 140,
                    bottom: 80,
                    left: const {'Model', 'Reasoning'}.contains(_popoverAnchor)
                        ? 0
                        : box.maxWidth - 420,
                    width: 420,
                    child: Align(
                      alignment: Alignment.topCenter,
                      child: _popoverSlot(),
                    ),
                  ),
                ],
              );
            },
          ),
        ),
      ],
    ),
  );
}

class ConverseModelSummary extends StatelessWidget {
  const ConverseModelSummary({super.key, required this.state});
  final AppState state;
  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(
        state.converseProvider?['model'] as String? ??
            'Conversation model unavailable',
        style: Theme.of(context).textTheme.titleSmall,
      ),
      const SizedBox(height: 12),
      Text(
        state.converseProvider?['local'] == true
            ? 'Local inference'
            : 'See Connection & privacy for disclosure settings.',
      ),
      const SizedBox(height: 8),
      const Text('Text · Microphone via speech-to-text'),
      const SizedBox(height: 8),
      const Text('Open Model settings to inspect all configured roles.'),
    ],
  );
}
