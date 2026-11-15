import 'dart:async';

import 'package:flutter/material.dart';

import '../api/core_subsystems.dart';
import '../api/core_subsystems_client.dart';
import '../state/core_subsystem_writes.dart';
import 'design_tokens.dart';

class OptionsSubsystems extends StatefulWidget {
  const OptionsSubsystems({
    super.key,
    required this.api,
    required this.active,
    this.writes,
  });
  final CoreSubsystemApi? api;
  final bool Function() active;
  final CoreSubsystemWrites? writes;
  @override
  State<OptionsSubsystems> createState() => _OptionsSubsystemsState();
}

class _OptionsSubsystemsState extends State<OptionsSubsystems> {
  CoreSubsystemWrites get _writes => widget.writes ?? coreSubsystemWrites;
  CoreSubsystemSnapshot? _snapshot;
  CoreSubsystemScope? _scope;
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
  void didUpdateWidget(covariant OptionsSubsystems oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.writes != widget.writes) {
      (oldWidget.writes ?? coreSubsystemWrites).removeListener(_changed);
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

  bool _current(CoreSubsystemApi api, bool Function() active) =>
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
    } catch (_) {
      if (_current(api, active) && ticket == _read) {
        setState(() {
          _snapshot = null;
          _error =
              'Core subsystem settings could not be inspected. Check the Core connection and host version.';
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

  String _label(CoreSubsystemMode? mode) => mode?.label ?? 'Unknown';

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
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Subsystems', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 8),
          const Text(
            'Choose the subsystems for the next managed host launch. Saving does not start or stop services.',
          ),
          const SizedBox(height: 8),
          const Text(
            'Converse always includes Decisions. Agent can run independently.',
          ),
          if (widget.api == null) ...[
            const SizedBox(height: 16),
            const Text('Connect to Core to inspect its saved selection.'),
          ],
          if (_error != null) ...[const SizedBox(height: 16), Text(_error!)],
          if (_reading) const LinearProgressIndicator(),
          if (snapshot != null) ...[
            const SizedBox(height: 16),
            Text(
              'Selection source: ${snapshot.selection.isHost ? 'Host defaults' : 'Owner saved'}',
            ),
            Text('Selected for next launch: ${_label(snapshot.selectedMode)}'),
            Text('Host defaults: ${_label(snapshot.hostMode)}'),
            Text('Startup declaration: ${_label(snapshot.startupMode)}'),
            const Text('Running services: Not observed'),
            if (scope?.isCurrentBackend == false)
              const Text(
                'The Core backend changed. Refresh to inspect its current selection.',
              ),
            const SizedBox(height: 16),
            if (draft != null)
              DropdownButtonFormField<String>(
                key: const Key('core-subsystem-choice'),
                initialValue: draft.selection.mode?.wire ?? 'host',
                isExpanded: true,
                itemHeight: null,
                decoration: const InputDecoration(
                  labelText: 'Next launch selection',
                  border: OutlineInputBorder(),
                ),
                items: [
                  const DropdownMenuItem(
                    value: 'host',
                    child: Text('Host defaults'),
                  ),
                  for (final mode in CoreSubsystemMode.values)
                    DropdownMenuItem(value: mode.wire, child: Text(mode.label)),
                ],
                onChanged: writable
                    ? (value) {
                        if (value == null) return;
                        final selection = value == 'host'
                            ? const CoreSubsystemSelection.host()
                            : CoreSubsystemSelection.saved(
                                CoreSubsystemMode.values.firstWhere(
                                  (mode) => mode.wire == value,
                                ),
                              );
                        _writes.select(scope, selection);
                      }
                    : null,
              ),
            if (stale) ...[
              const SizedBox(height: 12),
              const Text(
                'Core settings changed while this draft was open. The draft is retained; inspect the latest selection before saving.',
              ),
            ],
            if (!snapshot.writable || scope == null) ...[
              const SizedBox(height: 12),
              const Text(
                'This selection is read-only. Resolve retained changes or check the Core settings owner.',
              ),
            ],
          ],
          if (scope?.notice case final notice?) ...[
            const SizedBox(height: 12),
            Text(notice),
          ],
          if (pending != null) ...[
            const SizedBox(height: 12),
            Text(pending.notice),
            Align(
              alignment: Alignment.centerLeft,
              child: OutlinedButton(
                onPressed: pending.checking || !widget.active()
                    ? null
                    : _lookup,
                child: const Text('Check save status'),
              ),
            ),
          ],
          const SizedBox(height: 16),
          Wrap(
            spacing: 12,
            runSpacing: 12,
            children: [
              FilledButton(
                onPressed: writable && draft?.dirty == true && !stale
                    ? _save
                    : null,
                child: const Text('Save for next launch'),
              ),
              OutlinedButton(
                onPressed: writable && draft?.dirty == true
                    ? () => _writes.reset(scope)
                    : null,
                child: const Text('Use inspected selection'),
              ),
              OutlinedButton(
                onPressed: widget.api != null && widget.active() && !_reading
                    ? _load
                    : null,
                child: const Text('Refresh selection'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
