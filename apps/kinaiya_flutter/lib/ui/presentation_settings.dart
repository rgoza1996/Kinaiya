import 'dart:async';

import 'package:flutter/material.dart';

import '../character/pack.dart';
import '../presentation/controller.dart';
import '../presentation/platform.dart';
import 'widgets.dart';

/// Local presentation controls. This dialog has no runtime or service access.
class PresentationSettings extends StatefulWidget {
  const PresentationSettings({
    super.key,
    required this.controller,
    this.packagePicker,
    this.packageDownloader,
  });

  final PresentationController controller;

  @visibleForTesting
  final Future<String?> Function()? packagePicker;
  @visibleForTesting
  final Future<void> Function(String)? packageDownloader;

  @override
  State<PresentationSettings> createState() => _PresentationSettingsState();
}

class _PresentationSettingsState extends State<PresentationSettings> {
  final _primary = TextEditingController();
  final _background = TextEditingController();
  CharacterSelection? _preview;
  String? _notice, _primaryError, _backgroundError;
  bool _busy = false, _paletteDirty = false;
  int _generation = 0;

  bool get _canImport =>
      widget.packagePicker != null || presentationImportSupported;
  bool get _canExport =>
      widget.packageDownloader != null || presentationImportSupported;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_changed);
    _syncPalette();
  }

  @override
  void didUpdateWidget(covariant PresentationSettings oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.controller, widget.controller)) {
      oldWidget.controller.removeListener(_changed);
      widget.controller.addListener(_changed);
      _generation++;
      _busy = _paletteDirty = false;
      _preview = null;
      _notice = _primaryError = _backgroundError = null;
      _syncPalette();
    }
  }

  void _syncPalette() {
    _primary.text = widget.controller.preferences.primaryOverride ?? '';
    _background.text = widget.controller.preferences.backgroundOverride ?? '';
  }

  void _changed() {
    if (!mounted) return;
    setState(() {
      if (!_paletteDirty) _syncPalette();
    });
  }

  @override
  void dispose() {
    _generation++;
    widget.controller.removeListener(_changed);
    _primary.dispose();
    _background.dispose();
    super.dispose();
  }

  bool _owns(PresentationController controller, int generation) =>
      mounted &&
      identical(controller, widget.controller) &&
      generation == _generation;

  Future<void> _run(
    Future<void> Function(PresentationController, int) action, {
    required String failure,
  }) async {
    if (_busy || widget.controller.loading) return;
    final controller = widget.controller;
    final generation = ++_generation;
    setState(() {
      _busy = true;
      _notice = null;
    });
    try {
      await action(controller, generation).timeout(const Duration(minutes: 2));
    } catch (_) {
      // Files and browser errors are untrusted; never surface their raw text.
      if (_owns(controller, generation)) setState(() => _notice = failure);
    } finally {
      if (_owns(controller, generation)) {
        setState(() {
          _busy = false;
          // Also retire timed-out work before it can publish a late preview.
          _generation++;
        });
      }
    }
  }

  Future<void> _import() => _run(
    (controller, generation) async {
      final raw = await (widget.packagePicker ?? pickCharacterPackage)();
      if (!_owns(controller, generation) || raw == null) return;
      final candidate = await controller.inspectPackage(raw);
      if (!_owns(controller, generation)) return;
      setState(() => _preview = candidate);
    },
    failure:
        'Could not open this character pack. Choose a valid character package.',
  );

  Future<void> _usePreview() {
    final selection = _preview;
    if (selection == null) return Future<void>.value();
    return _run(
      (controller, generation) async {
        await controller.useCharacter(selection);
        if (!_owns(controller, generation)) return;
        setState(() {
          _preview = null;
          _notice = 'Character pack selected.';
        });
      },
      failure:
          'Could not select this character pack. Your current settings are still shown.',
    );
  }

  Future<void> _export() => _run((controller, generation) async {
    final raw = await controller.exportPackage();
    if (!_owns(controller, generation)) return;
    await (widget.packageDownloader ?? downloadCharacterPackage)(raw);
    if (!_owns(controller, generation)) return;
    setState(() => _notice = 'Character pack exported.');
  }, failure: 'Could not export this character pack. Try again.');

  Future<void> _restore() => _run((controller, generation) async {
    await controller.useDefault();
    if (!_owns(controller, generation)) return;
    setState(() {
      _preview = null;
      _notice = 'Default character restored.';
    });
  }, failure: 'Could not restore the default character. Try again.');

  Future<void> _preferences(PresentationPreferences preferences) => _run(
    (controller, generation) async {
      await controller.setPreferences(preferences);
    },
    failure:
        'Could not save presentation settings. Check the settings shown here.',
  );

  void _applyPalette() {
    String? validate(String value) =>
        value.isEmpty || RegExp(r'^#[0-9a-fA-F]{6}$').hasMatch(value)
        ? null
        : 'Use # followed by six color digits.';
    final primary = _primary.text.trim();
    final background = _background.text.trim();
    setState(() {
      _primaryError = validate(primary);
      _backgroundError = validate(background);
    });
    if (_primaryError != null || _backgroundError != null) return;
    final current = widget.controller.preferences;
    _paletteDirty = false;
    _preferences(
      PresentationPreferences(
        reducedMotion: current.reducedMotion,
        showCharacter: current.showCharacter,
        showCaptions: current.showCaptions,
        usePackPalette: current.usePackPalette,
        primaryOverride: primary.isEmpty ? null : primary.toUpperCase(),
        backgroundOverride: background.isEmpty
            ? null
            : background.toUpperCase(),
      ),
    );
  }

  void _clearPalette() {
    setState(() {
      _paletteDirty = false;
      _primaryError = _backgroundError = null;
      _primary.clear();
      _background.clear();
    });
    _preferences(widget.controller.preferences.copyWith(clearOverrides: true));
  }

  Widget _heading(String title) => Padding(
    padding: const EdgeInsets.only(top: 20, bottom: 8),
    child: Semantics(
      header: true,
      child: Text(title, style: Theme.of(context).textTheme.titleMedium),
    ),
  );

  Widget _metadata(CharacterPack pack) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(pack.name, style: Theme.of(context).textTheme.titleMedium),
      Text('Author: ${pack.author.isEmpty ? 'Not specified' : pack.author}'),
      Text(
        'Version: ${pack.packVersion.isEmpty ? 'Not specified' : pack.packVersion}',
      ),
      Text('License: ${pack.license.isEmpty ? 'Not specified' : pack.license}'),
    ],
  );

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    final preferences = controller.preferences;
    final enabled = !_busy && !controller.loading;
    final media = MediaQuery.of(context);
    final preview = _preview;
    final storageNotice = controller.persistent
        ? 'Presentation settings and imported artwork are saved in this browser on this device.'
        : 'Presentation settings last for this session. Persistent storage is unavailable on this platform.';
    return Dialog(
      insetPadding: const EdgeInsets.all(12),
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: 680,
          maxHeight: (media.size.height - media.viewInsets.vertical - 24)
              .clamp(0.0, 800.0)
              .toDouble(),
        ),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: Semantics(
                      header: true,
                      child: Text(
                        'Appearance',
                        style: Theme.of(context).textTheme.headlineSmall,
                      ),
                    ),
                  ),
                  IconButton(
                    tooltip: 'Close appearance settings',
                    onPressed: () => Navigator.of(context).pop(),
                    icon: const Icon(Icons.close),
                  ),
                ],
              ),
              if (_busy || controller.loading)
                const LinearProgressIndicator(
                  semanticsLabel: 'Updating appearance',
                ),
              Flexible(
                child: SingleChildScrollView(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const Text(
                        'Choose your character, colors and presentation preferences.',
                      ),
                      const SizedBox(height: 8),
                      Text(storageNotice),
                      if (_notice != null || controller.notice != null)
                        Semantics(
                          liveRegion: true,
                          child: Padding(
                            padding: const EdgeInsets.symmetric(vertical: 12),
                            child: Text(_notice ?? controller.notice!),
                          ),
                        ),
                      _heading('Current character'),
                      if (controller.selected != null)
                        _metadata(controller.selected!.pack)
                      else
                        const Text('Loading the default character…'),
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          OutlinedButton.icon(
                            onPressed: enabled && _canImport ? _import : null,
                            icon: const Icon(Icons.file_open_outlined),
                            label: const Text('Import character pack'),
                          ),
                          OutlinedButton.icon(
                            onPressed: enabled && _canExport ? _export : null,
                            icon: const Icon(Icons.download_outlined),
                            label: const Text('Export current pack'),
                          ),
                          TextButton(
                            onPressed: enabled ? _restore : null,
                            child: const Text('Restore default character'),
                          ),
                        ],
                      ),
                      if (!_canImport || !_canExport)
                        const Padding(
                          padding: EdgeInsets.only(top: 8),
                          child: Text(
                            'Character pack import and export are available in the web app.',
                          ),
                        ),
                      if (preview != null) ...[
                        _heading('Preview character pack'),
                        Card(
                          child: Padding(
                            padding: const EdgeInsets.all(12),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                Center(
                                  child: MediaQuery(
                                    data: media.copyWith(
                                      disableAnimations:
                                          media.disableAnimations ||
                                          preferences.reducedMotion,
                                    ),
                                    child: MascotWidget(
                                      pack: preview.pack,
                                      bundle: preview.bundle,
                                      mood: 'idle',
                                      size: 160,
                                    ),
                                  ),
                                ),
                                _metadata(preview.pack),
                                const SizedBox(height: 8),
                                const Text(
                                  'Only the artwork and its palette change when you use this pack.',
                                ),
                                const SizedBox(height: 8),
                                Wrap(
                                  spacing: 8,
                                  runSpacing: 8,
                                  children: [
                                    FilledButton(
                                      onPressed: enabled ? _usePreview : null,
                                      child: const Text('Use character pack'),
                                    ),
                                    TextButton(
                                      onPressed: enabled
                                          ? () =>
                                                setState(() => _preview = null)
                                          : null,
                                      child: const Text('Cancel preview'),
                                    ),
                                  ],
                                ),
                              ],
                            ),
                          ),
                        ),
                      ],
                      _heading('Presentation'),
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Reduce motion'),
                        subtitle: const Text(
                          'Keep character animation still. Your device’s reduced motion setting always applies.',
                        ),
                        value: preferences.reducedMotion,
                        onChanged: enabled
                            ? (value) => _preferences(
                                preferences.copyWith(reducedMotion: value),
                              )
                            : null,
                      ),
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Show character'),
                        subtitle: const Text(
                          'Turn off for a simpler view with the same controls.',
                        ),
                        value: preferences.showCharacter,
                        onChanged: enabled
                            ? (value) => _preferences(
                                preferences.copyWith(showCharacter: value),
                              )
                            : null,
                      ),
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Show captions'),
                        subtitle: const Text(
                          'Display spoken conversation text in Character View.',
                        ),
                        value: preferences.showCaptions,
                        onChanged: enabled
                            ? (value) => _preferences(
                                preferences.copyWith(showCaptions: value),
                              )
                            : null,
                      ),
                      _heading('Colors'),
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Use character pack colors'),
                        subtitle: const Text(
                          'Your color overrides take priority.',
                        ),
                        value: preferences.usePackPalette,
                        onChanged: enabled
                            ? (value) => _preferences(
                                preferences.copyWith(usePackPalette: value),
                              )
                            : null,
                      ),
                      const Text(
                        'Leave an override blank to use the character palette or Kinaiya colors. Text contrast adjusts automatically.',
                      ),
                      const SizedBox(height: 16),
                      TextField(
                        controller: _primary,
                        enabled: enabled,
                        maxLength: 7,
                        autocorrect: false,
                        textCapitalization: TextCapitalization.characters,
                        decoration: InputDecoration(
                          labelText: 'Primary color override',
                          helperText: '#RRGGBB, for example #141091',
                          helperMaxLines: 3,
                          errorMaxLines: 3,
                          errorText: _primaryError,
                          counterText: '',
                        ),
                        onChanged: (_) => _paletteDirty = true,
                        onSubmitted: enabled ? (_) => _applyPalette() : null,
                      ),
                      const SizedBox(height: 16),
                      TextField(
                        controller: _background,
                        enabled: enabled,
                        maxLength: 7,
                        autocorrect: false,
                        textCapitalization: TextCapitalization.characters,
                        decoration: InputDecoration(
                          labelText: 'Background color override',
                          helperText: '#RRGGBB, for example #FFFFFF',
                          helperMaxLines: 3,
                          errorMaxLines: 3,
                          errorText: _backgroundError,
                          counterText: '',
                        ),
                        onChanged: (_) => _paletteDirty = true,
                        onSubmitted: enabled ? (_) => _applyPalette() : null,
                      ),
                      const SizedBox(height: 12),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          FilledButton(
                            onPressed: enabled ? _applyPalette : null,
                            child: const Text('Apply color overrides'),
                          ),
                          TextButton(
                            onPressed: enabled ? _clearPalette : null,
                            child: const Text('Clear color overrides'),
                          ),
                        ],
                      ),
                      const SizedBox(height: 12),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
