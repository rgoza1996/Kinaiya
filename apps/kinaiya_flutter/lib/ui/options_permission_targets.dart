import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';
import '../api/permission_targets.dart';
import '../api/tool_permissions.dart';
import '../api/tool_permissions_client.dart';

class OptionsPermissionTargets extends StatefulWidget {
  const OptionsPermissionTargets({
    super.key,
    required this.api,
    required this.active,
    required this.scope,
    this.savedScopeId,
    required this.onChanged,
  });
  final ToolPermissionsApi? api;
  final bool Function() active;
  final ToolPermissionsSnapshot scope;
  final String? savedScopeId;
  final VoidCallback onChanged;
  @override
  State<OptionsPermissionTargets> createState() =>
      _OptionsPermissionTargetsState();
}

class _PendingTarget {
  _PendingTarget(this.owner, this.snapshot, this.body);
  final ToolPermissionsApi owner;
  final PermissionTargetsPage snapshot;
  final Map<String, Object?> body;
  bool busy = false, dispatched = false;
  ToolPermissionsApi? fenceOwner;
  int? fenceRevision;
  String notice = 'Saving exact target rule…';
  String get key => body['idempotency_key'] as String;
  String get label => PermissionTarget.fromJson(body['target']).label;
}

class _TargetDraft {
  _TargetDraft(this.owner, this.snapshot, this.row, this.mode);
  final ToolPermissionsApi owner;
  final PermissionTargetsPage snapshot;
  final PermissionTargetRow row;
  String? mode;
}

class _OptionsPermissionTargetsState extends State<OptionsPermissionTargets> {
  static final _pending = <_PendingTarget>[];
  static final _drafts = <_TargetDraft>[];
  static final _changed = ValueNotifier<int>(0);
  PermissionTargetsPage? _page;
  List<PermissionTargetRow> _rows = [];
  final _choices = <String, String?>{};
  String? _next, _error, _notice;
  bool _reading = false;
  int _read = 0;

  List<_PendingTarget> get _owned => widget.api == null
      ? []
      : _pending.where((p) => p.owner.sameOwner(widget.api!)).toList();
  List<_TargetDraft> get _ownedDrafts => widget.api == null
      ? []
      : _drafts.where((draft) => draft.owner.sameOwner(widget.api!)).toList();
  _TargetDraft? _draft(PermissionTargetsPage page, PermissionTargetRow row) {
    for (final draft in _ownedDrafts) {
      if (draft.snapshot.scopeId == page.scopeId &&
          draft.row.target.matches(row.target)) {
        return draft;
      }
    }
    return null;
  }

  bool _sameBinding(
    PermissionTargetBinding? left,
    PermissionTargetBinding? right,
  ) => left == null ? right == null : right != null && left.matches(right);

  bool _draftStale(
    _TargetDraft draft,
    PermissionTargetsPage page,
    PermissionTargetRow row,
  ) =>
      draft.snapshot.instanceId != page.instanceId ||
      draft.snapshot.revision != page.revision ||
      draft.snapshot.stateToken != page.stateToken ||
      !draft.snapshot.scope.matches(page.scope) ||
      !draft.row.target.matches(row.target) ||
      !_sameBinding(draft.row.binding, row.binding) ||
      !_sameBinding(draft.row.storedBinding, row.storedBinding) ||
      draft.row.override != row.override ||
      draft.row.ruleApi != row.ruleApi;

  bool get _scopePending =>
      _owned.any((p) => p.snapshot.scopeId == widget.scope.scopeId);
  bool _current(ToolPermissionsApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();

  @override
  void initState() {
    super.initState();
    _changed.addListener(_update);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant OptionsPermissionTargets oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api) ||
        oldWidget.scope.scopeId != widget.scope.scopeId ||
        oldWidget.scope.stateToken != widget.scope.stateToken ||
        oldWidget.savedScopeId != widget.savedScopeId) {
      _read++;
      _page = null;
      _rows = [];
      _choices.clear();
      _next = null;
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    _changed.removeListener(_update);
    super.dispose();
  }

  void _update() {
    if (mounted && widget.active()) {
      setState(() {
        final page = _page;
        if (page != null) {
          for (final row in _rows) {
            final draft = _draft(page, row);
            _choices[row.target.id] = draft == null ? row.override : draft.mode;
          }
        }
      });
    }
  }

  Future<void> _load({bool more = false}) async {
    final api = widget.api, active = widget.active, scope = widget.scope;
    if (!mounted || api == null || !active() || (more && _next == null)) return;
    final read = ++_read,
        after = more ? _next : null,
        saved = widget.savedScopeId;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final page = await api.inspectTargets(
        kind: saved == null ? scope.scope.kind : 'default',
        scopeId: saved,
        sessionId: saved == null && scope.scope.kind == 'session'
            ? scope.scope.sessionId
            : null,
        taskId: saved == null && scope.scope.kind == 'task'
            ? scope.scope.taskId
            : null,
        after: after,
      );
      if (!_current(api, active) || read != _read) return;
      if (page.scopeId != scope.scopeId ||
          !page.scope.matches(scope.scope) ||
          (more &&
              (_page == null ||
                  page.revision != _page!.revision ||
                  page.stateToken != _page!.stateToken ||
                  page.instanceId != _page!.instanceId))) {
        throw const FormatException();
      }
      final rows = more ? [..._rows, ...page.targets] : page.targets;
      if (rows.length > 4112 ||
          rows.map((r) => r.target.id).toSet().length != rows.length) {
        throw const FormatException();
      }
      setState(() {
        _page = page;
        _rows = rows;
        _next = page.nextAfter;
        if (!more) _choices.clear();
        for (final row in rows) {
          final draft = _draft(page, row);
          _choices.putIfAbsent(
            row.target.id,
            () => draft?.mode ?? row.override,
          );
          if (draft != null) _choices[row.target.id] = draft.mode;
        }
      });
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(() {
          _error =
              'Target inventory could not be confirmed for this scope. Refresh the scope and targets; no rule was changed.';
          if (!more) {
            _page = null;
            _rows = [];
          }
        });
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  bool _renderCurrent(
    PermissionTargetsPage page,
    PermissionTargetRow row,
    String? renderedMode,
  ) =>
      mounted &&
      widget.active() &&
      !_reading &&
      identical(_page, page) &&
      _rows.any((current) => identical(current, row)) &&
      _choices.containsKey(row.target.id) &&
      _choices[row.target.id] == renderedMode &&
      (_draft(page, row) == null ||
          !_draftStale(_draft(page, row)!, page, row));

  void _choose(
    PermissionTargetsPage page,
    PermissionTargetRow row,
    String? renderedMode,
    String? mode,
  ) {
    if (!_renderCurrent(page, row, renderedMode)) return;
    final draft = _draft(page, row);
    if (mode != row.override && draft == null && _drafts.length >= 32) return;
    setState(() {
      if (mode == row.override) {
        if (draft != null) _drafts.remove(draft);
      } else if (draft == null) {
        _drafts.add(_TargetDraft(widget.api!, page, row, mode));
      } else {
        draft.mode = mode;
      }
      _choices[row.target.id] = mode;
    });
    _changed.value++;
  }

  void _discardDraft(_TargetDraft draft, String? renderedMode) {
    final api = widget.api;
    if (!mounted ||
        api == null ||
        !widget.active() ||
        !draft.owner.sameOwner(api) ||
        !_drafts.contains(draft) ||
        draft.mode != renderedMode) {
      return;
    }
    _drafts.remove(draft);
    final page = _page;
    if (page != null && page.scopeId == draft.snapshot.scopeId) {
      for (final row in _rows) {
        if (row.target.matches(draft.row.target)) {
          _choices[row.target.id] = row.override;
        }
      }
    }
    _changed.value++;
  }

  void _begin(
    PermissionTargetsPage page,
    PermissionTargetRow row,
    String? mode, {
    required String? renderedMode,
  }) {
    final api = widget.api;
    if (api == null ||
        !_renderCurrent(page, row, renderedMode) ||
        _scopePending ||
        _pending.length >= 16) {
      return;
    }
    try {
      final item = _PendingTarget(
        api,
        page,
        page.change(row, mode, const Uuid().v4()),
      );
      _pending.add(item);
      _changed.value++;
      if (!_current(api, widget.active) ||
          !_renderCurrent(page, row, renderedMode)) {
        _pending.remove(item);
        _changed.value++;
        return;
      }
      unawaited(
        _send(
          item,
          api,
          firstDispatchCurrent: () => _renderCurrent(page, row, renderedMode),
        ),
      );
    } catch (_) {
      if (mounted && widget.active()) {
        setState(
          () => _error =
              'This target rule was not dispatched. Inspect its current identity and scope.',
        );
      }
    }
  }

  Future<void> _send(
    _PendingTarget item,
    ToolPermissionsApi api, {
    bool check = false,
    bool release = false,
    bool Function()? firstDispatchCurrent,
  }) async {
    if (!api.sameOwner(item.owner) || item.busy || !_pending.contains(item)) {
      return;
    }
    final active = widget.active;
    item.busy = true;
    item.notice = check
        ? 'Checking exact historical receipt…'
        : 'Saving exact original target rule…';
    _changed.value++;
    try {
      if (!_current(api, active) ||
          (firstDispatchCurrent != null && !firstDispatchCurrent())) {
        if (!check && !item.dispatched) _pending.remove(item);
        return;
      }
      final receipt = check
          ? await api.targetReceipt(item.key)
          : await api.changeTarget(item.body, item.snapshot.scopeId);
      if (!receipt.matches(item.body, item.snapshot.scopeId)) {
        throw const FormatException();
      }
      _pending.remove(item);
      _drafts.removeWhere(
        (draft) =>
            draft.owner.sameOwner(item.owner) &&
            draft.snapshot.scopeId == item.snapshot.scopeId &&
            draft.snapshot.instanceId == item.snapshot.instanceId &&
            draft.snapshot.revision == item.snapshot.revision &&
            draft.snapshot.stateToken == item.snapshot.stateToken &&
            draft.row.target.id ==
                PermissionTarget.fromJson(item.body['target']).id &&
            draft.mode == item.body['mode'],
      );
      if (_current(api, active)) {
        setState(
          () => _notice =
              'Exact target save confirmed at revision ${receipt.revision}. Current behavior is refreshed separately.',
        );
        widget.onChanged();
        unawaited(_load());
      }
    } on ToolPermissionFailure catch (error) {
      item.dispatched = item.dispatched || error.dispatched;
      if (!check && !item.dispatched) {
        _pending.remove(item);
        if (_current(api, active)) {
          setState(
            () => _error =
                'The target rule was not dispatched. Your choice is retained.',
          );
        }
      } else {
        item.notice =
            'The save outcome remains unknown. Check its exact receipt or retry the unchanged request.';
        if (check) await _fence(item, api, active, release: release);
      }
    } catch (_) {
      item.dispatched = true;
      item.notice =
          'The response could not be confirmed. The original target, binding, scope and key remain retained.';
      if (check) await _fence(item, api, active, release: release);
    } finally {
      item.busy = false;
      _changed.value++;
      if (_current(api, active)) setState(() {});
    }
  }

  Future<void> _fence(
    _PendingTarget item,
    ToolPermissionsApi api,
    bool Function() active, {
    required bool release,
  }) async {
    if (!_current(api, active) || !_pending.contains(item)) return;
    item.fenceOwner = null;
    item.fenceRevision = null;
    try {
      // Shared global CAS revision; a removed target needs no live lookup here.
      final fresh = await api.inspect();
      if (!_current(api, active) || !_pending.contains(item)) return;
      if (fresh.instanceId != item.snapshot.instanceId ||
          fresh.revision <= (item.body['expected_revision'] as int)) {
        item.notice =
            'Outcome unknown. This connection has not fenced any future commit of the original request.';
        return;
      }
      item.fenceOwner = api;
      item.fenceRevision = fresh.revision;
      if (release) {
        _pending.remove(item);
        setState(
          () => _notice =
              'Local tracking released after same-host revision ${fresh.revision} fenced this request. Historical outcome remains unknown. No work was cancelled or rolled back.',
        );
        widget.onChanged();
        unawaited(_load());
      } else {
        item.notice =
            'Outcome unknown. The same host advanced to revision ${fresh.revision}; this original request can no longer create a new commit. You may release local tracking.';
      }
    } catch (_) {
      if (_current(api, active) && _pending.contains(item)) {
        item.notice =
            'Outcome unknown; a same-host mutation fence could not be confirmed.';
      }
    }
  }

  Widget _pendingCard(_PendingTarget item) => Card(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Unconfirmed target rule · ${item.label}'),
          Text(item.snapshot.scope.label),
          Text(item.notice),
          ExpansionTile(
            title: const Text('Original request identity'),
            children: [
              SelectableText(
                'Native instance ${item.snapshot.instanceId}\nScope ${item.snapshot.scopeId}\n${const JsonEncoder.withIndent('  ').convert(item.body)}',
              ),
            ],
          ),
          Wrap(
            spacing: 8,
            children: [
              TextButton(
                onPressed: item.busy || !widget.active()
                    ? null
                    : () => unawaited(_send(item, widget.api!, check: true)),
                child: const Text('Check exact receipt'),
              ),
              OutlinedButton(
                onPressed: item.busy || !widget.active()
                    ? null
                    : () => unawaited(_send(item, widget.api!)),
                child: const Text('Retry exact save'),
              ),
              if (item.fenceRevision != null &&
                  identical(item.fenceOwner, widget.api))
                TextButton(
                  onPressed: item.busy || !widget.active()
                      ? null
                      : () => unawaited(
                          _send(item, widget.api!, check: true, release: true),
                        ),
                  child: const Text('Release fenced request'),
                ),
            ],
          ),
        ],
      ),
    ),
  );

  Widget _targetCard(PermissionTargetsPage page, PermissionTargetRow row) {
    final mode = _choices[row.target.id];
    final draft = _draft(page, row);
    final stale = draft != null && _draftStale(draft, page, row);
    final bindingChanged =
        row.binding != null &&
        row.storedBinding != null &&
        !row.binding!.matches(row.storedBinding!);
    final choiceChanged = mode != row.override;
    final rebind = mode != null && !choiceChanged && bindingChanged;
    final editable =
        widget.active() &&
        !_reading &&
        !_scopePending &&
        !stale &&
        _pending.length < 16 &&
        row.editable &&
        row.ruleApi == 'target' &&
        page.revision < permissionRevisionLimit;
    final canChoose = editable && (draft != null || _drafts.length < 32);
    final canGrant =
        page.scopeLive &&
        row.available &&
        row.binding != null &&
        row.binding!.observed;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              row.target.label,
              style: Theme.of(context).textTheme.titleMedium,
            ),
            Text(row.reason),
            Text(
              'Saved: ${row.override ?? 'inherit'} · Effective: ${row.effective} · Source: ${row.source}',
            ),
            Text(
              'Host: ${row.hostMode}${row.hardDenied ? ' · Hard Deny ceiling' : ''} · Binding: ${row.bindingState}',
            ),
            if (row.bindingState != 'current')
              const Text(
                'The saved binding is unavailable or changed. Review the exact current binding before an explicit new save.',
              ),
            if (row.target.isMcp)
              const Text(
                'This is a schema/configuration identity, not proof of remote behavior. Enablement, transport grant and tool allowlist remain required.',
              ),
            if (stale)
              const Text(
                'This draft belongs to earlier native state. Its exact choice and binding are retained below. Discard it before choosing against the current target.',
              ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              key: ValueKey((page.scopeId, row.target.id, page.revision, mode)),
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
                    enabled: value == 'deny'
                        ? page.scopeLive && row.binding != null
                        : canGrant,
                    child: Text(value[0].toUpperCase() + value.substring(1)),
                  ),
              ],
              onChanged: !canChoose || !page.scopeLive || row.binding == null
                  ? null
                  : (value) {
                      if (value != null) {
                        _choose(
                          page,
                          row,
                          mode,
                          value == 'inherit' ? null : value,
                        );
                      }
                    },
            ),
            Wrap(
              spacing: 8,
              children: [
                FilledButton(
                  onPressed:
                      editable &&
                          (choiceChanged || rebind) &&
                          page.scopeLive &&
                          row.binding != null &&
                          (mode == null || mode == 'deny' || canGrant)
                      ? () => _begin(page, row, mode, renderedMode: mode)
                      : null,
                  child: Text(
                    rebind
                        ? 'Rebind to current identity'
                        : 'Save exact target rule',
                  ),
                ),
                TextButton(
                  onPressed:
                      editable &&
                          row.override != null &&
                          (row.storedBinding != null || row.binding != null)
                      ? () => _begin(page, row, null, renderedMode: mode)
                      : null,
                  child: const Text('Reset this override'),
                ),
                if (choiceChanged && !stale)
                  TextButton(
                    onPressed: !widget.active()
                        ? null
                        : () {
                            _choose(page, row, mode, row.override);
                          },
                    child: const Text('Discard change'),
                  ),
              ],
            ),
            ExpansionTile(
              title: const Text('Target and binding details'),
              children: [
                SelectableText(
                  'Target ${row.target.id}\n${jsonEncode(row.target.body)}\nCurrent binding ${jsonEncode(row.binding?.body)}\nSaved binding ${jsonEncode(row.storedBinding?.body)}'
                  '${row.ruleId == null ? '' : '\nRule ${row.ruleId} · revision ${row.ruleRevision}\nSource scope ${row.sourceScopeId}'}',
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _draftCard(_TargetDraft draft) {
    final mode = draft.mode;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Unsaved target choice · ${draft.row.target.label}'),
            Text('${draft.snapshot.scope.label} · ${draft.mode ?? 'inherit'}'),
            const Text(
              'Retained against its original native revision and binding. Refreshing does not rebind or save it.',
            ),
            ExpansionTile(
              title: const Text('Original draft identity'),
              children: [
                SelectableText(
                  'Native instance ${draft.snapshot.instanceId}\nScope ${draft.snapshot.scopeId}\nRevision ${draft.snapshot.revision}\nState ${draft.snapshot.stateToken}\nTarget ${jsonEncode(draft.row.target.body)}\nBinding ${jsonEncode(draft.row.binding?.body)}',
                ),
              ],
            ),
            TextButton(
              onPressed: widget.active()
                  ? () => _discardDraft(draft, mode)
                  : null,
              child: const Text('Discard this unsaved choice'),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final page = _page;
    final inventory = _rows.where((r) => r.ruleApi != 'target').toList();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const Divider(height: 32),
        Text(
          'Search and MCP tool rules',
          style: Theme.of(context).textTheme.titleLarge,
        ),
        const Text(
          'Uses the scope selected above. Inventory is passive: opening this view does not discover tools or connect to servers. Rules apply to future action admissions.',
        ),
        Wrap(
          children: [
            TextButton.icon(
              onPressed: widget.active() && !_reading
                  ? () => unawaited(_load())
                  : null,
              icon: const Icon(Icons.refresh),
              label: const Text('Refresh target inventory'),
            ),
          ],
        ),
        if (_reading) const LinearProgressIndicator(),
        if (_error != null) Text(_error!),
        if (_notice != null) Text(_notice!),
        for (final draft in _ownedDrafts) _draftCard(draft),
        if (_drafts.length >= 32)
          const Text(
            'Unsaved target draft capacity reached. Existing choices are retained; discard a draft before editing another target.',
          ),
        for (final item in _owned) _pendingCard(item),
        if (_pending.length >= 16)
          const Text(
            'Target save tracking is full. Reconcile retained requests before another save; unknown outcomes are not discarded.',
          ),
        if (page != null) ...[
          Text('${page.scope.label} · permission revision ${page.revision}'),
          if (!page.scopeLive)
            const Text(
              'Retired scope. Only reset of its saved target overrides is available.',
            ),
          for (final row in _rows.where((r) => r.ruleApi == 'target'))
            _targetCard(page, row),
          if (inventory.isNotEmpty)
            ExpansionTile(
              title: const Text('Other native targets'),
              children: [
                for (final row in inventory)
                  ListTile(
                    title: Text(row.target.label),
                    subtitle: Text(
                      row.ruleApi == 'legacy'
                          ? 'Use the three native-tool controls above. Effective: ${row.effective}.'
                          : '${row.reason}. Effective: ${row.effective}. No scoped editor is available for this target.',
                    ),
                  ),
              ],
            ),
          if (_rows.isEmpty) const Text('No targets are currently visible.'),
          if (_next != null)
            TextButton(
              onPressed: _reading || !widget.active()
                  ? null
                  : () => unawaited(_load(more: true)),
              child: const Text('Load more targets'),
            ),
        ],
        const Text(
          'Deny does not stop already admitted work. Search, general filesystem access, shell, context selection and other tools keep their independent boundaries.',
        ),
      ],
    );
  }
}
