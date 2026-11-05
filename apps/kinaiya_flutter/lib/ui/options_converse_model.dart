import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:uuid/uuid.dart';

import '../api/converse_model_client.dart';
import '../api/converse_models.dart';
import 'design_tokens.dart';

class OptionsConverseModel extends StatefulWidget {
  const OptionsConverseModel({
    super.key,
    required this.api,
    required this.active,
    required this.onInvalidate,
    required this.onSettled,
    this.compact = false,
    this.onOpenModelOptions,
    this.onAddModel,
    this.favorites,
  });
  final ConverseModelsApi? api;
  final bool Function() active;
  final VoidCallback onInvalidate;
  final Future<void> Function() onSettled;
  final bool compact;
  final VoidCallback? onOpenModelOptions;
  final VoidCallback? onAddModel;

  /// Local presentation preference, scoped by the caller to the current host.
  final Set<String>? favorites;
  @override
  State<OptionsConverseModel> createState() => _OptionsConverseModelState();
}

class _PendingConverseModel {
  _PendingConverseModel(this.owner, this.snapshot, this.body);
  final ConverseModelsApi owner;
  final ConverseModelsSnapshot snapshot;
  final Map<String, Object?> body;
  String get key => body['idempotency_key'] as String;
  bool inFlight = false, dispatched = false, confirmed = false;
  String notice = 'Saving the selected text model…';
  ConverseModelsApi? fenceOwner;
  int? fenceRevision;
}

class _OptionsConverseModelState extends State<OptionsConverseModel> {
  static final _pending = <_PendingConverseModel>[];
  static final _changes = ValueNotifier<int>(0);
  ConverseModelsSnapshot? _snapshot, _base;
  ConverseModelSelection? _draft;
  List<_PendingConverseModel> _observed = [];
  bool _reading = false;
  int _read = 0;
  String? _error, _notice;
  final _search = TextEditingController();
  final _expandedChoices = <String>{};
  final _localFavorites = <String>{};
  final _choiceFocus = <String, FocusNode>{};

  _PendingConverseModel? get _ownerPending {
    final api = widget.api;
    if (api == null) return null;
    for (final item in _pending) {
      if (api.sameOwner(item.owner)) return item;
    }
    return null;
  }

  bool get _dirty =>
      _draft != null && _base != null && !_draft!.matches(_base!.selection);
  bool _current(ConverseModelsApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();

  bool _canEdit(
    ConverseModelsSnapshot rendered,
    ConverseModelSelection draft,
  ) =>
      mounted &&
      widget.active() &&
      !_reading &&
      identical(_snapshot, rendered) &&
      identical(_draft, draft) &&
      _ownerPending == null &&
      widget.api?.hasUnconfirmedChange == false;

  void _discard(ConverseModelsSnapshot rendered, ConverseModelSelection draft) {
    if (!_canEdit(rendered, draft)) return;
    setState(() {
      _base = rendered;
      _draft = rendered.selection;
    });
  }

  @override
  void initState() {
    super.initState();
    _observed = _ownerPending == null ? [] : [_ownerPending!];
    _changes.addListener(_pendingChanged);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant OptionsConverseModel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api)) {
      _read++;
      _snapshot = _base = null;
      _draft = null;
      _reading = false;
      _error = _notice = null;
      _expandedChoices.clear();
      _observed = _ownerPending == null ? [] : [_ownerPending!];
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    _search.dispose();
    for (final focus in _choiceFocus.values) {
      focus.dispose();
    }
    _changes.removeListener(_pendingChanged);
    super.dispose();
  }

  void _pendingChanged() {
    if (!mounted || !widget.active()) return;
    final prior = _observed;
    final item = _ownerPending;
    _observed = item == null ? [] : [item];
    setState(() {});
    if (prior.any((item) => item.confirmed && !_pending.contains(item))) {
      _base = null;
      _draft = null;
      unawaited(_load());
      unawaited(widget.onSettled());
    }
  }

  Future<void> _load() async {
    final api = widget.api, active = widget.active, read = ++_read;
    if (api == null || !active()) return;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final next = await api.inspect();
      if (!_current(api, active) || read != _read) return;
      setState(() {
        if (_draft == null || !_dirty) {
          _base = next;
          _draft = next.selection;
        }
        _snapshot = next;
      });
      if (_ownerPending == null && !api.hasUnconfirmedChange) {
        unawaited(widget.onSettled());
      }
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(() {
          _snapshot = null;
          _error =
              'Converse model options are unavailable. Your draft and any unresolved save are retained.';
        });
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  void _save(ConverseModelsSnapshot rendered, ConverseModelSelection choice) {
    final api = widget.api, active = widget.active;
    if (!mounted ||
        api == null ||
        !active() ||
        !identical(_snapshot, rendered) ||
        !identical(_draft, choice) ||
        _base == null ||
        _reading ||
        rendered.busy ||
        _ownerPending != null ||
        api.hasUnconfirmedChange ||
        _pending.length >= 16 ||
        _base!.instanceId != rendered.instanceId ||
        _base!.revision != rendered.revision ||
        _base!.stateToken != rendered.stateToken ||
        !rendered.permits(choice) ||
        !_dirty) {
      return;
    }
    try {
      final item = _PendingConverseModel(
        api,
        rendered,
        rendered.change(choice, const Uuid().v4()),
      );
      _pending.add(item);
      _changes.value++;
      if (!_current(api, active)) {
        _pending.remove(item);
        _changes.value++;
        return;
      }
      unawaited(_send(item, api, first: true));
    } catch (_) {
      if (_current(api, active)) {
        setState(
          () => _error =
              'The model choice was not dispatched. Review the current native revision and selection.',
        );
      }
    }
  }

  Future<void> _confirmed(
    _PendingConverseModel item,
    ConverseModelReceipt receipt,
    ConverseModelsApi api,
    bool Function() active,
  ) async {
    if (!receipt.matches(item.body)) throw const FormatException();
    item.confirmed = true;
    _pending.remove(item);
    if (_current(api, active)) {
      setState(() {
        _notice =
            'Text model selection saved at revision ${receipt.revision}. Model readiness has not been checked.';
      });
    }
  }

  Future<void> _send(
    _PendingConverseModel item,
    ConverseModelsApi api, {
    bool first = false,
    bool retry = false,
    bool release = false,
  }) async {
    if (!api.sameOwner(item.owner) ||
        item.inFlight ||
        !_pending.contains(item)) {
      return;
    }
    final active = widget.active;
    final settled = widget.onSettled;
    item.inFlight = true;
    item.notice = first
        ? 'Saving the exact selected model…'
        : 'Checking the original save receipt…';
    _changes.value++;
    try {
      if (!_current(api, active)) {
        if (first && !item.dispatched) _pending.remove(item);
        return;
      }
      if (!first) {
        try {
          final receipt = await api.receipt(item.key);
          await _confirmed(item, receipt, api, active);
          unawaited(settled());
          return;
        } on ConverseModelFailure catch (error) {
          item.notice = 'The earlier save outcome remains unknown.';
          if (release || !retry || error.status != 404) {
            await _fence(item, api, active, release: release);
            return;
          }
          // Only this explicit retry action, after receipt readback, may send
          // the unchanged request. A 404 is not proof the first save failed.
          if (!_current(api, active)) return;
        }
      }
      if (!_current(api, active)) return;
      widget.onInvalidate();
      if (!_current(api, active)) {
        if (first && !item.dispatched) _pending.remove(item);
        return;
      }
      final receipt = await api.change(item.body);
      await _confirmed(item, receipt, api, active);
      unawaited(settled());
    } on ConverseModelFailure catch (error) {
      item.dispatched |= error.dispatched;
      if (first && !item.dispatched && !api.hasUnconfirmedChange) {
        _pending.remove(item);
        if (_current(api, active)) {
          setState(
            () => _error =
                'The model choice was not dispatched. Your draft is retained.',
          );
        }
        unawaited(settled());
      } else {
        item.notice =
            'The model save outcome remains unknown. Keep its original body and key; check the receipt or retry the exact save.';
      }
    } catch (_) {
      item.dispatched = true;
      item.notice =
          'The model save response could not be confirmed. The original request is retained.';
    } finally {
      item.inFlight = false;
      _changes.value++;
      if (_current(api, active)) setState(() {});
    }
  }

  Future<void> _fence(
    _PendingConverseModel item,
    ConverseModelsApi api,
    bool Function() active, {
    required bool release,
  }) async {
    if (!_current(api, active) || !_pending.contains(item)) return;
    item.fenceOwner = null;
    item.fenceRevision = null;
    try {
      final fresh = await api.inspect();
      if (!_current(api, active) || !_pending.contains(item)) return;
      if (fresh.instanceId != item.snapshot.instanceId ||
          fresh.revision <= (item.body['expected_revision'] as int)) {
        item.notice =
            'The earlier save outcome remains unknown. This host has not confirmed that the original request can no longer change the selection.';
        return;
      }
      item.fenceOwner = api;
      item.fenceRevision = fresh.revision;
      if (release) {
        api.releaseFenced(item.body, item.snapshot.instanceId, fresh);
        _pending.remove(item);
        setState(
          () => _notice =
              'Local blocking released at revision ${fresh.revision}. The earlier outcome remains unknown; this unchanged request can make no new commit on this host. No work was cancelled or rolled back.',
        );
        unawaited(_load());
        unawaited(widget.onSettled());
      } else {
        item.notice =
            'The earlier outcome remains unknown. This same native instance has advanced to revision ${fresh.revision}, so the original request can make no new commit. You may release local blocking.';
      }
    } catch (_) {
      if (_current(api, active) && _pending.contains(item)) {
        item.notice =
            'The earlier outcome remains unknown. Its receipt or revision fence could not be confirmed.';
      }
    }
  }

  Widget _pendingCard(_PendingConverseModel item) => Card(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Unconfirmed Converse model save',
            style: Theme.of(context).textTheme.titleMedium,
          ),
          Text(item.notice),
          ExpansionTile(
            title: const Text('Original save identity'),
            children: [
              SelectableText(
                'Native instance ${item.snapshot.instanceId}\n${const JsonEncoder.withIndent('  ').convert(item.body)}',
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
                    : () => unawaited(_send(item, widget.api!)),
                child: const Text('Check exact receipt'),
              ),
              OutlinedButton(
                onPressed: item.inFlight || !widget.active()
                    ? null
                    : () => unawaited(_send(item, widget.api!, retry: true)),
                child: const Text('Retry exact save'),
              ),
              if (item.fenceRevision != null &&
                  identical(item.fenceOwner, widget.api))
                TextButton(
                  onPressed: item.inFlight || !widget.active()
                      ? null
                      : () =>
                            unawaited(_send(item, widget.api!, release: true)),
                  child: const Text('Release fenced request'),
                ),
            ],
          ),
        ],
      ),
    ),
  );

  @override
  Widget build(BuildContext context) {
    final snapshot = _snapshot, draft = _draft, base = _base;
    final stale =
        snapshot != null &&
        base != null &&
        (snapshot.instanceId != base.instanceId ||
            snapshot.revision != base.revision ||
            snapshot.stateToken != base.stateToken);
    final pending = _ownerPending;
    final editable =
        widget.active() &&
        !_reading &&
        pending == null &&
        widget.api?.hasUnconfirmedChange != true &&
        snapshot != null &&
        !stale;
    if (widget.compact) {
      return _compactPicker(snapshot, draft, pending, editable, stale);
    }
    final choices = <String, ConverseModelSelection>{
      'host': ConverseModelSelection.host(),
      if (snapshot != null)
        for (final profile in snapshot.catalogProfiles)
          profile.binding.identity: ConverseModelSelection.catalog(
            profile.binding,
            null,
          ),
      if (draft != null) draft.targetIdentity: draft,
    };
    final selectedProfile = snapshot == null || draft == null
        ? null
        : snapshot.profile(draft);
    final levels = selectedProfile?.reasoningOptions ?? const <String>[];
    return DesignPanel(
      child: Material(
        type: MaterialType.transparency,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Converse text model',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 8),
            const Text(
              'Choose the text model and reasoning used by new Converse turns and speech windows. Changes can be saved after conversation operations and cleanup have settled.',
            ),
            const SizedBox(height: 12),
            const Text(
              'Text only. Speech to text, text to speech and Decisions retain their separate model owners. Saving never tests, loads or calls a model.',
            ),
            if (widget.api == null || !widget.active())
              const Text(
                'Connect to the owner Converse service to inspect these options.',
              ),
            if (_reading) const LinearProgressIndicator(),
            if (_error != null) Text(_error!),
            if (_notice != null) Text(_notice!),
            if (pending != null) _pendingCard(pending),
            if (snapshot != null && draft != null) ...[
              const SizedBox(height: 16),
              Text(
                'Saved model: ${snapshot.selectedInfo?.model ?? 'Unavailable'}',
              ),
              if (snapshot.selectedInfo != null)
                Text(
                  '${snapshot.selectedInfo!.provider} · ${snapshot.selectedInfo!.local ? 'Local' : 'Cloud'} · Reasoning ${snapshot.selectedReasoning ?? 'Undeclared'}',
                ),
              if (snapshot.selectedError != null) Text(snapshot.selectedError!),
              if (snapshot.catalogError != null) Text(snapshot.catalogError!),
              if (snapshot.busy)
                const Text(
                  'Conversation operations or cleanup are active. You may prepare a draft; saving is unavailable until the host is idle.',
                ),
              if (stale) ...[
                const Text(
                  'Native settings changed while this draft was open. The original draft is retained. Review current values before saving.',
                ),
                TextButton(
                  onPressed: pending != null || !widget.active()
                      ? null
                      : () => _discard(snapshot, draft),
                  child: const Text('Use current saved selection'),
                ),
              ],
              const SizedBox(height: 16),
              DropdownButtonFormField<String>(
                key: ValueKey(('converse-target', draft.targetIdentity)),
                initialValue: draft.targetIdentity,
                isExpanded: true,
                decoration: const InputDecoration(
                  labelText: 'Model source and profile',
                  border: OutlineInputBorder(),
                ),
                items: [
                  for (final entry in choices.entries)
                    DropdownMenuItem(
                      value: entry.key,
                      enabled: entry.value.source == 'host'
                          ? snapshot.hostSelectable
                          : snapshot.profile(entry.value)?.selectable == true,
                      child: Text(
                        entry.value.source == 'host'
                            ? 'Host configuration · ${snapshot.hostInfo?.model ?? 'Unavailable'}'
                            : '${snapshot.profile(entry.value)?.label ?? 'Unavailable · ${entry.value.catalog!.id}'} · Generation ${entry.value.catalog!.generation}',
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                ],
                onChanged: editable
                    ? (value) {
                        if (value == null || !_canEdit(snapshot, draft)) {
                          return;
                        }
                        final choice = choices[value];
                        if (choice != null) {
                          setState(
                            () => _draft = choice.source == 'host'
                                ? ConverseModelSelection.host()
                                : ConverseModelSelection.catalog(
                                    choice.catalog!,
                                    null,
                                  ),
                          );
                        }
                      }
                    : null,
              ),
              const SizedBox(height: 16),
              if (draft.source == 'catalog') ...[
                DropdownButtonFormField<String>(
                  key: ValueKey((
                    'converse-reasoning',
                    draft.targetIdentity,
                    draft.reasoning,
                  )),
                  initialValue: draft.reasoning ?? 'profile',
                  isExpanded: true,
                  decoration: const InputDecoration(
                    labelText: 'Reasoning',
                    border: OutlineInputBorder(),
                  ),
                  items: [
                    const DropdownMenuItem(
                      value: 'profile',
                      child: Text('Use provider profile setting'),
                    ),
                    if (draft.reasoning != null &&
                        !levels.contains(draft.reasoning))
                      DropdownMenuItem(
                        value: draft.reasoning,
                        enabled: false,
                        child: Text('${draft.reasoning} · Unavailable'),
                      ),
                    for (final level in levels)
                      DropdownMenuItem(value: level, child: Text(level)),
                  ],
                  onChanged: editable && selectedProfile?.selectable == true
                      ? (value) {
                          if (value != null && _canEdit(snapshot, draft)) {
                            setState(
                              () => _draft = ConverseModelSelection.catalog(
                                draft.catalog!,
                                value == 'profile' ? null : value,
                              ),
                            );
                          }
                        }
                      : null,
                ),
                const SizedBox(height: 12),
                Text(
                  selectedProfile?.reason ??
                      'This exact provider generation is unavailable. A newer generation is a separate explicit choice.',
                ),
                ExpansionTile(
                  tilePadding: EdgeInsets.zero,
                  title: const Text('Exact provider generation'),
                  children: [
                    SelectableText(
                      'Profile ${draft.catalog!.id}\nGeneration ${draft.catalog!.generation}\nConfiguration ${draft.catalog!.configurationSha256}',
                    ),
                  ],
                ),
              ] else
                Text(
                  'Host reasoning: ${snapshot.hostReasoning ?? 'Undeclared'}. Choose a catalog profile to customize reasoning.',
                ),
              const Text('Readiness: unknown · Qualification: not checked'),
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                children: [
                  FilledButton(
                    onPressed:
                        editable &&
                            !snapshot.busy &&
                            _dirty &&
                            snapshot.permits(draft) &&
                            snapshot.revision < converseModelRevisionLimit &&
                            _pending.length < 16
                        ? () => _save(snapshot, draft)
                        : null,
                    child: const Text('Save text model'),
                  ),
                  TextButton(
                    onPressed: !widget.active() || pending != null || !_dirty
                        ? null
                        : () => _discard(snapshot, draft),
                    child: const Text('Discard draft'),
                  ),
                ],
              ),
              ExpansionTile(
                tilePadding: EdgeInsets.zero,
                title: const Text('Native operation ownership'),
                children: [
                  SelectableText(
                    'Revision ${snapshot.revision}\nInstance ${snapshot.instanceId}\n${snapshot.operationState.entries.map((entry) => '${entry.key}: ${entry.value}').join('\n')}',
                  ),
                ],
              ),
            ],
            TextButton.icon(
              onPressed: widget.api == null || !widget.active() || _reading
                  ? null
                  : () => unawaited(_load()),
              icon: const Icon(Icons.refresh),
              label: const Text('Refresh model options'),
            ),
            if (_pending.length >= 16)
              const Text(
                'The local unresolved-save limit is reached. No unknown operation is discarded to make room.',
              ),
          ],
        ),
      ),
    );
  }

  Widget _compactPicker(
    ConverseModelsSnapshot? snapshot,
    ConverseModelSelection? draft,
    _PendingConverseModel? pending,
    bool editable,
    bool stale,
  ) {
    final query = _search.text.trim().toLowerCase();
    final favorites = widget.favorites ?? _localFavorites;
    final groups = <String, List<ConverseCatalogProfile>>{};
    for (final profile
        in snapshot?.catalogProfiles ?? <ConverseCatalogProfile>[]) {
      final searchable =
          '${profile.label} ${profile.id} ${profile.provider.provider} ${profile.provider.model}'
              .toLowerCase();
      if (query.isEmpty || searchable.contains(query)) {
        groups.putIfAbsent(profile.provider.provider, () => []).add(profile);
      }
    }
    final providers = groups.keys.toList()..sort();
    for (final profiles in groups.values) {
      profiles.sort((a, b) {
        final favoriteOrder =
            (favorites.contains(b.binding.identity) ? 1 : 0) -
            (favorites.contains(a.binding.identity) ? 1 : 0);
        return favoriteOrder != 0 ? favoriteOrder : a.label.compareTo(b.label);
      });
    }
    final canSave =
        editable &&
        snapshot != null &&
        !snapshot.busy &&
        snapshot.revision < converseModelRevisionLimit &&
        _pending.length < 16;
    final hostMatches =
        snapshot != null &&
        (query.isEmpty ||
            'host configuration ${snapshot.hostInfo?.model ?? ''} ${snapshot.hostInfo?.provider ?? ''}'
                .toLowerCase()
                .contains(query));
    final keyboardChoices = <String>[
      if (canSave &&
          hostMatches &&
          snapshot.hostSelectable &&
          snapshot.selection.source != 'host')
        'host',
      if (canSave)
        for (final provider in providers)
          for (final profile in groups[provider]!)
            if (profile.selectable &&
                snapshot.selection.targetIdentity != profile.binding.identity)
              profile.binding.identity,
    ];
    FocusNode focusFor(String id) =>
        _choiceFocus.putIfAbsent(id, () => FocusNode(debugLabel: 'Model $id'));
    void moveChoice(int direction) {
      if (keyboardChoices.isEmpty) return;
      final current = keyboardChoices.indexWhere(
        (id) => _choiceFocus[id]?.hasPrimaryFocus == true,
      );
      final next = current < 0
          ? (direction > 0 ? 0 : keyboardChoices.length - 1)
          : (current + direction).clamp(0, keyboardChoices.length - 1);
      focusFor(keyboardChoices[next]).requestFocus();
    }

    Widget row(
      String title,
      String subtitle,
      ConverseModelSelection choice,
      bool available,
      String details, {
      String? reasoningBadge,
    }) {
      final selected =
          snapshot?.selection.targetIdentity == choice.targetIdentity;
      final enabled = canSave && available && !selected && draft != null;
      final expanded = _expandedChoices.contains(choice.targetIdentity);
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          ListTile(
            key: ValueKey(('quick-model', choice.targetIdentity)),
            focusNode: focusFor(choice.targetIdentity),
            dense: true,
            title: Text(title),
            subtitle: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(subtitle),
                if (reasoningBadge != null)
                  Container(
                    margin: const EdgeInsets.only(top: 4),
                    padding: const EdgeInsets.symmetric(
                      horizontal: 6,
                      vertical: 2,
                    ),
                    decoration: BoxDecoration(
                      color: Theme.of(context).colorScheme.secondaryContainer,
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Text(
                      reasoningBadge,
                      style: Theme.of(context).textTheme.labelSmall,
                    ),
                  ),
              ],
            ),
            isThreeLine: reasoningBadge != null,
            selected: selected,
            leading: choice.source != 'catalog'
                ? null
                : IconButton(
                    tooltip: favorites.contains(choice.targetIdentity)
                        ? 'Remove $title from session favorites'
                        : 'Favorite $title for this session',
                    icon: Icon(
                      favorites.contains(choice.targetIdentity)
                          ? Icons.star
                          : Icons.star_border,
                    ),
                    onPressed: () => setState(() {
                      if (!favorites.add(choice.targetIdentity)) {
                        favorites.remove(choice.targetIdentity);
                      }
                    }),
                  ),
            trailing: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (selected)
                  const Icon(Icons.check, semanticLabel: 'Selected'),
                IconButton(
                  tooltip: '${expanded ? 'Hide' : 'Show'} details for $title',
                  icon: Icon(
                    expanded ? Icons.expand_less : Icons.chevron_right,
                  ),
                  onPressed: () => setState(() {
                    if (expanded) {
                      _expandedChoices.remove(choice.targetIdentity);
                    } else {
                      _expandedChoices.add(choice.targetIdentity);
                    }
                  }),
                ),
              ],
            ),
            enabled: enabled,
            onTap: !enabled
                ? null
                : () {
                    if (!_canEdit(snapshot, draft)) return;
                    setState(() {
                      _draft = choice;
                      _notice = null;
                    });
                    _save(snapshot, choice);
                  },
          ),
          if (expanded)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
              child: Text(
                details,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
        ],
      );
    }

    return Material(
      type: MaterialType.transparency,
      child: CallbackShortcuts(
        bindings: <ShortcutActivator, VoidCallback>{
          const SingleActivator(LogicalKeyboardKey.arrowDown): () =>
              moveChoice(1),
          const SingleActivator(LogicalKeyboardKey.arrowUp): () =>
              moveChoice(-1),
        },
        child: SingleChildScrollView(
          key: const ValueKey('quick-model-picker'),
          primary: false,
          padding: const EdgeInsets.all(12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Choose a Converse model',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 12),
              TextField(
                key: const ValueKey('quick-model-search'),
                autofocus: true,
                controller: _search,
                onChanged: (_) => setState(() {}),
                decoration: const InputDecoration(
                  prefixIcon: Icon(Icons.search),
                  labelText: 'Search models',
                ),
              ),
              if (_reading) const LinearProgressIndicator(),
              if (widget.api == null || !widget.active())
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 12),
                  child: Text(
                    'Connect and authenticate to inspect Converse models.',
                  ),
                ),
              if (_error != null) Text(_error!),
              if (_notice != null) Text(_notice!),
              if (pending != null) _pendingCard(pending),
              if (snapshot != null) ...[
                if (snapshot.catalogError != null) Text(snapshot.catalogError!),
                if (snapshot.busy)
                  const Text(
                    'Model changes are unavailable while Converse operations are active.',
                  ),
                if (stale)
                  const Text(
                    'Model settings changed. Refresh or open model options to review the retained draft.',
                  ),
                if (hostMatches) ...[
                  const SizedBox(height: 12),
                  Text(
                    'Host configuration',
                    style: Theme.of(context).textTheme.labelLarge,
                  ),
                  row(
                    snapshot.hostInfo?.model ?? 'Unavailable',
                    snapshot.hostInfo == null
                        ? 'Host model unavailable'
                        : '${snapshot.hostInfo!.provider} · ${snapshot.hostInfo!.local ? 'Local' : 'Cloud'} · Readiness unknown',
                    ConverseModelSelection.host(),
                    snapshot.hostSelectable,
                    'Host reasoning: ${snapshot.hostReasoning ?? 'Undeclared'}. Readiness is not verified.',
                  ),
                ],
                for (final provider in providers) ...[
                  const SizedBox(height: 8),
                  Text(provider, style: Theme.of(context).textTheme.labelLarge),
                  for (final profile in groups[provider]!)
                    row(
                      profile.label,
                      '${profile.provider.model} · ${profile.provider.local ? 'Local' : 'Cloud'} · ${profile.selectable ? 'Readiness unknown' : profile.reason}',
                      ConverseModelSelection.catalog(profile.binding, null),
                      profile.selectable,
                      '${profile.reason}\nReasoning: ${profile.reasoning ?? 'Profile default'}\nAvailable reasoning options: ${profile.reasoningOptions.isEmpty ? 'Undeclared' : profile.reasoningOptions.join(', ')}\nProfile ${profile.id} · Generation ${profile.binding.generation}',
                      reasoningBadge: profile.reasoning == null
                          ? null
                          : 'Reasoning ${profile.reasoning!.toUpperCase()}',
                    ),
                ],
                if (query.isNotEmpty && groups.isEmpty && !hostMatches)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 16),
                    child: Text('No models match your search.'),
                  ),
                const SizedBox(height: 8),
                const Text('Selecting a model does not verify its readiness.'),
              ],
              TextButton.icon(
                onPressed: widget.api == null || !widget.active() || _reading
                    ? null
                    : () => unawaited(_load()),
                icon: const Icon(Icons.refresh),
                label: const Text('Refresh models'),
              ),
              if (widget.onAddModel != null)
                TextButton.icon(
                  onPressed: widget.onAddModel,
                  icon: const Icon(Icons.add),
                  label: const Text('Add custom model…'),
                ),
              if (widget.onOpenModelOptions != null)
                TextButton.icon(
                  onPressed: widget.onOpenModelOptions,
                  icon: const Icon(Icons.settings_outlined),
                  label: const Text('Edit models…'),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
