import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';
import '../api/workspace_settings.dart';
import '../api/workspace_settings_client.dart';
import 'design_tokens.dart';

class OptionsWorkspaceSettings extends StatefulWidget {
  const OptionsWorkspaceSettings({
    super.key,
    required this.api,
    required this.active,
    this.onChanged,
  });
  final WorkspaceSettingsApi? api;
  final bool Function() active;
  final VoidCallback? onChanged;
  @override
  State<OptionsWorkspaceSettings> createState() =>
      _OptionsWorkspaceSettingsState();
}

class _WorkspaceDraft {
  _WorkspaceDraft(this.owner, this.base)
    : root = base.saved.root,
      readonly = base.saved.readonly.join('\n');
  final WorkspaceSettingsApi owner;
  final WorkspaceSettingsSnapshot base;
  String root, readonly;
  int edit = 0;
  bool get dirty =>
      root != base.saved.root || readonly != base.saved.readonly.join('\n');
  WorkspaceSelection get selection =>
      WorkspaceSelection.fromInput(root, readonly);
}

class _PendingWorkspace {
  _PendingWorkspace(this.owner, this.body, this.draft, this.edit);
  final WorkspaceSettingsApi owner;
  final Map<String, Object?> body;
  final _WorkspaceDraft draft;
  final int edit;
  bool busy = false, dispatched = false;
  WorkspaceSettingsLookup? fence;
  WorkspaceSettingsApi? fenceOwner;
  String notice = 'Saving the selected workspace…';
}

class _OptionsWorkspaceSettingsState extends State<OptionsWorkspaceSettings> {
  static final _drafts = <_WorkspaceDraft>[];
  static final _pending = <_PendingWorkspace>[];
  static final _changes = ValueNotifier<int>(0);
  final _root = TextEditingController(), _readonly = TextEditingController();
  WorkspaceSettingsSnapshot? _snapshot;
  _WorkspaceDraft? _draft;
  bool _reading = false;
  int _read = 0;
  String? _error, _notice;

  _PendingWorkspace? get _ownerPending {
    final api = widget.api;
    if (api != null) {
      for (final item in _pending) {
        if (api.sameOwner(item.owner)) return item;
      }
    }
    return null;
  }

  bool _current(WorkspaceSettingsApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();
  bool _editable(
    WorkspaceSettingsSnapshot page,
    _WorkspaceDraft draft, {
    _PendingWorkspace? pending,
  }) =>
      mounted &&
      widget.active() &&
      !_reading &&
      identical(_snapshot, page) &&
      identical(_draft, draft) &&
      _drafts.contains(draft) &&
      widget.api?.sameOwner(draft.owner) == true &&
      draft.base.sameBase(page) &&
      page.writable &&
      !page.busy &&
      page.revision < workspaceRevisionLimit &&
      _ownerPending == pending &&
      widget.api?.hasUnconfirmedChange == false;

  @override
  void initState() {
    super.initState();
    _changes.addListener(_changed);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant OptionsWorkspaceSettings oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api)) {
      _read++;
      _snapshot = null;
      _draft = null;
      _error = _notice = null;
      _syncControllers();
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    _changes.removeListener(_changed);
    _root.dispose();
    _readonly.dispose();
    super.dispose();
  }

  void _syncControllers() {
    if (_root.text != (_draft?.root ?? '')) _root.text = _draft?.root ?? '';
    if (_readonly.text != (_draft?.readonly ?? '')) {
      _readonly.text = _draft?.readonly ?? '';
    }
  }

  void _changed() {
    if (mounted && widget.active()) {
      setState(() {
        final api = widget.api;
        if (api != null) {
          _WorkspaceDraft? owned;
          for (final draft in _drafts) {
            if (api.sameOwner(draft.owner)) {
              owned = draft;
              break;
            }
          }
          _draft = owned;
        }
        _syncControllers();
      });
    }
  }

  Future<void> _load() async {
    final api = widget.api, active = widget.active, read = ++_read;
    if (api == null || !mounted || !active()) return;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final page = await api.inspect();
      if (!_current(api, active) || read != _read) return;
      _WorkspaceDraft? prior;
      for (final draft in _drafts) {
        if (api.sameOwner(draft.owner)) {
          prior = draft;
          break;
        }
      }
      setState(() {
        _snapshot = page;
        if (prior == null || !prior.dirty) {
          if (prior != null) _drafts.remove(prior);
          if (_drafts.length >= 16) {
            for (final candidate in List<_WorkspaceDraft>.of(_drafts)) {
              if (!candidate.dirty &&
                  !_pending.any((item) => identical(item.draft, candidate))) {
                _drafts.remove(candidate);
                break;
              }
            }
          }
          if (_drafts.length >= 16) {
            _draft = null;
            _error =
                'Workspace draft storage is full. Existing drafts are retained.';
          } else {
            _draft = _WorkspaceDraft(api, page);
            _drafts.add(_draft!);
          }
        } else {
          _draft = prior;
        }
        _syncControllers();
      });
      _changes.value++;
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(() {
          _snapshot = null;
          _error =
              'Workspace authority could not be inspected. Drafts and unconfirmed saves are retained.';
        });
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  void _edit(
    WorkspaceSettingsSnapshot page,
    _WorkspaceDraft draft,
    String value, {
    required bool root,
  }) {
    if (!_editable(page, draft) ||
        (root ? _root.text : _readonly.text) != value) {
      return;
    }
    if (root) {
      draft.root = value;
    } else {
      draft.readonly = value;
    }
    draft.edit++;
    _changes.value++;
  }

  void _discard(
    WorkspaceSettingsSnapshot page,
    _WorkspaceDraft draft,
    int edit,
  ) {
    if (!mounted ||
        !widget.active() ||
        _reading ||
        !identical(_snapshot, page) ||
        !identical(_draft, draft) ||
        draft.edit != edit ||
        widget.api?.sameOwner(draft.owner) != true ||
        _ownerPending != null ||
        widget.api?.hasUnconfirmedChange != false) {
      return;
    }
    _drafts.remove(draft);
    _draft = _WorkspaceDraft(widget.api!, page);
    _drafts.add(_draft!);
    _changes.value++;
  }

  void _save(WorkspaceSettingsSnapshot page, _WorkspaceDraft draft, int edit) {
    final api = widget.api;
    if (api == null ||
        !_editable(page, draft) ||
        draft.edit != edit ||
        (!draft.dirty && page.activeMatchesSaved) ||
        _pending.length >= 16) {
      return;
    }
    try {
      final item = _PendingWorkspace(
        api,
        page.change(draft.selection, const Uuid().v4()),
        draft,
        edit,
      );
      _pending.add(item);
      _changes.value++;
      unawaited(
        _send(
          item,
          api,
          first: true,
          firstCurrent: () =>
              _editable(page, draft, pending: item) && draft.edit == edit,
        ),
      );
    } catch (_) {
      if (mounted && widget.active()) {
        setState(
          () => _error =
              'Use an absolute root and up to 32 unique relative read-only paths. Paths are preserved exactly; the native owner checks their existence and safety.',
        );
      }
    }
  }

  void _confirmed(
    _PendingWorkspace item,
    WorkspaceSettingsReceipt receipt,
    WorkspaceSettingsApi api,
    bool Function() active,
  ) {
    if (!receipt.matches(item.body)) throw const FormatException();
    _pending.remove(item);
    final draft = item.draft;
    if (_drafts.contains(draft) &&
        draft.edit == item.edit &&
        draft.base.backend == item.body['expected_backend'] &&
        draft.base.instance == item.body['expected_instance'] &&
        draft.base.revision == item.body['expected_revision'] &&
        draft.base.stateToken == item.body['expected_state'] &&
        draft.selection.matches(
          WorkspaceSelection.fromJson(item.body['settings']),
        )) {
      _drafts.remove(draft);
      if (identical(_draft, draft)) _draft = null;
    }
    if (_current(api, active)) {
      setState(
        () => _notice =
            'Save recorded at revision ${receipt.revision}. Current application is inspected separately.',
      );
      widget.onChanged?.call();
      unawaited(_load());
    }
  }

  Future<void> _send(
    _PendingWorkspace item,
    WorkspaceSettingsApi api, {
    bool first = false,
    bool retry = false,
    bool release = false,
    bool Function()? firstCurrent,
  }) async {
    if (item.busy || !api.sameOwner(item.owner) || !_pending.contains(item)) {
      return;
    }
    final active = widget.active;
    item.busy = true;
    item.fence = null;
    item.fenceOwner = null;
    _changes.value++;
    try {
      if (!_current(api, active) || (first && firstCurrent?.call() != true)) {
        if (first && !item.dispatched) _pending.remove(item);
        return;
      }
      if (!first) {
        final lookup = await api.lookup(item.body);
        if (lookup.receipt case final receipt?) {
          _confirmed(item, receipt, api, active);
          return;
        }
        if (!_current(api, active)) return;
        if (lookup.fenced) {
          item.fence = lookup;
          item.fenceOwner = api;
          item.notice =
              'The historical outcome is unknown. Native authority confirms this unchanged request can make no new commit.';
          if (release) {
            api.releaseFenced(item.body, lookup);
            _pending.remove(item);
            setState(
              () => _notice =
                  'Local tracking released. The outcome remains unknown; the draft is retained. No work was cancelled or rolled back.',
            );
            widget.onChanged?.call();
            unawaited(_load());
          }
          return;
        }
        item.notice =
            'Outcome unknown. The original request is not fenced; check again or retry it unchanged.';
        if (!retry) return;
      }
      if (!_current(api, active)) return;
      final receipt = await api.change(item.body);
      _confirmed(item, receipt, api, active);
    } on WorkspaceSettingsFailure catch (error) {
      item.dispatched |= error.dispatched;
      if (first && !item.dispatched) {
        _pending.remove(item);
        if (_current(api, active)) {
          setState(
            () => _error =
                'The workspace save was not dispatched. Your draft is retained.',
          );
        }
      } else {
        item.notice =
            'The save is unconfirmed. Its original root, restrictions, authority and request key remain retained.';
      }
    } catch (_) {
      item.dispatched = true;
      item.notice =
          'The original save could not be confirmed. Look up that exact request before retrying.';
    } finally {
      item.busy = false;
      _changes.value++;
      if (_current(api, active)) setState(() {});
    }
  }

  Widget _pendingCard(_PendingWorkspace item) {
    final api = widget.api;
    final enabled = api != null && widget.active() && !item.busy;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Unconfirmed workspace save'),
            Text(item.notice),
            ExpansionTile(
              title: const Text('Original request identity'),
              children: [
                SelectableText(
                  const JsonEncoder.withIndent('  ').convert(item.body),
                ),
              ],
            ),
            Wrap(
              spacing: 8,
              children: [
                TextButton(
                  onPressed: enabled ? () => unawaited(_send(item, api)) : null,
                  child: const Text('Check original request'),
                ),
                OutlinedButton(
                  onPressed: enabled
                      ? () => unawaited(_send(item, api, retry: true))
                      : null,
                  child: const Text('Retry exact save'),
                ),
                if (item.fence != null && identical(item.fenceOwner, api))
                  TextButton(
                    onPressed: enabled
                        ? () => unawaited(_send(item, api, release: true))
                        : null,
                    child: const Text('Release fenced request'),
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
    final page = _snapshot, draft = _draft, pending = _ownerPending;
    final enabled = page != null && draft != null && _editable(page, draft);
    final edit = draft?.edit;
    WorkspaceSelection? selection;
    try {
      selection = draft?.selection;
    } catch (_) {
      /* Keep the exact invalid draft. */
    }
    return DesignPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            'Approved workspace',
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: 8),
          const Text(
            'Save authorizes Agent access to the selected root under the existing tool, disclosure and host restrictions. No files are moved. Changes require settled native work.',
          ),
          if (_reading) const LinearProgressIndicator(),
          if (_error != null) Text(_error!),
          if (_notice != null) Text(_notice!),
          if (pending != null) _pendingCard(pending),
          if (page != null) ...[
            const SizedBox(height: 12),
            Text('Saved root · revision ${page.revision}'),
            SelectableText(page.saved.root),
            Text(
              'Saved read-only overlays: ${page.saved.readonly.isEmpty ? 'None' : page.saved.readonly.join(', ')}',
            ),
            Text(
              page.desiredAvailable
                  ? 'Saved root currently available'
                  : 'Saved root unavailable',
            ),
            if (page.desiredError != null) Text(page.desiredError!),
            const SizedBox(height: 12),
            const Text('Active workspace'),
            SelectableText(
              page.active?.settings.root ??
                  'Current application is unavailable or unconfirmed',
            ),
            if (page.active != null)
              Text(
                'Active read-only overlays: ${page.active!.settings.readonly.isEmpty ? 'None' : page.active!.settings.readonly.join(', ')}',
              ),
            Text(
              page.activeMatchesSaved
                  ? 'Active settings match the saved selection'
                  : 'Saved and active state are not confirmed to match',
            ),
            if (page.activeError != null) Text(page.activeError!),
            if (page.busy)
              const Text(
                'Native work or cleanup is still owned. Workspace changes are blocked.',
              ),
            if (!page.writable)
              const Text(
                'This service instance has no current workspace write authority.',
              ),
            Text(
              'Execution: Bubblewrap · direct mode · ${page.binaryDetected ? 'binary detected' : 'binary not detected'}. Isolation has not been verified by this inspection.',
            ),
            const Text(
              'Git worktree creation and alternative execution backends are not supported here.',
            ),
            ExpansionTile(
              title: const Text('Native authority and operation state'),
              children: [
                SelectableText(
                  'Backend ${page.backend}\nInstance ${page.instance}\nState ${page.stateToken}\nSaved root identity ${page.savedRootIdentity}\n${page.active == null ? '' : 'Active workspace ${page.active!.pin.workspaceId}\nActive root identity ${page.active!.pin.rootIdentity}\nActive source ${page.active!.pin.source} · revision ${page.active!.pin.revision}\n'}${jsonEncode(page.operationState)}',
                ),
              ],
            ),
            ExpansionTile(
              title: const Text('Host-owned restrictions'),
              children: [
                for (final entry in page.hostGuards.entries)
                  ListTile(
                    title: Text(entry.key),
                    subtitle: SelectableText(
                      entry.value.isEmpty
                          ? 'None advertised'
                          : entry.value.join('\n'),
                    ),
                  ),
                const Text(
                  'These protected and read-only ceilings cannot be removed by a workspace save.',
                ),
              ],
            ),
          ],
          if (draft != null) ...[
            const Divider(height: 32),
            if (page == null || !draft.base.sameBase(page))
              const Text(
                'This draft retains earlier native authority. Refresh metadata; retained edits require explicit discard before choosing against new state.',
              ),
            TextField(
              controller: _root,
              enabled: enabled,
              maxLength: 4096,
              decoration: const InputDecoration(
                labelText: 'Workspace root',
                helperText:
                    'Existing absolute canonical directory. Saving this form grants access.',
                helperMaxLines: 3,
                border: OutlineInputBorder(),
              ),
              onChanged: enabled
                  ? (value) => _edit(page, draft, value, root: true)
                  : null,
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _readonly,
              enabled: enabled,
              minLines: 3,
              maxLines: 8,
              maxLength: 16416,
              decoration: const InputDecoration(
                labelText: 'Additional read-only paths',
                helperText:
                    'One existing relative directory per line, up to 32. Use . for the whole root. Host restrictions still apply.',
                helperMaxLines: 3,
                border: OutlineInputBorder(),
              ),
              onChanged: enabled
                  ? (value) => _edit(page, draft, value, root: false)
                  : null,
            ),
            if (selection == null)
              const Text(
                'The retained draft contains an invalid root or read-only path list. Nothing has been saved.',
              ),
            Wrap(
              spacing: 8,
              children: [
                FilledButton(
                  onPressed:
                      enabled &&
                          (draft.dirty || !page.activeMatchesSaved) &&
                          selection != null
                      ? () => _save(page, draft, edit!)
                      : null,
                  child: Text(
                    !draft.dirty && page?.activeMatchesSaved == false
                        ? 'Authorize and repair saved workspace'
                        : 'Save and authorize this workspace',
                  ),
                ),
                TextButton(
                  onPressed:
                      page != null &&
                          widget.active() &&
                          !_reading &&
                          draft.dirty &&
                          pending == null &&
                          widget.api?.hasUnconfirmedChange == false
                      ? () => _discard(page, draft, edit!)
                      : null,
                  child: const Text('Discard draft'),
                ),
              ],
            ),
            if (draft.dirty)
              ExpansionTile(
                title: const Text('Original draft authority'),
                children: [
                  SelectableText(
                    'Backend ${draft.base.backend}\nInstance ${draft.base.instance}\nRevision ${draft.base.revision}\nState ${draft.base.stateToken}',
                  ),
                ],
              ),
          ],
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: widget.active() && !_reading
                  ? () => unawaited(_load())
                  : null,
              icon: const Icon(Icons.refresh),
              label: const Text('Refresh workspace metadata'),
            ),
          ),
        ],
      ),
    );
  }
}
