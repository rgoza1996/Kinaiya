import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../api/stt_model_client.dart';
import '../api/stt_model_settings.dart';
import 'design_tokens.dart';
import 'options_stt_admissions.dart';

/// Embedded speech-to-text selection, independent of text and speech output.
class OptionsSttModel extends StatefulWidget {
  const OptionsSttModel({
    super.key,
    required this.api,
    required this.active,
    this.onChanged,
  });
  final SttModelsApi? api;
  final bool Function() active;
  final VoidCallback? onChanged;
  @override
  State<OptionsSttModel> createState() => _OptionsSttModelState();
}

class _SttDraft {
  _SttDraft(this.owner, this.base) : choice = base.selection;
  final SttModelsApi owner;
  final SttModelsSnapshot base;
  SttModelSelection choice;
  int edit = 0;
  bool get dirty => !choice.matches(base.selection);
}

class _PendingSttModel {
  _PendingSttModel(this.owner, this.body);
  final SttModelsApi owner;
  final Map<String, Object?> body;
  bool inFlight = false, dispatched = false;
  SttModelLookup? lookup;
  String notice = 'Saving the transcription model selection…';
}

class _OptionsSttModelState extends State<OptionsSttModel> {
  static final _drafts = <_SttDraft>[];
  static final _pending = <_PendingSttModel>[];
  static final _changes = ValueNotifier<int>(0);
  SttModelsSnapshot? _snapshot;
  _SttDraft? _draft;
  String? _error, _notice;
  bool _reading = false;
  int _read = 0;

  _PendingSttModel? get _ownerPending {
    final api = widget.api;
    if (api == null) return null;
    for (final item in _pending) {
      if (api.sameOwner(item.owner)) return item;
    }
    return null;
  }

  _SttDraft? _ownerDraft(SttModelsApi api) {
    for (final item in _drafts) {
      if (api.sameOwner(item.owner)) return item;
    }
    return null;
  }

  bool _current(SttModelsApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();
  void _update() {
    if (!mounted) return;
    final api = widget.api;
    setState(() => _draft = api == null ? null : _ownerDraft(api));
  }

  @override
  void initState() {
    super.initState();
    _changes.addListener(_update);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant OptionsSttModel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api)) {
      _read++;
      _snapshot = null;
      _draft = null;
      _error = _notice = null;
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    _changes.removeListener(_update);
    super.dispose();
  }

  Future<void> _load() async {
    final api = widget.api, active = widget.active, read = ++_read;
    if (api == null || !active()) return;
    setState(() {
      _reading = true;
      _error = null;
      _draft = _ownerDraft(api);
    });
    try {
      final next = await api.inspect();
      if (!_current(api, active) || read != _read) return;
      var draft = _ownerDraft(api);
      if (draft == null || !draft.dirty) {
        if (draft != null) _drafts.remove(draft);
        if (_drafts.length >= 16) {
          _drafts.removeWhere(
            (item) =>
                !item.dirty &&
                !_pending.any((p) => item.owner.sameOwner(p.owner)),
          );
        }
        if (_drafts.length < 16) {
          draft = _SttDraft(api, next);
          _drafts.add(draft);
        } else {
          draft = null;
        }
      }
      setState(() {
        _snapshot = next;
        _draft = draft;
        if (draft == null) {
          _error =
              'Retained draft capacity is full. Resolve or discard an earlier transcription model draft.';
        }
      });
      _changes.value++;
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(() {
          _snapshot = null;
          _error =
              'STT model options are unavailable. Drafts and unconfirmed saves are retained.';
        });
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  bool _renderCurrent(SttModelsSnapshot snapshot, _SttDraft draft, int edit) =>
      mounted &&
      widget.api != null &&
      widget.active() &&
      identical(_snapshot, snapshot) &&
      identical(_draft, draft) &&
      _drafts.contains(draft) &&
      identical(_ownerDraft(widget.api!), draft) &&
      draft.edit == edit;
  bool _editable(SttModelsSnapshot snapshot, _SttDraft draft, int edit) =>
      widget.api != null &&
      _renderCurrent(snapshot, draft, edit) &&
      !_reading &&
      _ownerPending == null &&
      widget.api!.hasUnconfirmedChange == false &&
      snapshot.sameBase(draft.base);

  void _discard(SttModelsSnapshot snapshot, _SttDraft draft, int edit) {
    if (!_renderCurrent(snapshot, draft, edit) || _ownerPending != null) return;
    _drafts.remove(draft);
    _draft = null;
    _changes.value++;
    unawaited(_load());
  }

  void _save(SttModelsSnapshot snapshot, _SttDraft draft, int edit) {
    final api = widget.api, active = widget.active;
    if (api == null ||
        !_editable(snapshot, draft, edit) ||
        !draft.dirty ||
        snapshot.busy ||
        !snapshot.permits(draft.choice) ||
        _pending.length >= 16) {
      return;
    }
    final choice = draft.choice;
    final item = _PendingSttModel(
      api,
      snapshot.change(choice, const Uuid().v4()),
    );
    _pending.add(item);
    _changes.value++;
    // Synchronous listeners may reconnect, edit the shared draft, or replace the page.
    if (!_current(api, active) ||
        !_renderCurrent(snapshot, draft, edit) ||
        !draft.choice.matches(choice) ||
        !snapshot.sameBase(draft.base)) {
      _pending.remove(item);
      _changes.value++;
      return;
    }
    unawaited(
      _send(
        item,
        api,
        first: true,
        firstCurrent: () =>
            _renderCurrent(snapshot, draft, edit) &&
            draft.choice.matches(choice) &&
            snapshot.sameBase(draft.base),
      ),
    );
  }

  void _confirmed(
    _PendingSttModel item,
    SttModelReceipt receipt,
    SttModelsApi api,
    bool Function() active,
  ) {
    if (!receipt.matches(item.body)) throw const FormatException();
    _pending.remove(item);
    final draft = _ownerDraft(api);
    if (draft != null &&
        draft.base.backendId == item.body['expected_backend'] &&
        draft.base.instanceId == item.body['expected_instance'] &&
        draft.base.revision == item.body['expected_revision'] &&
        draft.base.stateToken == item.body['expected_state'] &&
        draft.choice.matches(
          SttModelSelection.fromJson(item.body['selection']),
        )) {
      _drafts.remove(draft);
    }
    if (_current(api, active)) {
      setState(
        () => _notice =
            'Transcription model selection saved at revision ${receipt.revision}. Readiness has not been checked.',
      );
      unawaited(_load());
      widget.onChanged?.call();
    }
  }

  Future<void> _send(
    _PendingSttModel item,
    SttModelsApi api, {
    bool first = false,
    bool retry = false,
    bool release = false,
    bool Function()? firstCurrent,
  }) async {
    if (!api.sameOwner(item.owner) ||
        item.inFlight ||
        !_pending.contains(item)) {
      return;
    }
    final active = widget.active;
    item.inFlight = true;
    item.lookup = null;
    item.notice = first
        ? 'Saving the exact transcription model selection…'
        : 'Looking up the original request…';
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
        if (!_current(api, active) || !_pending.contains(item)) return;
        item.lookup = lookup;
        if (release) {
          if (!lookup.fenced) {
            item.notice =
                'The earlier outcome is unknown. The original request is not natively fenced.';
            return;
          }
          api.releaseFenced(item.body, lookup);
          _pending.remove(item);
          setState(
            () => _notice =
                'Local tracking released. The earlier outcome remains unknown; the unchanged request can make no new commit. No work was cancelled or rolled back.',
          );
          unawaited(_load());
          widget.onChanged?.call();
          return;
        }
        if (!retry || lookup.fenced) {
          item.notice = lookup.fenced
              ? 'The earlier outcome is unknown. Native authority confirms this unchanged request can make no new commit.'
              : 'No exact receipt is currently visible. The earlier outcome remains unknown.';
          return;
        }
      }
      if (!_current(api, active) || (first && firstCurrent?.call() != true)) {
        if (first && !item.dispatched) _pending.remove(item);
        return;
      }
      final receipt = await api.change(item.body);
      _confirmed(item, receipt, api, active);
    } on SttModelFailure catch (error) {
      item.dispatched |= error.dispatched;
      if (first && !item.dispatched && !api.hasUnconfirmedChange) {
        _pending.remove(item);
        if (_current(api, active)) {
          setState(
            () => _error =
                'The selection was not dispatched. Your draft is retained.',
          );
          widget.onChanged?.call();
        }
      } else {
        item.notice =
            'The save outcome is unknown. Its exact original request is retained for native lookup.';
      }
    } catch (_) {
      item.dispatched = true;
      item.notice =
          'The response could not be confirmed. The exact original request is retained.';
    } finally {
      item.inFlight = false;
      _changes.value++;
      if (_current(api, active)) {
        setState(() {});
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = widget.api,
        snapshot = _snapshot,
        draft = _draft,
        pending = _ownerPending;
    final children = <Widget>[
      Row(
        children: [
          Expanded(
            child: Text(
              'Speech to text',
              style: Theme.of(context).textTheme.titleLarge,
            ),
          ),
          IconButton(
            tooltip: 'Refresh transcription model declarations',
            onPressed: api != null && widget.active() && !_reading
                ? _load
                : null,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      const Text(
        'Speech transcription is selected independently of the dialogue model and text to speech.',
      ),
      const SizedBox(height: 12),
      if (api == null)
        const Text(
          'Connect to the Converse service to inspect its independent STT selection. Text-to-speech settings remain separate.',
        ),
      if (_reading) const LinearProgressIndicator(),
      if (_error != null) Text(_error!),
      if (_notice != null) Text(_notice!),
    ];
    if (snapshot != null && draft != null) {
      final edit = draft.edit, choice = draft.choice;
      final enabled = _editable(snapshot, draft, edit);
      final choices = <String, SttModelSelection>{
        'host': SttModelSelection.host(),
        for (final profile in snapshot.profiles)
          profile.binding.identity: SttModelSelection.catalog(profile.binding),
        choice.identity: choice,
      };
      children.addAll([
        const SizedBox(height: 12),
        Text(
          'Saved selection: ${snapshot.selection.source == 'host' ? 'Host speech-to-text configuration' : '${snapshot.selection.catalog!.id} · generation ${snapshot.selection.catalog!.generation}'}',
        ),
        if (snapshot.selectedInfo case final provider?)
          Text(
            '${provider.provider} · ${provider.model} · ${provider.local ? 'Local declaration' : 'Cloud declaration'}',
          ),
        if (snapshot.selectedInfo == null && snapshot.selectedAvailable)
          const Text(
            'The selected host transcriber is available, but its model identity is undeclared. Readiness has not been checked.',
          ),
        if (!snapshot.selectedAvailable)
          const Text(
            'The saved STT selection is unavailable. No alternate transcriber is substituted.',
          ),
        if (snapshot.selectedError != null) Text(snapshot.selectedError!),
        if (snapshot.catalogError != null) Text(snapshot.catalogError!),
        const SizedBox(height: 12),
        DropdownButtonFormField<String>(
          key: ValueKey(
            '${snapshot.backendId}:${snapshot.instanceId}:${snapshot.revision}:${choice.identity}:$edit',
          ),
          initialValue: choice.identity,
          isExpanded: true,
          decoration: const InputDecoration(labelText: 'Transcription model'),
          items: [
            DropdownMenuItem(
              value: 'host',
              enabled: snapshot.hostSelectable,
              child: const Text('Host speech-to-text configuration'),
            ),
            for (final profile in snapshot.profiles)
              DropdownMenuItem(
                value: profile.binding.identity,
                enabled: profile.selectable,
                child: Text(
                  '${profile.label} · generation ${profile.binding.generation}${profile.selectable ? ' · Meta transcription' : ' · unavailable'}',
                ),
              ),
            if (choice.source == 'catalog' &&
                !snapshot.profiles.any(
                  (p) => p.binding.identity == choice.identity,
                ))
              DropdownMenuItem(
                value: choice.identity,
                enabled: false,
                child: Text(
                  '${choice.catalog!.id} · generation ${choice.catalog!.generation} · unavailable retained choice',
                ),
              ),
          ],
          onChanged: enabled
              ? (id) {
                  if (!_editable(snapshot, draft, edit) || id == null) return;
                  final next = choices[id];
                  if (next == null || !snapshot.permits(next)) return;
                  draft.choice = next;
                  draft.edit++;
                  _changes.value++;
                }
              : null,
        ),
        const SizedBox(height: 8),
        const Text(
          'Catalog selection supports exact Meta STT generations only. Saving does not transcribe audio, test readiness, grant cloud disclosure, or change text to speech.',
        ),
        if (!snapshot.sameBase(draft.base))
          const Text(
            'This draft belongs to earlier native authority. Discard it explicitly before selecting from refreshed declarations.',
          ),
        if (snapshot.busy)
          Text(
            'Selection changes wait for actual Converse work and cleanup to settle. ${snapshot.operationState.entries.where((e) => e.value > 0).map((e) => '${e.key}: ${e.value}').join(' · ')}',
          ),
        const SizedBox(height: 12),
        Wrap(
          spacing: 12,
          children: [
            FilledButton(
              onPressed:
                  enabled &&
                      draft.dirty &&
                      !snapshot.busy &&
                      snapshot.permits(choice)
                  ? () => _save(snapshot, draft, edit)
                  : null,
              child: const Text('Save transcription model selection'),
            ),
            OutlinedButton(
              onPressed: widget.active() && pending == null && draft.dirty
                  ? () => _discard(snapshot, draft, edit)
                  : null,
              child: const Text('Discard draft'),
            ),
          ],
        ),
        const SizedBox(height: 8),
        ExpansionTile(
          title: Text('Native revision ${snapshot.revision}'),
          children: [
            SelectableText(
              'Backend ${snapshot.backendId}\nInstance ${snapshot.instanceId}',
            ),
          ],
        ),
      ]);
    } else if (draft != null) {
      children.add(
        const Text(
          'Your earlier transcription model draft is retained while native metadata is unavailable.',
        ),
      );
    }
    if (pending != null && api != null) {
      children.add(
        Padding(
          padding: const EdgeInsets.only(top: 16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(pending.notice),
              ExpansionTile(
                title: const Text('Original save request'),
                children: [
                  SelectableText(
                    const JsonEncoder.withIndent('  ').convert(pending.body),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 12,
                runSpacing: 8,
                children: [
                  OutlinedButton(
                    onPressed: !pending.inFlight && widget.active()
                        ? () => _send(pending, api)
                        : null,
                    child: const Text('Check original save'),
                  ),
                  OutlinedButton(
                    onPressed: !pending.inFlight && widget.active()
                        ? () => _send(pending, api, retry: true)
                        : null,
                    child: const Text('Retry exact save'),
                  ),
                  if (pending.lookup?.fenced == true)
                    OutlinedButton(
                      onPressed: !pending.inFlight && widget.active()
                          ? () => _send(pending, api, release: true)
                          : null,
                      child: const Text('Release local tracking'),
                    ),
                ],
              ),
            ],
          ),
        ),
      );
    }
    children.add(
      OptionsSttAdmissions(
        api: api,
        active: widget.active,
        onSettled: () {
          if (mounted && widget.active()) unawaited(_load());
        },
      ),
    );
    return DesignPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: children,
      ),
    );
  }
}
