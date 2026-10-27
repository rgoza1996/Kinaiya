import 'dart:convert';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../state/app_state.dart';

/// Owner controls, available without inference and only linked from Agent.
class McpSettings extends StatefulWidget {
  const McpSettings({super.key, required this.state});
  final AppState state;

  @override
  State<McpSettings> createState() => _McpSettingsState();
}

class _McpSettingsState extends State<McpSettings> {
  Map<String, Object?>? _snapshot;
  KinaiyaClient? _client;
  int _generation = -1;
  bool _busy = false;
  bool _unconfirmed = false;
  String? _notice;

  AppState get _app => widget.state;

  @override
  void initState() {
    super.initState();
    _app.addListener(_connectionChanged);
    _connectionChanged();
  }

  @override
  void dispose() {
    _app.removeListener(_connectionChanged);
    super.dispose();
  }

  @override
  void didUpdateWidget(covariant McpSettings oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.state, widget.state)) {
      oldWidget.state.removeListener(_connectionChanged);
      _app.addListener(_connectionChanged);
      _connectionChanged();
    }
  }

  void _connectionChanged() {
    if (identical(_client, _app.client) &&
        _generation == _app.connectionGeneration) {
      return;
    }
    setState(() {
      _client = _app.client;
      _generation = _app.connectionGeneration;
      _snapshot = null;
      _notice = null;
      _busy = false;
      _unconfirmed = false;
    });
    _refresh();
  }

  bool _owns(KinaiyaClient client, int generation) =>
      mounted && _app.ownsConnection(generation, client);

  Future<void> _refresh() async {
    final client = _client;
    final generation = _generation;
    if (client == null) {
      if (mounted) {
        setState(() => _notice = 'Connect to the owner service first.');
      }
      return;
    }
    try {
      final snapshot = await client.mcpManagement();
      if (!_owns(client, generation)) return;
      if (snapshot['configured'] is! bool ||
          snapshot['revision'] is! int ||
          snapshot['blocked'] is! bool ||
          snapshot['servers'] is! List ||
          (snapshot['servers'] as List).length > 16) {
        throw const FormatException();
      }
      for (final row in snapshot['servers'] as List) {
        if (row is! Map ||
            row['id'] is! String ||
            row['enabled'] is! bool ||
            !{'stdio', 'http'}.contains(row['transport']) ||
            row['endpoint'] is! String ||
            row['allowed_tools'] is! List ||
            (row['allowed_tools'] as List).any((name) => name is! String)) {
          throw const FormatException();
        }
      }
      setState(() {
        if (_unconfirmed || _snapshot == null) _notice = null;
        _snapshot = snapshot;
        _unconfirmed = false;
      });
    } catch (_) {
      if (!_owns(client, generation)) return;
      setState(() {
        _snapshot = null;
        _notice =
            'Could not inspect MCP management. Check the service connection.';
      });
    }
  }

  Future<bool> _confirm(String title, String body, String action) async =>
      await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text(title),
          content: Text(body),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text(action),
            ),
          ],
        ),
      ) ??
      false;

  Future<void> _configure() async {
    final client = _client;
    final generation = _generation;
    final revision = _snapshot?['revision'];
    if (client == null || revision is! int || _busy || _unconfirmed) return;
    final profile = await showDialog<Map<String, Object?>>(
      context: context,
      builder: (context) => const _ProfileEditor(),
    );
    if (!_owns(client, generation) || profile == null) return;
    await _apply(
      'configure',
      client,
      generation,
      revision,
      config: profile,
      reviewed: true,
    );
  }

  Future<void> _action(String operation, Map<String, Object?>? server) async {
    final client = _client;
    final generation = _generation;
    final revision = _snapshot?['revision'];
    if (client == null || revision is! int || _busy || _unconfirmed) return;
    final id = server?['id'] as String?;
    final reviewed = operation == 'enable' || operation == 'reconcile';
    if (reviewed ||
        operation == 'remove' ||
        operation == 'test' ||
        operation == 'reconnect') {
      final body = operation == 'enable'
          ? 'Enable $id with its configured tool allowlist, disclosure scope and secret references? '
                'Existing workspace and network permissions still apply.'
          : operation == 'reconcile'
          ? 'Apply the saved configuration after reviewing the original profiles and their permissions. '
                'This does not replay tool calls.'
          : operation == 'remove'
          ? 'Remove connection $id? Reconfiguration requires its original profile.'
          : 'Connect to $id and list its tools. For a local server this starts its configured process. '
                'No tools or language-model inference will be invoked.';
      if (!await _confirm('$operation MCP connection', body, operation)) return;
    }
    if (!_owns(client, generation)) return;
    await _apply(
      operation,
      client,
      generation,
      revision,
      serverId: id,
      reviewed: reviewed,
    );
  }

  Future<void> _apply(
    String operation,
    KinaiyaClient client,
    int generation,
    int revision, {
    String? serverId,
    Map<String, Object?>? config,
    bool reviewed = false,
  }) async {
    if (!_owns(client, generation)) return;
    setState(() {
      _busy = true;
      _notice = 'Applying $operation…';
    });
    try {
      final result = await client.controlMcp({
        'schema_version': '1.0',
        'operation': operation,
        'idempotency_key': newIdempotencyKey(),
        'expected_revision': revision,
        if (serverId != null) 'server_id': serverId,
        if (config != null) 'config': config,
        'reviewed': reviewed,
      });
      if (!_owns(client, generation)) return;
      setState(() {
        _notice = operation == 'test' || operation == 'reconnect'
            ? 'Connection check completed: ${result['status'] ?? 'inspect server status below'}.'
            : '$operation completed. Inspect the current revision below.';
      });
      await _refresh();
    } catch (error) {
      if (!_owns(client, generation)) return;
      setState(() {
        _unconfirmed = true;
        _notice = error is ApiException && error.status == 409
            ? 'Finish or stop active and recoverable Agent tasks before managing MCP.'
            : 'Request not confirmed. Refresh and inspect state before another action; '
                  'the request will not be retried automatically.';
      });
    } finally {
      if (_owns(client, generation)) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final configured = _snapshot?['configured'] == true;
    final blocked = _snapshot?['blocked'] == true;
    final rows = (_snapshot?['servers'] as List? ?? const [])
        .whereType<Map>()
        .map((row) => row.cast<String, Object?>())
        .toList();
    final canControl = configured && !_busy && !_unconfirmed;
    return Dialog(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 760, maxHeight: 700),
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  const Expanded(
                    child: Text(
                      'MCP connections',
                      style: TextStyle(fontSize: 22),
                    ),
                  ),
                  IconButton(
                    tooltip: 'Close MCP settings',
                    onPressed: () => Navigator.pop(context),
                    icon: const Icon(Icons.close),
                  ),
                ],
              ),
              const Text(
                'Owner controls run without model inference. Finish or stop active Agent tasks before changing connections. '
                'Inspection never starts a server; Test and Reconnect do.',
              ),
              if (_notice != null)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(_notice!),
                ),
              if (_snapshot == null && _notice == null)
                const LinearProgressIndicator(),
              if (_snapshot != null && !configured)
                const Text(
                  'MCP management is not enabled. Enable the optional MCP host in owner configuration, then restart Agent.',
                ),
              if (configured)
                Text('Configuration revision ${_snapshot!['revision']}'),
              if (blocked)
                const Text(
                  'A configuration change needs owner reconciliation. Review the saved profiles before applying them.',
                ),
              Wrap(
                spacing: 8,
                children: [
                  TextButton.icon(
                    onPressed: _busy ? null : _refresh,
                    icon: const Icon(Icons.refresh),
                    label: const Text('Refresh connections'),
                  ),
                  FilledButton.icon(
                    onPressed: canControl && !blocked ? _configure : null,
                    icon: const Icon(Icons.add),
                    label: const Text('Add or replace profile'),
                  ),
                  if (blocked)
                    TextButton(
                      onPressed: canControl
                          ? () => _action('reconcile', null)
                          : null,
                      child: const Text('Review and reconcile'),
                    ),
                ],
              ),
              Flexible(
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    if (configured && rows.isEmpty)
                      const Padding(
                        padding: EdgeInsets.all(16),
                        child: Text('No MCP connections configured.'),
                      ),
                    for (final row in rows)
                      Card(
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                '${row['id']} · ${row['enabled'] == true ? 'enabled' : 'disabled'}',
                                style: const TextStyle(
                                  fontWeight: FontWeight.bold,
                                ),
                              ),
                              Text('${row['transport']} · ${row['endpoint']}'),
                              Text(
                                'Disclosure: ${row['content_scope']} · Allowed tools: ${(row['allowed_tools'] as List? ?? []).join(', ')}',
                              ),
                              if (row['credentialed'] == true)
                                const Text(
                                  'Uses owner-scoped secret references.',
                                ),
                              Wrap(
                                spacing: 8,
                                children: [
                                  for (final operation in [
                                    'test',
                                    if (row['enabled'] == true) ...[
                                      'reconnect',
                                      'disable',
                                    ] else
                                      'enable',
                                    'remove',
                                  ])
                                    TextButton(
                                      onPressed: canControl && !blocked
                                          ? () => _action(operation, row)
                                          : null,
                                      child: Text('$operation ${row['id']}'),
                                    ),
                                ],
                              ),
                            ],
                          ),
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

class _ProfileEditor extends StatefulWidget {
  const _ProfileEditor();
  @override
  State<_ProfileEditor> createState() => _ProfileEditorState();
}

class _ProfileEditorState extends State<_ProfileEditor> {
  final _input = TextEditingController(
    text: const JsonEncoder.withIndent('  ').convert({
      'id': 'new-server',
      'transport': 'http',
      'url': '',
      'allow_remote': false,
      'enabled': false,
      'allowed_tools': <String>[],
      'content_scope': 'local_only',
    }),
  );
  bool _reviewed = false;
  String? _error;

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  void _save() {
    try {
      if (utf8.encode(_input.text).length > 262144) {
        throw const FormatException();
      }
      final value = jsonDecode(_input.text);
      if (value is! Map<String, dynamic> || value['enabled'] != false) {
        throw const FormatException();
      }
      Navigator.pop(context, value.cast<String, Object?>());
    } catch (_) {
      setState(
        () => _error =
            'Enter a valid JSON profile with enabled: false, within 256 KiB.',
      );
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: const Text('Add or replace MCP profile'),
    content: SizedBox(
      width: 620,
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text(
              'Use env:NAME references for secrets; never enter credential values. '
              'HTTP needs the network grant and allow_remote: true; stdio needs the shell grant. '
              'Profiles are saved disabled. Enabling and connection testing are separate owner actions.',
            ),
            TextField(
              controller: _input,
              minLines: 8,
              maxLines: 14,
              maxLength: 262144,
              decoration: InputDecoration(
                labelText: 'MCP configuration JSON',
                errorText: _error,
                border: const OutlineInputBorder(),
              ),
            ),
            CheckboxListTile(
              value: _reviewed,
              onChanged: (value) => setState(() => _reviewed = value ?? false),
              title: const Text(
                'I reviewed the endpoint, permissions, disclosure scope and secret references.',
              ),
            ),
          ],
        ),
      ),
    ),
    actions: [
      TextButton(
        onPressed: () => Navigator.pop(context),
        child: const Text('Cancel'),
      ),
      FilledButton(
        onPressed: _reviewed ? _save : null,
        child: const Text('Save disabled profile'),
      ),
    ],
  );
}
