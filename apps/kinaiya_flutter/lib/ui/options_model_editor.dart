import 'package:flutter/material.dart';

import '../api/agent_model_settings.dart';
import 'design_tokens.dart';

class OptionsModelEditor extends StatefulWidget {
  const OptionsModelEditor({
    super.key,
    required this.models,
    required this.onSave,
    required this.busy,
    this.onSaveCatalog,
    this.harness,
    this.auxiliaryTask,
  });
  final AgentModelsSnapshot models;
  final String? harness;
  final String? auxiliaryTask;
  final bool busy;
  final Future<void> Function(String? profile, String? reasoning) onSave;
  final Future<void> Function(AgentCatalogBinding catalog, String? reasoning)?
  onSaveCatalog;
  @override
  State<OptionsModelEditor> createState() => _OptionsModelEditorState();
}

class _OptionsModelEditorState extends State<OptionsModelEditor> {
  late bool _inherit;
  late AgentModelReference _profile;
  String? _reasoning;

  void _load() {
    final binding = widget.models.effective(
      widget.harness,
      widget.auxiliaryTask,
    );
    _inherit =
        (widget.harness != null || widget.auxiliaryTask != null) &&
        !widget.models.overrides(widget.harness, widget.auxiliaryTask);
    _profile = binding.reference;
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
    final profiles = widget.models.choices;
    final profile = widget.models.profileFor(_profile);
    final catalog = _profile.catalogBinding;
    final latest = catalog == null
        ? null
        : widget.models.currentCatalogProfile(catalog.id);
    final editable = !widget.busy && !_inherit;
    final selectionAllowed = widget.auxiliaryTask == null
        ? profile?.selectable == true
        : profile?.auxiliarySelectable == true;
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
                          _profile = inherited.reference;
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
            if (widget.models.catalogError != null) ...[
              Text(widget.models.catalogError!),
              const SizedBox(height: 12),
            ],
            DropdownButtonFormField<AgentModelReference>(
              key: catalog == null
                  ? ValueKey<String>(
                      'harness-model-${widget.harness ?? 'default'}-${widget.auxiliaryTask ?? 'main'}-${_profile.profileId}-$_inherit',
                    )
                  : ValueKey((
                      'catalog-model',
                      widget.harness,
                      widget.auxiliaryTask,
                      _profile,
                      _inherit,
                    )),
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
                    enabled: false,
                    child: Text(
                      catalog == null
                          ? 'Unavailable · ${_profile.profileId}'
                          : 'Unavailable · ${catalog.id} · Generation ${catalog.generation}',
                    ),
                  ),
                for (final item in profiles)
                  DropdownMenuItem(
                    value: item.reference,
                    enabled: widget.auxiliaryTask == null
                        ? item.selectable
                        : item.auxiliarySelectable,
                    child: Text(
                      '${item.catalogBinding == null ? item.label : 'Saved · ${item.label} · Generation ${item.catalogBinding!.generation}'}'
                      '${item.model != null ? ' · ${item.model}' : ' · Unavailable'}'
                      '${item.model != null && (widget.auxiliaryTask == null ? !item.selectable : !item.auxiliarySelectable) ? ' · Unavailable' : ''}',
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
                labelText: 'Configuration source',
                border: OutlineInputBorder(),
              ),
              child: Text(
                profile?.sourceLabel ??
                    (catalog == null
                        ? 'Host configuration · Unavailable'
                        : 'Provider catalog · Generation ${catalog.generation} · Unavailable'),
              ),
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
            if (catalog != null) ...[
              const SizedBox(height: 12),
              const Text(
                'This selection uses one saved provider generation. Model readiness and quality have not been checked.',
              ),
              if (!selectionAllowed && profile?.reason != null)
                Text(profile!.reason!),
              if (profile == null)
                Text(
                  widget.models.catalogError != null
                      ? 'The catalog could not be inspected. This saved selection is kept. Refresh before choosing a saved provider.'
                      : latest == null
                      ? 'This exact provider generation is no longer listed. Choose another profile to change future tasks.'
                      : 'This selection stays on generation ${catalog.generation}. The catalog now lists generation ${latest.catalogBinding!.generation}. '
                            'Choose it explicitly to change future tasks.',
                ),
              ExpansionTile(
                tilePadding: EdgeInsets.zero,
                title: const Text('Saved provider identity'),
                children: [
                  Align(
                    alignment: Alignment.centerLeft,
                    child: SelectableText(
                      'Provider ${catalog.id}\nGeneration ${catalog.generation}\nConfiguration SHA-256 ${catalog.configurationSha256}',
                    ),
                  ),
                ],
              ),
            ],
            const SizedBox(height: 20),
            DropdownButtonFormField<String>(
              key: catalog == null
                  ? ValueKey<String>(
                      'harness-reasoning-${widget.harness ?? 'default'}-${widget.auxiliaryTask ?? 'main'}-${_profile.profileId}-${_reasoning ?? 'profile'}-$_inherit',
                    )
                  : ValueKey((
                      'catalog-reasoning',
                      widget.harness,
                      widget.auxiliaryTask,
                      _profile,
                      _reasoning,
                      _inherit,
                    )),
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
                  'This profile has no additional reasoning levels declared.',
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
                            (!selectionAllowed ||
                                !validEffort ||
                                (catalog != null &&
                                    widget.onSaveCatalog == null)))
                    ? null
                    : () => _inherit
                          ? widget.onSave(null, null)
                          : catalog == null
                          ? widget.onSave(_profile.profileId!, _reasoning)
                          : widget.onSaveCatalog!(catalog, _reasoning),
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
    this.onSaveCatalog,
    this.harness,
    this.initialPurpose,
    this.onPurposeChanged,
  });
  final AgentModelsSnapshot models;
  final bool busy;
  final String? harness;
  final String? initialPurpose;
  final ValueChanged<String>? onPurposeChanged;
  final Future<void> Function(
    String purpose,
    String? profile,
    String? reasoning,
  )
  onSave;
  final Future<void> Function(
    String purpose,
    AgentCatalogBinding catalog,
    String? reasoning,
  )?
  onSaveCatalog;
  @override
  State<OptionsAuxiliaryModels> createState() => _OptionsAuxiliaryModelsState();
}

class _OptionsAuxiliaryModelsState extends State<OptionsAuxiliaryModels> {
  String? _purpose;
  @override
  Widget build(BuildContext context) {
    final tasks = widget.models.auxiliaryTasks;
    if (tasks.isEmpty) return const SizedBox.shrink();
    final selected = _purpose ?? widget.initialPurpose;
    final purpose = tasks.containsKey(selected) ? selected! : tasks.keys.first;
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
              : (value) {
                  if (value == null) return;
                  setState(() => _purpose = value);
                  widget.onPurposeChanged?.call(value);
                },
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
          onSaveCatalog: widget.onSaveCatalog == null
              ? null
              : (catalog, reasoning) =>
                    widget.onSaveCatalog!(purpose, catalog, reasoning),
        ),
      ],
    );
  }
}
