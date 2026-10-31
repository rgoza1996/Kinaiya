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
  });
  final AgentModelsSnapshot models;
  final String? harness;
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
    final binding = widget.models.effective(widget.harness);
    _inherit =
        widget.harness != null &&
        !widget.models.harnesses.containsKey(widget.harness);
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
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            widget.harness == null ? 'Agent default' : 'Model settings',
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: 8),
          const Text(
            'Applies to new tasks. Running tasks keep their model, reasoning and permissions.',
          ),
          if (widget.harness != null) ...[
            const SizedBox(height: 20),
            RadioGroup<bool>(
              groupValue: _inherit,
              onChanged: widget.busy
                  ? (_) {}
                  : (value) => setState(() {
                      _inherit = value ?? true;
                      if (_inherit) {
                        _profile = widget.models.defaultBinding.profileId;
                        _reasoning = widget.models.defaultBinding.reasoning;
                      }
                    }),
              child: Column(
                children: [
                  RadioListTile<bool>(
                    value: true,
                    title: const Text('Inherit Agent default'),
                    subtitle: const Text(
                      'Future changes to the default also apply to this harness.',
                    ),
                    enabled: !widget.busy,
                    contentPadding: EdgeInsets.zero,
                  ),
                  RadioListTile<bool>(
                    value: false,
                    title: const Text('Use a custom model'),
                    subtitle: const Text(
                      'Choose a configured profile for this harness.',
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
              'harness-model-${widget.harness ?? 'default'}-$_profile-$_inherit',
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
                  enabled: item.selectable,
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
              'harness-reasoning-${widget.harness ?? 'default'}-$_profile-${_reasoning ?? 'profile'}-$_inherit',
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
              key: const ValueKey('harness-model-save'),
              onPressed:
                  widget.busy ||
                      (!_inherit &&
                          (profile?.selectable != true || !validEffort))
                  ? null
                  : () => widget.onSave(
                      _inherit ? null : _profile,
                      _inherit ? null : _reasoning,
                    ),
              icon: const Icon(Icons.save_outlined),
              label: const Text('Save model settings'),
            ),
          ),
        ],
      ),
    );
  }
}
