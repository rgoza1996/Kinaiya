import 'package:flutter/material.dart';

import '../api/converse_setup.dart';
import '../presentation/controller.dart';
import '../state/app_state.dart';

/// Read-only host configuration and a separately saved local view preference.
/// Opening this surface never loads a model, captures audio or changes consent.
class ModelSetupDialog extends StatefulWidget {
  const ModelSetupDialog({
    super.key,
    required this.state,
    required this.presentation,
  });
  final AppState state;
  final PresentationController presentation;
  @override
  State<ModelSetupDialog> createState() => _ModelSetupDialogState();
}

class _ModelSetupDialogState extends State<ModelSetupDialog> {
  ConverseSetup? _setup;
  bool _loading = false, _saving = false;
  late bool _showConverse;
  String? _error;
  int _request = 0, _connection = -1;

  @override
  void initState() {
    super.initState();
    _showConverse = widget.presentation.preferences.showConverseView;
    widget.state.addListener(_changed);
    _refresh();
  }

  void _changed() {
    if (_connection != widget.state.connectionGeneration) _refresh();
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
      _showConverse = widget.presentation.preferences.showConverseView;
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
    await widget.presentation.setPreferences(
      widget.presentation.preferences.copyWith(showConverseView: _showConverse),
    );
    if (mounted) Navigator.of(context).pop();
  }

  @override
  void dispose() {
    _request++;
    widget.state.removeListener(_changed);
    super.dispose();
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
            decoration: InputDecoration(
              suffixIcon: const Icon(Icons.lock_outline, size: 18),
              enabled: false,
              contentPadding: const EdgeInsets.all(14),
            ),
            child: Text(
              role?.label ?? 'Not declared by this host',
              key: ValueKey('model-$key'),
            ),
          ),
          const SizedBox(height: 6),
          Text(
            role == null ? 'Host configuration unavailable.' : role.reason,
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final reasoning = _setup?.reasoning;
    return Dialog(
      key: const ValueKey('model-setup-dialog'),
      alignment: Alignment.centerRight,
      insetPadding: const EdgeInsets.all(16),
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: 490,
          maxHeight:
              (MediaQuery.sizeOf(context).height -
                      MediaQuery.viewInsetsOf(context).bottom -
                      32)
                  .clamp(0, double.infinity),
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(24, 20, 8, 12),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Kinaiya setup',
                            style: Theme.of(context).textTheme.headlineSmall,
                          ),
                          const Text('Models and conversation preferences'),
                        ],
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
              ),
              const Divider(height: 1),
              Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    if (_loading) const LinearProgressIndicator(),
                    if (_error != null)
                      Text(
                        _error!,
                        key: const ValueKey('model-setup-unavailable'),
                      ),
                    Align(
                      alignment: Alignment.centerRight,
                      child: TextButton.icon(
                        onPressed: _loading ? null : _refresh,
                        icon: const Icon(Icons.refresh),
                        label: const Text('Refresh host configuration'),
                      ),
                    ),
                    _role('conversation', 'Conversation model'),
                    Text(
                      'Accepted inputs',
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 12,
                      children: [
                        for (final modality in [
                          'Text',
                          'Image',
                          'Video',
                          'Audio',
                        ])
                          FilterChip(
                            label: Text(modality),
                            selected: _setup != null && modality == 'Text',
                            onSelected: null,
                          ),
                      ],
                    ),
                    const Text(
                      'This host supports text input. Microphone audio uses speech-to-text; direct audio, images and video are unavailable.',
                    ),
                    const SizedBox(height: 16),
                    Text(
                      'Reasoning level',
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        for (final level in ['Auto', 'Low', 'Medium', 'High'])
                          ChoiceChip(
                            label: Text(level),
                            selected: reasoning == level.toLowerCase(),
                            onSelected: null,
                          ),
                      ],
                    ),
                    const SizedBox(height: 6),
                    Text(
                      reasoning == null || reasoning == 'unavailable'
                          ? 'Reasoning configuration is unavailable.'
                          : 'Configured: ${reasoning == 'none' ? 'off' : reasoning}. Reasoning changes are managed on the host.',
                    ),
                    const SizedBox(height: 20),
                    _role('stt', 'Speech-to-text'),
                    const SwitchListTile.adaptive(
                      contentPadding: EdgeInsets.zero,
                      value: true,
                      onChanged: null,
                      title: Text('Transcribe microphone audio'),
                      subtitle: Text(
                        'Required by this host’s text conversation pipeline.',
                      ),
                    ),
                    const SizedBox(height: 12),
                    _role('tts', 'Text-to-speech'),
                    _role('decisions', 'Decisions model'),
                    const Divider(),
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            'Converse view',
                            style: Theme.of(context).textTheme.titleSmall,
                          ),
                        ),
                        Switch.adaptive(
                          key: const ValueKey('show-converse-view'),
                          value: _showConverse,
                          onChanged: _saving
                              ? null
                              : (value) =>
                                    setState(() => _showConverse = value),
                        ),
                      ],
                    ),
                    const Text(
                      'Optional mascot interaction · Open with the waveform icon. Home voice stays available when hidden.',
                    ),
                    const SizedBox(height: 12),
                    DecoratedBox(
                      decoration: BoxDecoration(
                        color: colors.surfaceContainer,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: const Padding(
                        padding: EdgeInsets.all(12),
                        child: Text(
                          'Save updates the Converse view on this device. Model choices are configured on the host; switching them here is not supported.',
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              const Divider(height: 1),
              Padding(
                padding: const EdgeInsets.all(16),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    TextButton(
                      onPressed: _saving
                          ? null
                          : () => Navigator.of(context).pop(),
                      child: const Text('Cancel'),
                    ),
                    const SizedBox(width: 12),
                    Flexible(
                      child: FilledButton(
                        key: const ValueKey('save-model-setup'),
                        onPressed: _saving ? null : _save,
                        child: Text(_saving ? 'Saving…' : 'Save setup'),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
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
