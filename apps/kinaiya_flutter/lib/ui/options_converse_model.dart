import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
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
  });
  final ConverseModelsApi? api;
  final bool Function() active;
  final VoidCallback onInvalidate;
  final Future<void> Function() onSettled;
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
      _observed = _ownerPending == null ? [] : [_ownerPending!];
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
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
}
