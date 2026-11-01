import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../api/tool_permissions.dart';
import '../api/tool_permissions_client.dart';
import 'design_tokens.dart';

class OptionsToolPermissions extends StatefulWidget {
  const OptionsToolPermissions({
    super.key,
    required this.api,
    required this.active,
    this.currentSessionId,
    this.currentTaskIds = const [],
  });
  final ToolPermissionsApi? api;
  final bool Function() active;
  final String? currentSessionId;
  final List<String> currentTaskIds;
  @override
  State<OptionsToolPermissions> createState() => _OptionsToolPermissionsState();
}

class _PermissionDraft {
  _PermissionDraft(this.base)
    : modes = {for (final row in base.tools) row.tool: row.override};
  final ToolPermissionsSnapshot base;
  final Map<String, String?> modes;
  bool get dirty => base.tools.any((row) => modes[row.tool] != row.override);
}

class _PendingPermissionSave {
  _PendingPermissionSave(this.owner, this.snapshot, this.body);
  final ToolPermissionsApi owner;
  final ToolPermissionsSnapshot snapshot;
  final Map<String, Object?> body;
  bool inFlight = false, dispatched = false, confirmed = false;
  ToolPermissionsApi? fenceOwner;
  int? fenceRevision;
  String notice = 'Saving exact tool rule…';
  String get key => body['idempotency_key'] as String;
}

class _OptionsToolPermissionsState extends State<OptionsToolPermissions> {
  // Exact unknown operations only. Owner credentials and bodies stay in RAM.
  // Do not silently evict a scope to admit another write.
  static final _pending = <_PendingPermissionSave>[];
  static final _changes = ValueNotifier<int>(0);
  final _session = TextEditingController(), _task = TextEditingController();
  final _drafts = <String, _PermissionDraft>{};
  List<_PendingPermissionSave> _observed = [];
  List<SavedToolPermissionScope> _saved = [];
  String _kind = 'default';
  String? _savedId, _nextSaved, _error, _notice, _savedError;
  String? _resolvedSession, _resolvedTask;
  ToolPermissionsSnapshot? _snapshot;
  bool _reading = false, _readingSaved = false;
  int _read = 0, _savedRead = 0;

  List<_PendingPermissionSave> get _ownerPending {
    final api = widget.api;
    return api == null
        ? []
        : _pending.where((item) => item.owner.sameOwner(api)).toList();
  }

  _PendingPermissionSave? get _scopePending {
    final scope = _snapshot?.scopeId;
    for (final item in _ownerPending) {
      if (item.snapshot.scopeId == scope) return item;
    }
    return null;
  }

  bool _current(ToolPermissionsApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();

  @override
  void initState() {
    super.initState();
    _observed = _ownerPending;
    _changes.addListener(_pendingChanged);
    unawaited(_loadScope());
    unawaited(_loadSaved());
  }

  @override
  void didUpdateWidget(covariant OptionsToolPermissions oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api)) {
      _read++;
      _savedRead++;
      _snapshot = null;
      _saved = [];
      _drafts.clear();
      _kind = 'default';
      _savedId = null;
      _nextSaved = null;
      _resolvedSession = _resolvedTask = null;
      _session.clear();
      _task.clear();
      _observed = _ownerPending;
      unawaited(_loadScope());
      unawaited(_loadSaved());
    }
  }

  @override
  void dispose() {
    _read++;
    _savedRead++;
    _changes.removeListener(_pendingChanged);
    _session.dispose();
    _task.dispose();
    super.dispose();
  }

  void _pendingChanged() {
    if (!mounted || !widget.active()) return;
    final prior = _observed;
    _observed = _ownerPending;
    setState(() {});
    if (prior.any((item) => item.confirmed && !_pending.contains(item))) {
      setState(
        () => _notice =
            'Exact save confirmed. Refresh effective values; a receipt is historical.',
      );
      unawaited(_loadScope());
      unawaited(_loadSaved());
    }
  }

  Future<void> _loadScope({bool resolve = false}) async {
    final api = widget.api, active = widget.active, read = ++_read;
    if (api == null || !widget.active()) return;
    if (resolve) {
      _savedId = null;
      _resolvedSession = _kind == 'session' ? _session.text : null;
      _resolvedTask = _kind == 'task' ? _task.text : null;
    }
    final kind = _kind, saved = _savedId;
    final session = kind == 'session' ? _resolvedSession : null;
    final task = kind == 'task' ? _resolvedTask : null;
    if (saved == null &&
        ((kind == 'session' && (session == null || session.isEmpty)) ||
            (kind == 'task' && (task == null || task.isEmpty)))) {
      setState(() {
        _snapshot = null;
        _reading = false;
      });
      return;
    }
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final snapshot = await api.inspect(
        kind: saved == null ? kind : 'default',
        scopeId: saved,
        sessionId: saved == null ? session : null,
        taskId: saved == null ? task : null,
      );
      if (!_current(api, active) || read != _read) return;
      setState(() {
        _snapshot = snapshot;
        final draft = _drafts[snapshot.scopeId];
        if (draft == null ||
            !draft.dirty ||
            snapshot.tools.every(
              (row) => draft.modes[row.tool] == row.override,
            )) {
          if (_drafts.length < 32 || draft != null) {
            _drafts[snapshot.scopeId] = _PermissionDraft(snapshot);
          }
        }
        _observed = _ownerPending;
      });
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(() {
          _snapshot = null;
          _error =
              'Native scope could not be read. No permission was changed. Check the ID and owner connection.';
        });
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  Future<void> _loadSaved({bool more = false}) async {
    final api = widget.api, active = widget.active, read = ++_savedRead;
    if (api == null || !widget.active() || (more && _nextSaved == null)) return;
    final after = more ? _nextSaved : null;
    setState(() {
      _readingSaved = true;
      _savedError = null;
    });
    try {
      final page = await api.savedScopes(after: after);
      if (!_current(api, active) || read != _savedRead) return;
      final rows = more ? [..._saved, ...page.scopes] : page.scopes;
      if (rows.length > 1024 ||
          rows.map((row) => row.scopeId).toSet().length != rows.length) {
        throw const FormatException();
      }
      setState(() {
        _saved = rows;
        _nextSaved = page.nextAfter;
      });
    } catch (_) {
      if (_current(api, active) && read == _savedRead) {
        setState(
          () => _savedError =
              'Saved scope inventory could not be read. Retained requests are unchanged.',
        );
      }
    } finally {
      if (_current(api, active) && read == _savedRead) {
        setState(() => _readingSaved = false);
      }
    }
  }

  void _begin(
    ToolPermissionsSnapshot expected,
    ToolPermissionRow row,
    String? mode,
  ) {
    final api = widget.api, snapshot = _snapshot;
    if (!mounted ||
        api == null ||
        snapshot == null ||
        !identical(snapshot, expected) ||
        !widget.active() ||
        _reading ||
        _scopePending != null ||
        _pending.length >= 16) {
      return;
    }
    final draft = _drafts[snapshot.scopeId];
    if (draft == null ||
        draft.base.revision != snapshot.revision ||
        draft.base.stateToken != snapshot.stateToken ||
        (!snapshot.scopeLive && mode != null)) {
      return;
    }
    try {
      draft.modes[row.tool] = mode;
      final item = _PendingPermissionSave(
        api,
        snapshot,
        snapshot.change(row.tool, mode, const Uuid().v4()),
      );
      _pending.add(item);
      _changes.value++;
      if (!_current(api, widget.active)) {
        _pending.remove(item);
        _changes.value++;
        return;
      }
      unawaited(_send(item, api));
    } catch (_) {
      if (mounted && widget.active()) {
        setState(
          () => _error =
              'This tool rule was not dispatched. Review its native scope and bounds.',
        );
      }
    }
  }

  Future<void> _send(
    _PendingPermissionSave item,
    ToolPermissionsApi api, {
    bool receiptOnly = false,
    bool releaseFenced = false,
  }) async {
    if (!api.sameOwner(item.owner) ||
        item.inFlight ||
        !_pending.contains(item)) {
      return;
    }
    final active = widget.active;
    item.inFlight = true;
    item.notice = receiptOnly
        ? 'Checking the exact historical receipt…'
        : 'Saving the exact original rule…';
    _changes.value++;
    try {
      // Listeners may reconnect during the notification. Never begin an old
      // view's new operation on a replacement connection.
      if (!_current(api, active)) {
        if (!receiptOnly && !item.dispatched) _pending.remove(item);
        return;
      }
      final receipt = receiptOnly
          ? await api.receipt(item.key)
          : await api.change(item.body, item.snapshot.scopeId);
      if (!receipt.matches(item.body, item.snapshot.scopeId)) {
        throw const FormatException();
      }
      item.confirmed = true;
      _pending.remove(item);
      if (_current(api, active)) {
        setState(() {
          _notice =
              'Rule save confirmed at revision ${receipt.revision}. Current effective behavior is shown after refresh.';
        });
      }
    } on ToolPermissionFailure catch (error) {
      item.dispatched = item.dispatched || error.dispatched;
      if (!receiptOnly && !item.dispatched) {
        // Locally refused before any POST; no delayed save can originate here.
        _pending.remove(item);
        if (_current(api, active)) {
          setState(
            () =>
                _error = 'The rule was not dispatched. Your draft is retained.',
          );
        }
      } else {
        item.notice = receiptOnly && error.status == 404
            ? 'No committed receipt is currently visible. The save outcome remains unknown.'
            : 'The save outcome remains unknown. Check its exact receipt or retry the unchanged rule.';
        if (receiptOnly) {
          await _checkFence(item, api, active, release: releaseFenced);
        }
      }
    } catch (_) {
      item.dispatched = true;
      item.notice =
          'The save response could not be confirmed. Keep the original scope, body and key for reconciliation.';
      if (receiptOnly) {
        await _checkFence(item, api, active, release: releaseFenced);
      }
    } finally {
      item.inFlight = false;
      _changes.value++;
      if (_current(api, active)) setState(() {});
    }
  }

  Future<void> _checkFence(
    _PendingPermissionSave item,
    ToolPermissionsApi api,
    bool Function() active, {
    required bool release,
  }) async {
    if (!_current(api, active) || !_pending.contains(item)) return;
    item.fenceOwner = null;
    item.fenceRevision = null;
    try {
      // A default snapshot observes the global native revision even if the
      // original session/task scope has retired. No rule is applied here.
      final fresh = await api.inspect();
      if (!_current(api, active) || !_pending.contains(item)) return;
      if (fresh.instanceId != item.snapshot.instanceId ||
          fresh.revision <= (item.body['expected_revision'] as int)) {
        item.notice =
            'The earlier save’s outcome remains unknown. This connection has not confirmed that it can no longer change permissions.';
        return;
      }
      item.fenceOwner = api;
      item.fenceRevision = fresh.revision;
      if (release) {
        // This releases only the owner's RAM tracker. Any real old HTTP
        // operation retains its independent global transport ownership slot.
        _pending.remove(item);
        setState(
          () => _notice =
              'Local blocking released at host revision ${fresh.revision}. The earlier save’s outcome remains unknown; this request can no longer change permissions on this host. Review current rules. No work was cancelled or rolled back.',
        );
        unawaited(_loadScope());
        unawaited(_loadSaved());
      } else {
        item.notice =
            'The earlier save’s outcome remains unknown. The same host has advanced to revision ${fresh.revision}; this request can no longer change permissions. You may release its local blocking.';
      }
    } catch (_) {
      if (_current(api, active) && _pending.contains(item)) {
        item.notice =
            'The save outcome remains unknown. The host could not confirm that the earlier request can no longer change permissions.';
      }
    }
  }

  Widget _pendingCard(_PendingPermissionSave item) => Card(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Unconfirmed tool rule · ${item.body['tool']}',
            style: Theme.of(context).textTheme.titleSmall,
          ),
          Text(item.snapshot.scope.label),
          Text(item.notice),
          ExpansionTile(
            title: const Text('Original save identity'),
            children: [
              SelectableText(
                'Native instance ${item.snapshot.instanceId}\nScope ${item.snapshot.scopeId}\n${const JsonEncoder.withIndent('  ').convert(item.body)}',
              ),
            ],
          ),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              TextButton(
                onPressed: item.inFlight || !widget.active()
                    ? null
                    : () => unawaited(
                        _send(item, widget.api!, receiptOnly: true),
                      ),
                child: const Text('Check exact receipt'),
              ),
              OutlinedButton(
                onPressed: item.inFlight || !widget.active()
                    ? null
                    : () => unawaited(_send(item, widget.api!)),
                child: const Text('Retry exact save'),
              ),
              if (item.fenceRevision != null &&
                  identical(item.fenceOwner, widget.api))
                TextButton(
                  onPressed: item.inFlight || !widget.active()
                      ? null
                      : () => unawaited(
                          _send(
                            item,
                            widget.api!,
                            receiptOnly: true,
                            releaseFenced: true,
                          ),
                        ),
                  child: const Text('Release fenced request'),
                ),
            ],
          ),
        ],
      ),
    ),
  );

  static String _label(String mode) => switch (mode) {
    'allow' => 'Allow',
    'ask' => 'Ask',
    _ => 'Deny',
  };
  static String _title(String tool) => switch (tool) {
    'read_file' => 'Read file tool',
    'write_file' => 'Write file tool',
    _ => 'Shell tool',
  };
  Widget _toolCard(ToolPermissionsSnapshot snapshot, ToolPermissionRow row) {
    final draft = _drafts[snapshot.scopeId];
    final stale =
        draft != null &&
        (draft.base.revision != snapshot.revision ||
            draft.base.stateToken != snapshot.stateToken);
    final pending = _scopePending;
    final otherDirty = snapshot.tools.any(
      (other) =>
          other.tool != row.tool && draft?.modes[other.tool] != other.override,
    );
    final editable =
        !otherDirty &&
        widget.active() &&
        !_reading &&
        pending == null &&
        !stale &&
        draft != null &&
        _pending.length < 16 &&
        snapshot.revision < permissionRevisionLimit;
    final mode = draft?.modes[row.tool];
    final changed = mode != row.override;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              _title(row.tool),
              style: Theme.of(context).textTheme.titleMedium,
            ),
            Text(switch (row.tool) {
              'read_file' =>
                'Controls read_file only. Search, shell and context selection can read through separate native owners.',
              'write_file' =>
                'Controls write_file only. A permitted shell or another tool may write through its own native policy.',
              _ =>
                'Controls shell action approval. It does not grant network access or remove the required sandbox.',
            }),
            const SizedBox(height: 12),
            if (otherDirty)
              const Text(
                'Save or discard the changed tool choice before editing another tool in this scope.',
              ),
            DropdownButtonFormField<String>(
              key: ValueKey((
                snapshot.scopeId,
                row.tool,
                mode,
                draft?.base.revision,
              )),
              initialValue: mode ?? 'inherit',
              isExpanded: true,
              decoration: const InputDecoration(
                labelText: 'Override for this scope',
                border: OutlineInputBorder(),
              ),
              items: [
                const DropdownMenuItem(
                  value: 'inherit',
                  child: Text('Inherit'),
                ),
                for (final value in permissionModes)
                  DropdownMenuItem(
                    value: value,
                    enabled: snapshot.scopeLive,
                    child: Text(_label(value)),
                  ),
              ],
              onChanged: editable && snapshot.scopeLive
                  ? (value) {
                      if (value != null) {
                        setState(
                          () => draft.modes[row.tool] = value == 'inherit'
                              ? null
                              : value,
                        );
                      }
                    }
                  : null,
            ),
            const SizedBox(height: 12),
            Text(
              'Saved override: ${row.override == null ? 'Inherit' : _label(row.override!)}',
            ),
            Text('Effective: ${_label(row.effective)} · Source: ${row.source}'),
            Text(
              'Host behavior: ${_label(row.hostMode)}${row.hardDenied ? ' · Hard host Deny ceiling' : ''}',
            ),
            if (row.ruleId != null)
              SelectableText(
                'Rule ${row.ruleId} · revision ${row.ruleRevision}\nSource scope ${row.sourceScopeId}',
              ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              children: [
                FilledButton(
                  onPressed: editable && snapshot.scopeLive && changed
                      ? () => _begin(snapshot, row, mode)
                      : null,
                  child: const Text('Save override'),
                ),
                TextButton(
                  onPressed: editable && row.override != null
                      ? () => _begin(snapshot, row, null)
                      : null,
                  child: const Text('Reset this override'),
                ),
                if (changed)
                  TextButton(
                    onPressed: pending != null || !widget.active()
                        ? null
                        : () => setState(
                            () => _drafts[snapshot.scopeId] = _PermissionDraft(
                              snapshot,
                            ),
                          ),
                    child: const Text('Discard change'),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final snapshot = _snapshot;
    final draft = snapshot == null ? null : _drafts[snapshot.scopeId];
    final stale =
        snapshot != null &&
        draft != null &&
        (snapshot.revision != draft.base.revision ||
            snapshot.stateToken != draft.base.stateToken);
    return DesignPanel(
      child: Material(
        type: MaterialType.transparency,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Scoped Agent tool rules',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 8),
            const Text(
              'Allow, Ask or Deny for three exact native tools. Task overrides Session, then Workspace, Default and host behavior. Hard host Deny, containment, disclosure and action-specific speech approval remain independent ceilings.',
            ),
            const SizedBox(height: 16),
            if (widget.api == null || !widget.active())
              const Text(
                'Connect as the owner to inspect native tool permissions.',
              ),
            DropdownButtonFormField<String>(
              key: ValueKey(('permission-scope-kind', _kind)),
              initialValue: _kind,
              decoration: const InputDecoration(
                labelText: 'Rule scope',
                border: OutlineInputBorder(),
              ),
              items: [
                for (final kind in permissionScopeKinds)
                  DropdownMenuItem(
                    value: kind,
                    child: Text(kind[0].toUpperCase() + kind.substring(1)),
                  ),
              ],
              onChanged: !widget.active()
                  ? null
                  : (value) {
                      if (value == null) return;
                      setState(() {
                        _kind = value;
                        _savedId = null;
                        _snapshot = null;
                        _error = null;
                      });
                      unawaited(_loadScope());
                    },
            ),
            if (_kind == 'session' || _kind == 'task') ...[
              const SizedBox(height: 12),
              TextField(
                controller: _kind == 'session' ? _session : _task,
                maxLength: 128,
                enabled: widget.active(),
                decoration: InputDecoration(
                  labelText: _kind == 'session'
                      ? 'Native session ID'
                      : 'Native task ID',
                  helperText:
                      'Resolve on the host to inspect its canonical scope. Nothing is saved by resolving.',
                  helperMaxLines: 3,
                  border: const OutlineInputBorder(),
                ),
              ),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  OutlinedButton(
                    onPressed: !widget.active() || _reading
                        ? null
                        : () => unawaited(_loadScope(resolve: true)),
                    child: const Text('Resolve native scope'),
                  ),
                  if (_kind == 'session' && widget.currentSessionId != null)
                    TextButton(
                      onPressed: !widget.active()
                          ? null
                          : () {
                              _session.text = widget.currentSessionId!;
                              unawaited(_loadScope(resolve: true));
                            },
                      child: const Text('Use selected Agent session'),
                    ),
                ],
              ),
              if (_kind == 'task' && widget.currentTaskIds.isNotEmpty)
                DropdownButtonFormField<String>(
                  decoration: const InputDecoration(
                    labelText: 'Known native task (ID hint)',
                  ),
                  items: [
                    for (final id in widget.currentTaskIds.take(1024).toSet())
                      DropdownMenuItem(
                        value: id,
                        child: Text(id, overflow: TextOverflow.ellipsis),
                      ),
                  ],
                  onChanged: !widget.active()
                      ? null
                      : (id) {
                          if (id != null) {
                            _task.text = id;
                            unawaited(_loadScope(resolve: true));
                          }
                        },
                ),
            ],
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              children: [
                TextButton.icon(
                  onPressed: !widget.active() || _reading
                      ? null
                      : () => unawaited(_loadScope()),
                  icon: const Icon(Icons.refresh),
                  label: const Text('Refresh effective rules'),
                ),
                TextButton(
                  onPressed: !widget.active() || _readingSaved
                      ? null
                      : () => unawaited(_loadSaved()),
                  child: const Text('Refresh saved scopes'),
                ),
              ],
            ),
            if (_reading) const LinearProgressIndicator(),
            if (_error != null) Text(_error!),
            if (_notice != null) Text(_notice!),
            for (final item in _ownerPending) _pendingCard(item),
            if (_pending.length >= 16)
              const Text(
                'The bounded local save limit is reached. Reconcile retained exact requests; no unknown request is discarded.',
              ),
            if (snapshot != null) ...[
              const SizedBox(height: 12),
              Text(
                snapshot.scope.label,
                style: Theme.of(context).textTheme.titleMedium,
              ),
              SelectableText(
                'Native scope ${snapshot.scopeId}\nPermission revision ${snapshot.revision}',
              ),
              if (snapshot.scope.sessionId != null)
                SelectableText(
                  'Session ${snapshot.scope.sessionId}\nThread ${snapshot.scope.threadId} · epoch ${snapshot.scope.epoch}',
                ),
              if (!snapshot.scopeLive)
                const Text(
                  'This native scope is retired. Only reset of its saved overrides is available; it cannot grant a new action.',
                ),
              if (draft == null)
                const Text(
                  'Local draft capacity reached. Existing unsaved drafts are retained; close and reopen Options to discard local drafts.',
                ),
              if (stale) ...[
                const Text(
                  'Native rules changed while this draft was open. Your draft is retained. Review current values before saving.',
                ),
                TextButton(
                  onPressed: _scopePending != null
                      ? null
                      : () => setState(
                          () => _drafts[snapshot.scopeId] = _PermissionDraft(
                            snapshot,
                          ),
                        ),
                  child: const Text('Use current values'),
                ),
              ],
              for (final row in snapshot.tools) _toolCard(snapshot, row),
            ],
            const SizedBox(height: 16),
            Text(
              'Saved native scopes',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const Text(
              'Includes scopes with saved rules after their session or task revision retires. Opening a scope only inspects it.',
            ),
            if (_savedError != null) Text(_savedError!),
            if (_readingSaved) const LinearProgressIndicator(),
            if (_saved.isEmpty && !_readingSaved)
              const Text('No saved scopes are currently visible.'),
            for (final row in _saved)
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(row.scope.label),
                subtitle: Text(row.scopeId),
                trailing: const Icon(Icons.chevron_right),
                onTap: !widget.active()
                    ? null
                    : () {
                        setState(() {
                          _savedId = row.scopeId;
                          _snapshot = null;
                          _kind = row.scope.kind;
                          _session.text = row.scope.sessionId ?? '';
                          _task.text = row.scope.taskId ?? '';
                          _resolvedSession = row.scope.sessionId;
                          _resolvedTask = row.scope.taskId;
                        });
                        unawaited(_loadScope());
                      },
              ),
            if (_nextSaved != null)
              TextButton(
                onPressed: _readingSaved || !widget.active()
                    ? null
                    : () => unawaited(_loadSaved(more: true)),
                child: const Text('Load more saved scopes'),
              ),
            const SizedBox(height: 12),
            const Text(
              'Saved Deny affects subsequent action admissions. Already admitted or running work may continue; this editor does not report cancellation or rollback. Other native tools, individual MCP/plugin rules and scoped network controls require separate native owners.',
            ),
          ],
        ),
      ),
    );
  }
}
