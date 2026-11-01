/// Owner mode controls; health, task inspection and Stop remain independent.
library;

import 'dart:async';
import 'package:flutter/material.dart';
import '../api/agent_control.dart';
import '../api/agent_control_client.dart';
import '../api/client.dart' show newIdempotencyKey;
import '../state/agent_mode_writes.dart';

class AgentOperatingModePanel extends StatefulWidget {
  const AgentOperatingModePanel({
    super.key,
    required this.api,
    required this.active,
    required this.ownerAuthenticated,
    this.writes,
  });
  final AgentControlApi? api;
  final bool Function() active;
  final bool ownerAuthenticated;
  final AgentModeWrites? writes;

  @override
  State<AgentOperatingModePanel> createState() =>
      _AgentOperatingModePanelState();
}

class _AgentOperatingModePanelState extends State<AgentOperatingModePanel> {
  AgentModeWrites get _writes => widget.writes ?? agentModeWrites;
  AgentControlSnapshot? _snapshot;
  AgentModeWriteScope? _scope;
  String? _readFailure;
  bool _loading = false, _expanded = false;
  int _readGeneration = 0;

  @override
  void initState() {
    super.initState();
    _writes.addListener(_changed);
  }

  @override
  void didUpdateWidget(AgentOperatingModePanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.writes != widget.writes) {
      (oldWidget.writes ?? agentModeWrites).removeListener(_changed);
      _writes.addListener(_changed);
    }
    if (!identical(oldWidget.api, widget.api) ||
        oldWidget.writes != widget.writes) {
      _snapshot = null;
      _scope = null;
      _readGeneration++;
      _loading = false;
      _readFailure = null;
      if (_expanded) unawaited(_refresh());
    }
  }

  void _changed() {
    if (mounted && widget.active()) setState(() {});
  }

  Future<void> _refresh() async {
    final api = widget.api;
    final generation = ++_readGeneration;
    if (api == null || !widget.active()) {
      _snapshot = null;
      _scope = null;
      _readFailure = null;
      _loading = false;
      return;
    }
    setState(() {
      _loading = true;
      _readFailure = null;
    });
    try {
      final snapshot = await api.snapshot().result;
      if (!_ownsRead(api, generation)) return;
      setState(() {
        _snapshot = snapshot;
        _scope = _writes.open(api, snapshot);
      });
    } catch (_) {
      if (!_ownsRead(api, generation)) return;
      setState(() {
        _snapshot = null;
        _scope = null;
        _readFailure =
            'Operating mode is unknown. Refresh to verify Session control.';
      });
    } finally {
      if (_ownsRead(api, generation)) setState(() => _loading = false);
    }
  }

  bool _ownsRead(AgentControlApi api, int generation) =>
      mounted &&
      widget.active() &&
      identical(api, widget.api) &&
      generation == _readGeneration;

  Future<void> _save() async {
    final api = widget.api, scope = _scope;
    if (api == null || scope == null || !_editable) return;
    final state = _snapshot!;
    final request = AgentModeChange.fromJson({
      'schema_version': '1.0',
      'idempotency_key': newIdempotencyKey(),
      'expected_backend': state.backendId,
      'expected_instance': state.instanceId,
      'expected_revision': state.revision,
      'mode': scope.selection.wire,
      'active_work_policy': agentActiveWorkPolicy,
    });
    final operation = _writes.begin(scope, api, request);
    if (operation != null) await _attempt(operation, lookup: false);
  }

  Future<void> _attempt(
    PendingAgentModeChange operation, {
    required bool lookup,
  }) async {
    final api = widget.api, snapshot = _snapshot;
    if (api == null || !widget.active() || !widget.ownerAuthenticated) return;
    final outcome = await (lookup
        ? _writes.lookup(operation, api, snapshot)
        : _writes.retry(operation, api, snapshot));
    // A receipt remains history. One fresh metadata read follows an explicit
    // action; reopening or checking state never dispatches a mutation or lookup.
    if (outcome != null &&
        mounted &&
        widget.active() &&
        identical(api, widget.api)) {
      await _refresh();
    }
  }

  bool get _editable =>
      widget.active() &&
      widget.ownerAuthenticated &&
      !_loading &&
      _scope != null &&
      _snapshot!.matches(_scope!.snapshot) &&
      _snapshot?.controlWritable == true &&
      _snapshot!.revision < 9007199254740991 &&
      _scope!.pending == null &&
      widget.api?.hasCapacity == true;

  @override
  Widget build(BuildContext context) {
    final snapshot = _snapshot, scope = _scope, api = widget.api;
    final pending = scope?.pending;
    return Card(
      margin: const EdgeInsets.fromLTRB(16, 4, 16, 12),
      child: ExpansionTile(
        title: const Text('Agent operating mode'),
        onExpansionChanged: (expanded) {
          _expanded = expanded;
          if (expanded && _snapshot == null && !_loading) unawaited(_refresh());
        },
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              snapshot == null
                  ? 'Current mode: unknown · read-only'
                  : 'Current mode: ${snapshot.mode.label} · revision ${snapshot.revision}${snapshot.controlWritable ? '' : ' · read-only'}',
            ),
            if (snapshot != null && !snapshot.standingWorkAvailable)
              const Text('Autonomous standing work is unavailable.'),
            if (pending != null)
              Text(
                pending.uncertain
                    ? 'Earlier mode save is unconfirmed.'
                    : 'Saving mode…',
              ),
          ],
        ),
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    TextButton(
                      onPressed: _loading || api == null || !widget.active()
                          ? null
                          : _refresh,
                      child: Text(_loading ? 'Refreshing…' : 'Refresh mode'),
                    ),
                  ],
                ),
                const Text(
                  'Mode controls new work admission, separately from Agent health. Work already accepted may finish. Stop and task inspection remain available.',
                ),
                const Text(
                  'Mode does not grant tool, network, disclosure or spending permission.',
                ),
                if (_readFailure != null) Text(_readFailure!),
                if (snapshot == null)
                  const Text(
                    'Read-only until current mode and control authority are verified.',
                  )
                else ...[
                  if (!widget.ownerAuthenticated)
                    const Text(
                      'Read-only: owner authentication is not verified.',
                    ),
                  if (!snapshot.controlWritable)
                    const Text(
                      'Read-only: this Session controller is not the active mode writer.',
                    ),
                  if (scope == null)
                    const Text(
                      'Mode controls are busy. Retained drafts and uncertain saves have not been discarded.',
                    ),
                  if (scope != null && !snapshot.matches(scope.snapshot))
                    const Text(
                      'Read-only: the mode observation changed. Refresh before editing.',
                    ),
                  if (snapshot.revision == 9007199254740991)
                    const Text(
                      'Read-only: the mode revision limit has been reached.',
                    ),
                  if (!snapshot.standingWorkAvailable)
                    Text(
                      snapshot.mode == AgentMode.autonomous
                          ? 'Autonomous is the persisted mode. Standing work is unavailable; monitoring is not configured.'
                          : 'Autonomous is unavailable until standing work is implemented and authorized.',
                    ),
                  DropdownButton<AgentMode>(
                    key: const ValueKey('agent-mode-selection'),
                    value: scope?.selection ?? snapshot.mode,
                    isExpanded: true,
                    items: [
                      for (final mode in AgentMode.values)
                        DropdownMenuItem(
                          value: mode,
                          enabled: snapshot.availableModes.contains(mode),
                          child: Text(
                            snapshot.availableModes.contains(mode)
                                ? mode.label
                                : '${mode.label} (unavailable)',
                          ),
                        ),
                    ],
                    onChanged: !_editable
                        ? null
                        : (mode) {
                            if (mode != null &&
                                snapshot.availableModes.contains(mode)) {
                              setState(() => scope!.selection = mode);
                            }
                          },
                  ),
                  if (scope?.notice != null) Text(scope!.notice!),
                  if (pending != null) ...[
                    Text(
                      'Original requested mode: ${pending.request.mode.label}',
                    ),
                    Text(pending.notice),
                    Wrap(
                      spacing: 8,
                      children: [
                        OutlinedButton(
                          onPressed:
                              api != null &&
                                  !_loading &&
                                  widget.active() &&
                                  widget.ownerAuthenticated &&
                                  _writes.canLookup(pending, api, snapshot)
                              ? () => _attempt(pending, lookup: true)
                              : null,
                          child: const Text('Check original save'),
                        ),
                        TextButton(
                          onPressed:
                              api != null &&
                                  !_loading &&
                                  widget.active() &&
                                  widget.ownerAuthenticated &&
                                  _writes.canRetry(pending, api, snapshot)
                              ? () => _attempt(pending, lookup: false)
                              : null,
                          child: const Text('Retry exact save'),
                        ),
                      ],
                    ),
                  ] else
                    Wrap(
                      spacing: 8,
                      children: [
                        FilledButton(
                          onPressed:
                              _editable &&
                                  scope!.dirty &&
                                  snapshot.availableModes.contains(
                                    scope.selection,
                                  )
                              ? _save
                              : null,
                          child: const Text('Save mode'),
                        ),
                        TextButton(
                          onPressed: _editable && scope!.dirty
                              ? () => _writes.reset(scope)
                              : null,
                          child: const Text('Reset mode draft'),
                        ),
                      ],
                    ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    _readGeneration++;
    _writes.removeListener(_changed);
    super.dispose();
  }
}
