import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/library_models.dart';
import '../state/app_state.dart';

enum LibrarySection { projects, documents, media }

class _ProjectChange {
  _ProjectChange(this.body);
  final Map<String, Object?> body;
  int inFlight = 0;
  String? notice;
}

final _projectChanges = <String, _ProjectChange>{};
final _projectChangeUpdates = ValueNotifier(0);

/// Native records only. Selecting a library never reads workspace files.
class LibraryWorkspace extends StatefulWidget {
  const LibraryWorkspace({
    super.key,
    required this.state,
    required this.section,
    required this.onOpenAgent,
  });
  final AppState state;
  final LibrarySection section;
  final VoidCallback onOpenAgent;
  @override
  State<LibraryWorkspace> createState() => _LibraryWorkspaceState();
}

class _LibraryWorkspaceState extends State<LibraryWorkspace> {
  final _search = TextEditingController(), _name = TextEditingController();
  final _outputs = <GeneratedOutput>[], _projects = <AgentProject>[];
  int _read = 0, _connection = -1;
  Object? _cursor;
  bool _loading = false;
  String? _error, _notice;
  String get _kind =>
      widget.section == LibrarySection.documents ? 'documents' : 'media';
  bool get _isProjects => widget.section == LibrarySection.projects;
  _ProjectChange? get _pending =>
      _projectChanges[widget.state.client?.agentBase];

  @override
  void initState() {
    super.initState();
    widget.state.addListener(_changed);
    _projectChangeUpdates.addListener(_pendingChanged);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _refresh();
    });
  }

  @override
  void didUpdateWidget(covariant LibraryWorkspace oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.state, widget.state)) {
      oldWidget.state.removeListener(_changed);
      widget.state.addListener(_changed);
      _connection = -1;
    }
    if (oldWidget.section != widget.section ||
        _connection != widget.state.connectionGeneration) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _refresh();
      });
    }
  }

  @override
  void dispose() {
    _read++;
    widget.state.removeListener(_changed);
    _projectChangeUpdates.removeListener(_pendingChanged);
    _search.dispose();
    _name.dispose();
    super.dispose();
  }

  void _changed() {
    if (!mounted) return;
    if (_connection != widget.state.connectionGeneration) {
      _refresh();
    } else {
      setState(() {});
    }
  }

  void _pendingChanged() {
    if (!mounted || !_isProjects) return;
    setState(() {});
    if (_pending == null) unawaited(_refresh());
  }

  Future<void> _refresh({bool more = false}) async {
    final owner = widget.state,
        api = widget.state.client,
        section = widget.section;
    final generation = owner.connectionGeneration, read = ++_read;
    _connection = generation;
    setState(() {
      _error = null;
      _loading = api != null && owner.sessionAuthenticated;
      if (!more) {
        _outputs.clear();
        _projects.clear();
        _cursor = null;
      }
    });
    if (!_loading || api == null) return;
    bool current() =>
        mounted &&
        identical(owner, widget.state) &&
        read == _read &&
        owner.ownsConnection(generation, api) &&
        section == widget.section;
    try {
      if (_isProjects) {
        final page = await api.projects(
          after: more ? _cursor as String? : null,
        );
        if (!current()) return;
        if (more &&
            page.items.any(
              (row) => _projects.any((prior) => row.id == prior.id),
            )) {
          throw const FormatException();
        }
        _projects.addAll(page.items);
        _cursor = page.nextCursor;
      } else {
        final page = await api.generatedOutputs(
          kind: _kind,
          after: more ? _cursor as int? ?? 0 : 0,
        );
        if (!current()) return;
        _outputs.addAll(page.items);
        _cursor = page.nextCursor;
      }
    } catch (_) {
      if (current()) {
        _error = 'These records could not be read. Refresh to try again.';
      }
    } finally {
      if (current()) setState(() => _loading = false);
    }
  }

  Future<void> _openSession(String id) async {
    final owner = widget.state,
        api = widget.state.client,
        generation = widget.state.connectionGeneration;
    if (api == null || !owner.sessionAuthenticated) return;
    await owner.selectAgentSession(id);
    if (!mounted ||
        !identical(owner, widget.state) ||
        !owner.ownsConnection(generation, api)) {
      return;
    }
    if (owner.agentSession?.sessionId == id &&
        owner.agentHistoryError == null) {
      widget.onOpenAgent();
    } else {
      setState(() => _error = 'That Agent session could not be opened.');
    }
  }

  Future<void> _change(Map<String, Object?> body) async {
    final api = widget.state.client;
    if (api == null || !widget.state.sessionAuthenticated || _pending != null) {
      return;
    }
    if (_projectChanges.length >= 8) {
      setState(
        () =>
            _error = 'Resolve earlier project changes before creating another.',
      );
      return;
    }
    final operation = _ProjectChange({
      'schema_version': '1.0',
      'idempotency_key': newIdempotencyKey(),
      ...body,
    });
    _projectChanges[api.agentBase] = operation;
    _projectChangeUpdates.value++;
    await _send(api, operation);
  }

  Future<void> _send(KinaiyaClient api, _ProjectChange operation) async {
    if (operation.inFlight >= 2 ||
        !identical(_projectChanges[api.agentBase], operation)) {
      return;
    }
    operation.inFlight++;
    operation.notice = null;
    _projectChangeUpdates.value++;
    try {
      final receipt = await api.changeProject(operation.body);
      final project = AgentProject.fromJson(receipt['project']);
      final body = operation.body;
      if (receipt['schema_version'] != '1.0' ||
          receipt['idempotency_key'] != body['idempotency_key'] ||
          receipt['operation'] != body['operation'] ||
          project.revision !=
              (body['expected_revision'] == null
                  ? 1
                  : (body['expected_revision'] as int) + 1) ||
          (body['project_id'] != null && project.id != body['project_id']) ||
          (body['name'] != null &&
              project.name != (body['name'] as String).trim()) ||
          (body['session_id'] != null &&
              project.sessionIds.contains(body['session_id']) !=
                  (body['operation'] != 'detach'))) {
        throw const FormatException();
      }
      if (identical(_projectChanges[api.agentBase], operation)) {
        _projectChanges.remove(api.agentBase);
      }
      if (mounted && identical(widget.state.client, api)) {
        _notice = 'Project saved.';
        if (body['operation'] == 'create' &&
            _name.text.trim() == body['name']) {
          _name.clear();
        }
      }
    } on ApiException catch (error) {
      if ({400, 401, 403, 404, 409, 422}.contains(error.status) &&
          operation.inFlight == 1) {
        if (identical(_projectChanges[api.agentBase], operation)) {
          _projectChanges.remove(api.agentBase);
        }
        if (mounted && identical(widget.state.client, api)) {
          setState(
            () => _notice =
                'Project change refused. Refresh before editing again.',
          );
        }
      } else {
        operation.notice =
            'The change is unconfirmed. Retry the same request to recover its receipt.';
      }
    } catch (_) {
      operation.notice =
          'The change is unconfirmed. Retry the same request to recover its receipt.';
    } finally {
      operation.inFlight--;
      _projectChangeUpdates.value++;
    }
  }

  String _date(DateTime value) {
    final local = value.toLocal();
    String two(int part) => '$part'.padLeft(2, '0');
    return '${local.year}-${two(local.month)}-${two(local.day)} ${two(local.hour)}:${two(local.minute)}';
  }

  Widget _project(AgentProject project) {
    final current = widget.state.agentSession;
    final included =
        current != null && project.sessionIds.contains(current.sessionId);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(project.name, style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 6),
            Text(
              'Created ${_date(project.createdAt)} · ${project.sessionIds.length} sessions',
            ),
            const SizedBox(height: 12),
            for (final id in project.sessionIds)
              TextButton.icon(
                onPressed: () => _openSession(id),
                icon: const Icon(Icons.chat_bubble_outline),
                label: Text('Open Agent session $id'),
              ),
            if (current != null)
              OutlinedButton.icon(
                onPressed: _pending != null
                    ? null
                    : () => _change({
                        'operation': included ? 'detach' : 'attach',
                        'project_id': project.id,
                        'expected_revision': project.revision,
                        'session_id': current.sessionId,
                      }),
                icon: Icon(included ? Icons.remove : Icons.add),
                label: Text(
                  included
                      ? 'Remove current session from project'
                      : 'Add current Agent session',
                ),
              ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final connected =
        widget.state.client != null && widget.state.sessionAuthenticated;
    final query = _search.text.toLowerCase();
    final outputs =
        _outputs
            .where(
              (row) =>
                  '${row.name} ${row.type} ${row.path ?? ''} ${row.workspaceStatus ?? ''}'
                      .toLowerCase()
                      .contains(query),
            )
            .toList()
          ..sort((a, b) => b.createdAt.compareTo(a.createdAt));
    final projects =
        _projects
            .where((row) => row.name.toLowerCase().contains(query))
            .toList()
          ..sort(
            (a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()),
          );
    final title = _isProjects
        ? 'Projects'
        : widget.section == LibrarySection.documents
        ? 'Generated Documents'
        : 'Generated Media';
    final pending = _pending;
    return Material(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Align(
          alignment: Alignment.topLeft,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 1200),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        title,
                        style: Theme.of(context).textTheme.headlineMedium,
                      ),
                    ),
                    IconButton(
                      tooltip: 'Refresh records',
                      onPressed: connected && !_loading
                          ? () => _refresh()
                          : null,
                      icon: const Icon(Icons.refresh),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  _isProjects
                      ? 'Organize Agent sessions into projects.'
                      : 'Outputs recorded by Agent. Creation times use your local time.',
                ),
                const SizedBox(height: 20),
                TextField(
                  controller: _search,
                  onChanged: (_) => setState(() {}),
                  decoration: InputDecoration(
                    prefixIcon: const Icon(Icons.search),
                    labelText: _isProjects
                        ? 'Search projects'
                        : 'Search loaded outputs',
                  ),
                ),
                if (_isProjects) ...[
                  const SizedBox(height: 16),
                  Wrap(
                    spacing: 12,
                    runSpacing: 12,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      SizedBox(
                        width: 320,
                        child: TextField(
                          controller: _name,
                          maxLength: 120,
                          decoration: const InputDecoration(
                            labelText: 'Project name',
                          ),
                          onChanged: (_) => setState(() {}),
                        ),
                      ),
                      FilledButton.icon(
                        onPressed:
                            !connected ||
                                pending != null ||
                                _name.text.trim().isEmpty
                            ? null
                            : () => _change({
                                'operation': 'create',
                                'name': _name.text.trim(),
                              }),
                        icon: const Icon(Icons.add),
                        label: const Text('Create project'),
                      ),
                    ],
                  ),
                  if (pending != null) ...[
                    const SizedBox(height: 12),
                    if (pending.inFlight > 0) const LinearProgressIndicator(),
                    Text(pending.notice ?? 'Saving project change…'),
                    if (pending.notice != null)
                      Align(
                        alignment: Alignment.centerLeft,
                        child: OutlinedButton(
                          onPressed: !connected || pending.inFlight >= 2
                              ? null
                              : () => _send(widget.state.client!, pending),
                          child: const Text('Retry same request'),
                        ),
                      ),
                  ],
                ],
                const SizedBox(height: 20),
                if (!connected)
                  const Text('Connect to your host to read these records.'),
                if (_loading) const LinearProgressIndicator(),
                if (_error != null)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 12),
                    child: Text(_error!),
                  ),
                if (_notice != null)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 12),
                    child: Text(_notice!),
                  ),
                if (connected &&
                    !_loading &&
                    _error == null &&
                    (_isProjects ? _projects.isEmpty : _outputs.isEmpty))
                  Text(
                    _isProjects
                        ? 'No projects yet. Create one to group Agent sessions.'
                        : 'No generated outputs in the loaded records.',
                  ),
                if (_isProjects) ...projects.map(_project),
                if (!_isProjects && outputs.isNotEmpty)
                  SingleChildScrollView(
                    scrollDirection: Axis.horizontal,
                    child: DataTable(
                      columns: const [
                        DataColumn(label: Text('Name')),
                        DataColumn(label: Text('File type')),
                        DataColumn(label: Text('Created')),
                        DataColumn(label: Text('Workspace')),
                        DataColumn(label: Text('Source')),
                      ],
                      rows: [
                        for (final output in outputs)
                          DataRow(
                            cells: [
                              DataCell(
                                Tooltip(
                                  message:
                                      output.path ??
                                      'Historical output with unknown workspace. No current-root path is inferred.',
                                  child: Text(output.name),
                                ),
                              ),
                              DataCell(Text(output.type)),
                              DataCell(Text(_date(output.createdAt))),
                              DataCell(
                                Tooltip(
                                  message: output.workspacePin == null
                                      ? 'This historical record has no workspace identity. It is metadata only; no file is opened or resolved against the current root.'
                                      : 'Workspace ${output.workspacePin!.workspaceId}\nRoot identity ${output.workspacePin!.rootIdentity}\nRevision ${output.workspacePin!.revision} · ${output.workspacePin!.source}\nRecorded path ${output.path}\nThis is receipt metadata, not a check that the file still exists.',
                                  child: Text(switch (output.workspaceStatus) {
                                    'current' => 'Matches active identity',
                                    'historical' =>
                                      'Recorded workspace identity',
                                    _ => 'Workspace unknown',
                                  }),
                                ),
                              ),
                              DataCell(
                                TextButton(
                                  onPressed: () =>
                                      _openSession(output.sessionId),
                                  child: const Text('Open Agent session'),
                                ),
                              ),
                            ],
                          ),
                      ],
                    ),
                  ),
                if (_cursor != null &&
                    connected &&
                    _outputs.length < 2000 &&
                    _projects.length < 500)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: TextButton(
                      onPressed: _loading ? null : () => _refresh(more: true),
                      child: const Text('Load more records'),
                    ),
                  ),
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    onPressed: widget.onOpenAgent,
                    icon: const Icon(Icons.chat_bubble_outline),
                    label: const Text('Open Agent Chat'),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
