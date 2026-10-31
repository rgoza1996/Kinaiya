import 'package:flutter/material.dart';

import '../api/agent_model_settings.dart';
import 'design_tokens.dart';

class OptionsModelEditor extends StatefulWidget {
  const OptionsModelEditor({
    super.key,
    required this.models,
    required this.onSave,
    required this.busy,
    this.harness,
    this.auxiliaryTask,
  });
  final AgentModelsSnapshot models;
  final String? harness;
  final String? auxiliaryTask;
  final bool busy;
  final Future<void> Function(String? profile, String? reasoning) onSave;
  @override
  State<OptionsModelEditor> createState() => _OptionsModelEditorState();
}

class _OptionsModelEditorState extends State<OptionsModelEditor> {
  late bool _inherit;
  late String _profile;
  String? _reasoning;

  void _load() {
    final binding = widget.models.effective(
      widget.harness,
      widget.auxiliaryTask,
    );
    _inherit =
        (widget.harness != null || widget.auxiliaryTask != null) &&
        !widget.models.overrides(widget.harness, widget.auxiliaryTask);
    _profile = binding.profileId;
    _reasoning = binding.reasoning;
  }

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant OptionsModelEditor oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.harness != widget.harness ||
        oldWidget.auxiliaryTask != widget.auxiliaryTask ||
        oldWidget.models.revision != widget.models.revision) {
      _load();
    }
  }

  @override
  Widget build(BuildContext context) {
    final profiles = widget.models.profiles;
    final profile = widget.models.profile(_profile);
    final editable = !widget.busy && !_inherit;
    final levels = profile?.reasoningOptions ?? const <String>[];
    final validEffort = _reasoning == null || levels.contains(_reasoning);
    return DesignPanel(
      child: Material(
        type: MaterialType.transparency,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              widget.auxiliaryTask == null
                  ? (widget.harness == null
                        ? 'Agent default'
                        : 'Model settings')
                  : widget.models.auxiliaryTasks[widget.auxiliaryTask] ??
                        'Auxiliary model',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 8),
            const Text(
              'Applies to new tasks. Running tasks keep their model, reasoning and permissions.',
            ),
            if (widget.harness != null || widget.auxiliaryTask != null) ...[
              const SizedBox(height: 20),
              RadioGroup<bool>(
                groupValue: _inherit,
                onChanged: widget.busy
                    ? (_) {}
                    : (value) => setState(() {
                        _inherit = value ?? true;
                        if (_inherit) {
                          final inherited = widget.models.inherited(
                            widget.harness,
                            widget.auxiliaryTask,
                          );
                          _profile = inherited.profileId;
                          _reasoning = inherited.reasoning;
                        }
                      }),
                child: Column(
                  children: [
                    RadioListTile<bool>(
                      value: true,
                      title: Text(
                        widget.auxiliaryTask == null
                            ? 'Inherit Agent default'
                            : 'Use inherited model',
                      ),
                      subtitle: Text(
                        widget.auxiliaryTask == null
                            ? 'Future changes to the default also apply to this harness.'
                            : widget.harness == null
                            ? 'Use the main Agent model.'
                            : 'Use the task default, or this harness’s model when no task default is set.',
                      ),
                      enabled: !widget.busy,
                      contentPadding: EdgeInsets.zero,
                    ),
                    RadioListTile<bool>(
                      value: false,
                      title: const Text('Use a custom model'),
                      subtitle: Text(
                        widget.auxiliaryTask == null
                            ? 'Choose a configured profile for this harness.'
                            : 'Choose a configured profile for this task.',
                      ),
                      enabled: !widget.busy,
                      contentPadding: EdgeInsets.zero,
                    ),
                  ],
                ),
              ),
            ],
            const SizedBox(height: 24),
            DropdownButtonFormField<String>(
              key: ValueKey(
                'harness-model-${widget.harness ?? 'default'}-${widget.auxiliaryTask ?? 'main'}-$_profile-$_inherit',
              ),
              initialValue: _profile,
              isExpanded: true,
              decoration: const InputDecoration(
                labelText: 'Model profile',
                border: OutlineInputBorder(),
              ),
              items: [
                if (profile == null)
                  DropdownMenuItem(
                    value: _profile,
                    child: Text('Unavailable · $_profile'),
                  ),
                for (final item in profiles)
                  DropdownMenuItem(
                    value: item.id,
                    enabled: widget.auxiliaryTask == null
                        ? item.selectable
                        : item.auxiliarySelectable,
                    child: Text(
                      '${item.label}${item.model != null ? ' · ${item.model}' : ' · Unavailable'}',
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
              ],
              onChanged: editable
                  ? (value) {
                      if (value != null) {
                        setState(() {
                          _profile = value;
                          _reasoning = null;
                        });
                      }
                    }
                  : null,
            ),
            const SizedBox(height: 20),
            InputDecorator(
              decoration: const InputDecoration(
                labelText: 'Provider',
                border: OutlineInputBorder(),
              ),
              child: Text(
                profile?.provider == null
                    ? 'Unavailable'
                    : '${profile!.provider} · ${profile.local == true ? 'Local' : 'Cloud'}',
              ),
            ),
            const SizedBox(height: 20),
            DropdownButtonFormField<String>(
              key: ValueKey(
                'harness-reasoning-${widget.harness ?? 'default'}-${widget.auxiliaryTask ?? 'main'}-$_profile-${_reasoning ?? 'profile'}-$_inherit',
              ),
              initialValue: _reasoning ?? 'profile',
              isExpanded: true,
              decoration: const InputDecoration(
                labelText: 'Reasoning level',
                border: OutlineInputBorder(),
              ),
              items: [
                DropdownMenuItem(
                  value: 'profile',
                  child: Text(
                    'Profile default · ${profile?.reasoning ?? 'Not specified'}',
                  ),
                ),
                if (!validEffort)
                  DropdownMenuItem(
                    value: _reasoning,
                    child: Text('Unavailable · $_reasoning'),
                  ),
                for (final level in levels)
                  DropdownMenuItem(value: level, child: Text(level)),
              ],
              onChanged: editable && (levels.isNotEmpty || _reasoning != null)
                  ? (value) => setState(
                      () => _reasoning = value == 'profile' ? null : value,
                    )
                  : null,
            ),
            if (levels.isEmpty)
              const Padding(
                padding: EdgeInsets.only(top: 8),
                child: Text(
                  'This profile has no additional reasoning levels declared by the host.',
                ),
              ),
            if (widget.auxiliaryTask != null &&
                profile?.auxiliarySelectable != true)
              const Padding(
                padding: EdgeInsets.only(top: 12),
                child: Text(
                  'This profile cannot serve auxiliary text requests. Choose an available profile.',
                ),
              ),
            if (profile?.local == false)
              const Padding(
                padding: EdgeInsets.only(top: 20),
                child: Text(
                  'Cloud profile. Your existing input disclosure rules still apply to every task.',
                ),
              ),
            const SizedBox(height: 24),
            Align(
              alignment: Alignment.centerLeft,
              child: FilledButton.icon(
                key: ValueKey(
                  widget.auxiliaryTask == null
                      ? 'harness-model-save'
                      : 'auxiliary-model-save',
                ),
                onPressed:
                    widget.busy ||
                        (!_inherit &&
                            ((widget.auxiliaryTask == null
                                        ? profile?.selectable
                                        : profile?.auxiliarySelectable) !=
                                    true ||
                                !validEffort))
                    ? null
                    : () => widget.onSave(
                        _inherit ? null : _profile,
                        _inherit ? null : _reasoning,
                      ),
                icon: const Icon(Icons.save_outlined),
                label: Text(
                  widget.auxiliaryTask == null
                      ? 'Save model settings'
                      : 'Save auxiliary model',
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class OptionsAuxiliaryModels extends StatefulWidget {
  const OptionsAuxiliaryModels({
    super.key,
    required this.models,
    required this.busy,
    required this.onSave,
    this.harness,
  });
  final AgentModelsSnapshot models;
  final bool busy;
  final String? harness;
  final Future<void> Function(
    String purpose,
    String? profile,
    String? reasoning,
  )
  onSave;
  @override
  State<OptionsAuxiliaryModels> createState() => _OptionsAuxiliaryModelsState();
}

class _OptionsAuxiliaryModelsState extends State<OptionsAuxiliaryModels> {
  String? _purpose;
  @override
  Widget build(BuildContext context) {
    final tasks = widget.models.auxiliaryTasks;
    if (tasks.isEmpty) return const SizedBox.shrink();
    final purpose = tasks.containsKey(_purpose) ? _purpose! : tasks.keys.first;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const SizedBox(height: 24),
        Text('Auxiliary models', style: Theme.of(context).textTheme.titleLarge),
        const SizedBox(height: 8),
        const Text(
          'Choose a text model for each task. Each harness can override these choices.',
        ),
        const SizedBox(height: 16),
        DropdownButtonFormField<String>(
          key: ValueKey('auxiliary-task-$purpose'),
          initialValue: purpose,
          isExpanded: true,
          decoration: const InputDecoration(
            labelText: 'Task',
            border: OutlineInputBorder(),
          ),
          items: [
            for (final entry in tasks.entries)
              DropdownMenuItem(
                value: entry.key,
                child: Text(entry.value, overflow: TextOverflow.ellipsis),
              ),
          ],
          onChanged: widget.busy
              ? null
              : (value) => setState(() => _purpose = value),
        ),
        const SizedBox(height: 16),
        OptionsModelEditor(
          key: ValueKey('auxiliary-editor-${widget.harness}-$purpose'),
          models: widget.models,
          harness: widget.harness,
          auxiliaryTask: purpose,
          busy: widget.busy,
          onSave: (profile, reasoning) =>
              widget.onSave(purpose, profile, reasoning),
        ),
      ],
    );
  }
}
