import 'dart:async';

import 'package:flutter/material.dart';

import '../api/capture_modes.dart';
import '../api/capture_modes_client.dart';
import '../state/capture_mode_writes.dart';
import 'design_tokens.dart';

class VoiceCaptureModeOptions extends StatefulWidget {
  const VoiceCaptureModeOptions({
    super.key,
    required this.api,
    required this.active,
    required this.capturing,
    required this.currentMode,
    this.modeSupported,
    this.adaptiveLiveSupported = false,
    this.stopUnconfirmed = false,
    this.startPending = false,
    this.onInspected,
    this.writes,
  });

  final CaptureModeApi? api;
  final bool Function() active;
  final bool capturing;
  final CaptureMode? currentMode;
  final bool Function(CaptureMode)? modeSupported;
  final bool adaptiveLiveSupported;
  final bool stopUnconfirmed, startPending;
  final void Function(CaptureModeSnapshot)? onInspected;
  final CaptureModeWrites? writes;

  @override
  State<VoiceCaptureModeOptions> createState() =>
      _VoiceCaptureModeOptionsState();
}

class _VoiceCaptureModeOptionsState extends State<VoiceCaptureModeOptions> {
  CaptureModeWrites get _writes => widget.writes ?? captureModeWrites;
  CaptureModeScope? _scope;
  CaptureModeSnapshot? _snapshot;
  String? _error;
  bool _reading = false;
  int _read = 0;

  @override
  void initState() {
    super.initState();
    _writes.addListener(_changed);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant VoiceCaptureModeOptions oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.writes != widget.writes) {
      (oldWidget.writes ?? captureModeWrites).removeListener(_changed);
      _writes.addListener(_changed);
    }
    if (!identical(oldWidget.api, widget.api) ||
        oldWidget.writes != widget.writes) {
      _read++;
      _snapshot = null;
      _scope = null;
      _error = null;
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    _writes.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  bool _current(CaptureModeApi api, bool Function() active) =>
      mounted && identical(api, widget.api) && active() && widget.active();

  Future<void> _load() async {
    final api = widget.api, active = widget.active, ticket = ++_read;
    if (api == null || !active()) return;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final snapshot = await api.inspect().result;
      if (!_current(api, active) || ticket != _read) return;
      final scope = _writes.open(api, snapshot);
      setState(() {
        _snapshot = snapshot;
        _scope = scope;
      });
      widget.onInspected?.call(snapshot);
    } catch (_) {
      if (_current(api, active) && ticket == _read) {
        setState(() {
          _snapshot = null;
          _error =
              'Capture Mode could not be inspected. Check the Converse connection and host version.';
        });
      }
    } finally {
      if (_current(api, active) && ticket == _read) {
        setState(() => _reading = false);
      }
    }
  }

  Future<void> _save() async {
    final api = widget.api, scope = _scope, active = widget.active;
    if (api == null || scope == null || _reading || !active()) return;
    final pending = _writes.begin(scope, api);
    if (pending == null) return;
    await _writes.send(pending, api);
    if (_current(api, active)) await _load();
  }

  Future<void> _lookup() async {
    final api = widget.api, pending = _scope?.pending, active = widget.active;
    if (api == null || pending == null || !active()) return;
    await _writes.lookup(pending, api);
    if (_current(api, active) && pending.complete) await _load();
  }

  String _label(CaptureMode? mode) => mode?.label ?? 'Unknown';

  @override
  Widget build(BuildContext context) {
    final snapshot = _snapshot, scope = _scope, pending = scope?.pending;
    final draft = scope?.draft;
    final stale =
        snapshot != null &&
        draft != null &&
        !draft.base.sameObservation(snapshot);
    final writable =
        widget.active() &&
        !_reading &&
        pending == null &&
        snapshot?.writable == true &&
        scope != null &&
        scope.isCurrentBackend;
    return DesignPanel(
      key: const ValueKey('voice-group-capture-mode'),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Capture Mode', style: Theme.of(context).textTheme.titleSmall),
          const SizedBox(height: 8),
          const Text(
            'Choose how the next Call captures speech. Saving does not activate the microphone.',
          ),
          Text(
            widget.stopUnconfirmed
                ? 'Microphone stop: Unconfirmed'
                : widget.startPending && !widget.capturing
                ? 'Current Call: Starting'
                : widget.capturing
                ? 'Current capture: ${_label(widget.currentMode)}'
                : 'Current Call: Inactive',
          ),
          if (widget.api == null)
            const Text('Connect to Converse to inspect its saved choice.'),
          if (_reading) const LinearProgressIndicator(),
          if (_error != null) Text(_error!),
          if (snapshot != null) ...[
            Text('Selected for next Call: ${_label(snapshot.selectedMode)}'),
            if (snapshot.selectedMode case final mode?)
              if (widget.modeSupported?.call(mode) == false)
                const Text(
                  'The selected Capture Mode is unavailable for this audio input interface. Choose another mode explicitly.',
                ),
            Text(
              'Source: ${snapshot.selection.isHost ? 'Host defaults' : 'Owner saved'}',
            ),
            if (!snapshot.adaptiveLiveEnabled || !widget.adaptiveLiveSupported)
              const Text('Adaptive Live is not available on this host.'),
            if (scope?.isCurrentBackend == false)
              const Text(
                'The Converse backend changed. Refresh before changing Capture Mode.',
              ),
            const SizedBox(height: 12),
            if (draft != null)
              DropdownButtonFormField<String>(
                key: const Key('capture-mode-choice'),
                initialValue: draft.selection.mode?.wire ?? 'host',
                isExpanded: true,
                itemHeight: null,
                decoration: const InputDecoration(
                  labelText: 'Next Call',
                  border: OutlineInputBorder(),
                ),
                items: [
                  const DropdownMenuItem(
                    value: 'host',
                    child: Text('Host defaults'),
                  ),
                  DropdownMenuItem(
                    value: CaptureMode.adaptiveLive.wire,
                    enabled:
                        snapshot.adaptiveLiveEnabled &&
                        widget.adaptiveLiveSupported,
                    child: const Text('Adaptive Live'),
                  ),
                  DropdownMenuItem(
                    value: CaptureMode.rollingWindows.wire,
                    child: const Text('Rolling Windows'),
                  ),
                  const DropdownMenuItem<String>(
                    enabled: false,
                    child: Text('Advanced / Compatibility'),
                  ),
                  DropdownMenuItem(
                    value: CaptureMode.legacySilence.wire,
                    child: const Text('Legacy Silence'),
                  ),
                ],
                onChanged: writable
                    ? (value) {
                        if (value == null) return;
                        final selection = value == 'host'
                            ? const CaptureModeSelection.host()
                            : CaptureModeSelection.saved(
                                CaptureMode.values.firstWhere(
                                  (mode) => mode.wire == value,
                                ),
                              );
                        _writes.select(scope, selection);
                      }
                    : null,
              ),
            const SizedBox(height: 8),
            const Text(
              'Rolling Windows stays live during speech. Legacy Silence ends a clip after its configured silence interval.',
            ),
            if (stale)
              const Text(
                'Capture settings changed. This draft is retained; inspect the current choice before saving.',
              ),
            if (!snapshot.writable || scope == null)
              const Text(
                'Capture Mode is read-only. Resolve retained changes or inspect the settings owner.',
              ),
          ],
          if (scope?.notice case final notice?) Text(notice),
          if (pending != null) ...[
            Text(pending.notice),
            OutlinedButton(
              onPressed: pending.checking || !widget.active() ? null : _lookup,
              child: const Text('Check save status'),
            ),
          ],
          const SizedBox(height: 12),
          Wrap(
            spacing: 12,
            runSpacing: 8,
            children: [
              FilledButton(
                onPressed: writable && draft?.dirty == true && !stale
                    ? _save
                    : null,
                child: const Text('Save Capture Mode'),
              ),
              OutlinedButton(
                onPressed: _reading || !widget.active() ? null : _load,
                child: const Text('Refresh Capture Mode'),
              ),
              if (stale && pending == null && scope != null)
                OutlinedButton(
                  onPressed: () => _writes.reset(scope),
                  child: const Text('Use inspected choice'),
                ),
            ],
          ),
        ],
      ),
    );
  }
}
