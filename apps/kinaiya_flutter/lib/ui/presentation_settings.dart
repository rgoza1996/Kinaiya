import 'dart:async';

import 'package:flutter/material.dart';

import '../character/pack.dart';
import '../presentation/controller.dart';
import '../presentation/platform.dart';
import 'widgets.dart';
import 'settings_surface.dart';

/// The same local preference owner used by Appearance, exposed in Quick settings.
class InterfaceTransitionsSwitch extends StatefulWidget {
  const InterfaceTransitionsSwitch({
    super.key,
    required this.controller,
    this.showStorageNotice = true,
  });
  final PresentationController controller;
  final bool showStorageNotice;

  @override
  State<InterfaceTransitionsSwitch> createState() =>
      _InterfaceTransitionsSwitchState();
}

class _InterfaceTransitionsSwitchState
    extends State<InterfaceTransitionsSwitch> {
  bool _saving = false;
  int _generation = 0;
  String? _error;

  @override
  void didUpdateWidget(covariant InterfaceTransitionsSwitch oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.controller, widget.controller)) {
      _generation++;
      _saving = false;
      _error = null;
    }
  }

  Future<void> _set(bool enabled) async {
    if (_saving || widget.controller.loading) return;
    final controller = widget.controller;
    final generation = ++_generation;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await controller.setPreferences(
        controller.preferences.copyWith(animateTransitions: enabled),
      );
    } catch (_) {
      if (mounted &&
          generation == _generation &&
          identical(controller, widget.controller)) {
        setState(() => _error = 'Could not save interface transitions.');
      }
    } finally {
      if (mounted &&
          generation == _generation &&
          identical(controller, widget.controller)) {
        setState(() => _saving = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.controller,
    builder: (context, _) {
      final controller = widget.controller;
      final preferences = controller.preferences;
      final reduced =
          preferences.reducedMotion || MediaQuery.disableAnimationsOf(context);
      return Material(
        type: MaterialType.transparency,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SwitchListTile.adaptive(
              contentPadding: EdgeInsets.zero,
              title: const Text('Animations'),
              subtitle: Text(
                reduced
                    ? 'Instant · Reduced motion takes priority'
                    : preferences.animateTransitions
                    ? 'On · Soft fade'
                    : 'Off · Instant',
              ),
              value: preferences.animateTransitions,
              onChanged: _saving || controller.loading ? null : _set,
            ),
            for (final notice in <String>{
              if (widget.showStorageNotice && controller.notice != null)
                controller.notice!,
              if (_error != null) _error!,
            })
              Semantics(liveRegion: true, child: Text(notice)),
          ],
        ),
      );
    },
  );
}

enum PresentationSection { all, appearance, character, accessibility }

/// Local presentation controls. This dialog has no runtime or service access.
class PresentationSettings extends StatefulWidget {
  const PresentationSettings({
    super.key,
    required this.controller,
    this.packagePicker,
    this.packageDownloader,
    this.embedded = false,
    this.section = PresentationSection.all,
  });

  final PresentationController controller;
  final bool embedded;
  final PresentationSection section;

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
        animateTransitions: current.animateTransitions,
        speechMouthMotion: current.speechMouthMotion,
        intelligentUi: current.intelligentUi,
        showCharacter: current.showCharacter,
        showCaptions: current.showCaptions,
        usePackPalette: current.usePackPalette,
        showConverseView: current.showConverseView,
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
    // Action results and controller storage status describe separate outcomes.
    // Neither may hide the other, including when persistence fails after use.
    final notices = <String>{
      if (_notice != null) _notice!,
      if (controller.notice != null) controller.notice!,
    };
    final storageNotice = controller.persistent
        ? 'Presentation settings and imported artwork stay on this device. Browser storage is used when available.'
        : 'Presentation settings last for this session. Persistent storage is unavailable on this platform.';
    return SettingsSurface(
      embedded: widget.embedded,
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
                  if (!widget.embedded)
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
                      for (final notice in notices)
                        Semantics(
                          liveRegion: true,
                          child: Padding(
                            padding: const EdgeInsets.symmetric(vertical: 12),
                            child: Text(notice),
                          ),
                        ),
                      if (widget.section == PresentationSection.all ||
                          widget.section == PresentationSection.character) ...[
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
                                            ? () => setState(
                                                () => _preview = null,
                                              )
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
                      ],
                      if (widget.section == PresentationSection.all ||
                          widget.section == PresentationSection.accessibility ||
                          widget.section == PresentationSection.character) ...[
                        _heading('Character & motion'),
                        SwitchListTile(
                          contentPadding: EdgeInsets.zero,
                          title: const Text('Reduce motion'),
                          subtitle: const Text(
                            'Keep character animation and interface transitions still. Your device’s reduced motion setting always applies.',
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
                          title: const Text('Interface transitions'),
                          subtitle: const Text(
                            'Fade between views and settings panels. Reduced motion always takes priority.',
                          ),
                          value: preferences.animateTransitions,
                          onChanged: enabled
                              ? (value) => _preferences(
                                  preferences.copyWith(
                                    animateTransitions: value,
                                  ),
                                )
                              : null,
                        ),
                        SwitchListTile(
                          contentPadding: EdgeInsets.zero,
                          title: const Text('Speech mouth movement'),
                          subtitle: Text(
                            preferences.reducedMotion || media.disableAnimations
                                ? 'Still · Reduced motion takes priority. Your choice is saved for when motion is enabled.'
                                : 'Move Kinaiya’s mouth with her spoken audio in the default Converse portrait. Turn off to keep her mouth still.',
                          ),
                          value: preferences.speechMouthMotion,
                          onChanged: enabled
                              ? (value) => _preferences(
                                  controller.preferences.copyWith(
                                    speechMouthMotion: value,
                                  ),
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
                      ],
                      if (widget.section == PresentationSection.all ||
                          widget.section == PresentationSection.appearance) ...[
                        _heading('Interface'),
                        SwitchListTile(
                          contentPadding: EdgeInsets.zero,
                          title: const Text('Show Converse page'),
                          subtitle: const Text(
                            'Keep the close-up interaction view in navigation. Home remains available.',
                          ),
                          value: preferences.showConverseView,
                          onChanged: enabled
                              ? (value) => _preferences(
                                  preferences.copyWith(showConverseView: value),
                                )
                              : null,
                        ),
                        InterfaceTransitionsSwitch(
                          controller: controller,
                          showStorageNotice: false,
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
