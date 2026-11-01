import 'dart:async';
import 'package:flutter/material.dart';
import '../api/client.dart';
import '../api/context_usage.dart';
import '../api/context_usage_client.dart';
import '../state/app_state.dart';

/// Passive measurements and the actual suffix policy, within traditional Options.
class OptionsContextHistory extends StatefulWidget {
  const OptionsContextHistory({
    super.key,
    required this.state,
    required this.onProviders,
  });
  final AppState state;
  final VoidCallback onProviders;
  @override
  State<OptionsContextHistory> createState() => _OptionsContextHistoryState();
}

class _OptionsContextHistoryState extends State<OptionsContextHistory> {
  KinaiyaClient? _client;
  int _generation = -1, _read = 0;
  final Map<String, ContextUsageSnapshot> _snapshots = {};
  final Map<String, String> _errors = {};
  final Set<String> _loading = {};
  @override
  void initState() {
    super.initState();
    widget.state.addListener(_changed);
    _changed();
  }

  @override
  void didUpdateWidget(covariant OptionsContextHistory oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.state, widget.state)) {
      oldWidget.state.removeListener(_changed);
      widget.state.addListener(_changed);
      _client = null;
      _generation = -1;
      _changed();
    }
  }

  void _changed() {
    if (identical(_client, widget.state.client) &&
        _generation == widget.state.connectionGeneration) {
      return;
    }
    _client = widget.state.client;
    _generation = widget.state.connectionGeneration;
    _read++;
    _snapshots.clear();
    _errors.clear();
    _loading.clear();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) {
        setState(() {});
        unawaited(_refresh());
      }
    });
  }

  @override
  void dispose() {
    _read++;
    widget.state.removeListener(_changed);
    super.dispose();
  }

  Future<void> _refresh() async {
    final client = _client, generation = _generation;
    if (client == null || _loading.isNotEmpty) {
      return;
    }
    final read = ++_read;
    setState(() {
      _loading.addAll(['agent', 'converse']);
      _errors.clear();
      _snapshots.clear();
    });
    bool current() =>
        mounted &&
        read == _read &&
        identical(widget.state.client, client) &&
        generation == widget.state.connectionGeneration &&
        identical(_client, client);
    Future<void> load(String role, ContextUsageApi api) async {
      try {
        final value = await api.inspect();
        if (current()) {
          setState(() {
            _snapshots[role] = value;
          });
        }
      } catch (_) {
        if (current()) {
          setState(() {
            _errors[role] =
                '$role context observations are unavailable. Other roles remain independent.';
          });
        }
      } finally {
        if (current()) {
          setState(() {
            _loading.remove(role);
          });
        }
      }
    }

    await Future.wait([
      load('converse', client.converseContext),
      load('agent', client.agentContext),
    ]);
  }

  Widget _line(String label, String value) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: SelectableText('$label: $value'),
  );
  Widget _observation(ContextUsageRow row) {
    final item = row.observation, model = row.observation.model;
    final renderedClient = _client, renderedGeneration = _generation;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${model.model} · ${model.provider}',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 8),
            _line(
              'Observed',
              '${item.observedAt} · ${item.phase == 'prepared' ? 'Finalized preparation' : 'Native generation export admitted'}',
            ),
            _line(
              'Configuration at last refresh',
              row.configurationStatus == 'current'
                  ? 'Matches the saved role/harness configuration'
                  : row.configurationStatus == 'obsolete'
                  ? 'Configuration has changed; this is a historical measurement'
                  : 'Current configuration could not be established',
            ),
            Wrap(
              spacing: 24,
              runSpacing: 8,
              children: [
                Text('Input  ${item.input} tokens'),
                Text('Output reserve  ${item.reserve}'),
                Text('Capacity  ${item.capacity}'),
                Text('Headroom  ${item.headroom}'),
              ],
            ),
            const SizedBox(height: 12),
            _line(
              'Prompt history',
              '${item.kept} ${item.unit}s retained, ${item.dropped} omitted from ${item.examined} examined · ${item.attempts} measurements',
            ),
            if (!item.historyComplete)
              const Text(
                'Older history was not fully examined. These counts cover the examined history only.',
              ),
            if (model.catalog != null) ...[
              _line(
                'Catalog',
                '${model.catalog!.id} · generation ${model.catalog!.generation}',
              ),
              _line('Configuration SHA-256', model.catalog!.digest),
              OutlinedButton(
                onPressed: () {
                  if (mounted &&
                      identical(widget.state.client, renderedClient) &&
                      widget.state.connectionGeneration == renderedGeneration) {
                    widget.onProviders();
                  }
                },
                child: const Text('Open provider settings'),
              ),
              const Text(
                'Edit a supported current profile’s response reserve there. Saving creates a new generation; review and select that generation in Models or Harness Customization.',
              ),
            ] else
              const Text(
                'This host configuration has no editable catalog generation here. Its response reserve is the captured native value.',
              ),
            ExpansionTile(
              tilePadding: EdgeInsets.zero,
              title: const Text('Measurement identity and scope'),
              children: [
                _line('Operation', item.operationId),
                _line('Observation', item.id),
                _line(
                  'Session / thread / epoch',
                  '${item.sessionId} / ${item.threadId} / ${item.epoch}',
                ),
                if (item.taskId != null)
                  _line(
                    'Task / objective revision',
                    '${item.taskId} / ${item.objectiveRevision}',
                  ),
                if (item.harnessVersion != null)
                  _line('Harness', item.harnessVersion!),
                _line(
                  'Source event IDs',
                  item.sourceEventIds.isEmpty
                      ? 'None'
                      : item.sourceEventIds.join(', '),
                ),
                _line(
                  'History watermark',
                  item.throughSequence?.toString() ??
                      'Not recorded for this role',
                ),
                _line(
                  'Model source',
                  '${model.source} · ${model.local ? 'local' : 'remote'}',
                ),
                _line(
                  'Model settings revision',
                  model.settingsRevision?.toString() ?? 'Unmanaged / unknown',
                ),
                if (model.profileId != null)
                  _line('Host profile', model.profileId!),
                if (model.catalogRevision != null)
                  _line(
                    'Catalog revision at capture',
                    '${model.catalogRevision}',
                  ),
                if (model.reasoning != null)
                  _line('Captured reasoning', model.reasoning!),
                _line('Request SHA-256', item.requestDigest),
                _line('Binding SHA-256', item.bindingDigest),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _role(String role, String title) {
    final snapshot = _snapshots[role];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const SizedBox(height: 24),
        Text(title, style: Theme.of(context).textTheme.titleLarge),
        const SizedBox(height: 8),
        if (_loading.contains(role)) const LinearProgressIndicator(),
        if (_errors[role] case final error?) Text(error),
        if (snapshot != null) ...[
          if (snapshot.rows.isEmpty)
            Text(
              snapshot.mode == 'legacy_unmeasured'
                  ? 'This embedded host uses legacy unmeasured context. No token capacity is inferred.'
                  : 'No measured preparation has been captured in this process. Context usage is unknown.',
            ),
          if (snapshot.rows.isNotEmpty)
            Text(
              'Latest ${snapshot.rows.length} observations · ${snapshot.retained} retained in this process. Values describe those requests, not the current allocation or billing.',
            ),
          for (final row in snapshot.rows) _observation(row),
          _line('Native process instance', snapshot.instanceId),
        ],
      ],
    );
  }

  @override
  Widget build(BuildContext context) => SingleChildScrollView(
    child: Align(
      alignment: Alignment.topLeft,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 1100),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Measured prompt context'),
            const SizedBox(height: 8),
            const Text(
              'Measurements are captured during normal work. Refresh reads metadata only. Headroom is capacity minus measured input and reserved output at that preparation; it is not a live allocation.',
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 12,
              children: [
                OutlinedButton.icon(
                  onPressed: _client == null || _loading.isNotEmpty
                      ? null
                      : _refresh,
                  icon: const Icon(Icons.refresh),
                  label: const Text('Refresh observations'),
                ),
                TextButton(
                  onPressed: _client == null ? null : widget.onProviders,
                  child: const Text('Provider reserve settings'),
                ),
              ],
            ),
            const SizedBox(height: 16),
            const Text(
              'Converse keeps a chronological suffix of whole messages. Agent keeps whole exchanges, including action/result pairs. Messages omitted from a prompt remain in the transcript. Optional recall and protected instructions still follow their native admission rules.',
            ),
            const SizedBox(height: 8),
            const Text(
              'Automatic summary compaction is not configured by this page. Transcript retention, export and deletion are separate data controls. These bounded metadata samples are lost on service restart; absence is not proof that no work occurred.',
            ),
            if (_client == null)
              const Padding(
                padding: EdgeInsets.only(top: 16),
                child: Text('Connect to inspect native context observations.'),
              ),
            _role('converse', 'Converse'),
            _role('agent', 'Agent'),
          ],
        ),
      ),
    ),
  );
}
