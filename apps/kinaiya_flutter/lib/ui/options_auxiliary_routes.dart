import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';
import '../api/auxiliary_routes.dart';
import '../api/auxiliary_route_client.dart';
import '../api/converse_models.dart' show ConverseCatalogProfile;
import 'design_tokens.dart';

class OptionsAuxiliaryRoutes extends StatefulWidget {
  const OptionsAuxiliaryRoutes({
    super.key,
    required this.api,
    required this.active,
    this.harness,
    this.allowConfigure = true,
  });
  final AuxiliaryRoutesApi? api;
  final bool Function() active;
  final String? harness;
  final bool allowConfigure;
  @override
  State<OptionsAuxiliaryRoutes> createState() => _OptionsAuxiliaryRoutesState();
}

class _PendingRoute {
  _PendingRoute(this.owner, this.body);
  final AuxiliaryRoutesApi owner;
  final Map<String, Object?> body;
  bool inFlight = false, dispatched = false;
  String notice = 'Saving the original route…';
  AuxiliaryRouteLookup? fence;
  AuxiliaryRoutesApi? fenceOwner;
}

class _OptionsAuxiliaryRoutesState extends State<OptionsAuxiliaryRoutes> {
  static final _pending = <_PendingRoute>[];
  static final _changes = ValueNotifier<int>(0);
  String _purpose = 'analysis';
  AuxiliaryRoutesSnapshot? _snapshot, _base;
  AuxiliaryRoute? _draft;
  bool _reading = false;
  int _read = 0;
  String? _error, _notice;
  AuxiliaryScope get _scope => AuxiliaryScope(_purpose, widget.harness);
  _PendingRoute? get _ownerPending {
    final api = widget.api;
    if (api != null) {
      for (final item in _pending) {
        if (api.sameOwner(item.owner)) return item;
      }
    }
    return null;
  }

  bool get _dirty => _base != null && !auxiliaryRouteSame(_draft, _base!.route);
  bool _current(AuxiliaryRoutesApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();
  bool _editable(AuxiliaryRoutesSnapshot rendered) =>
      mounted &&
      widget.active() &&
      !_reading &&
      identical(_snapshot, rendered) &&
      _base != null &&
      rendered.writable &&
      _base!.backend == rendered.backend &&
      _base!.instance == rendered.instance &&
      _base!.revision == rendered.revision &&
      _base!.stateToken == rendered.stateToken &&
      _ownerPending == null &&
      widget.api?.hasUnconfirmedChange == false;

  @override
  void initState() {
    super.initState();
    _changes.addListener(_changed);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant OptionsAuxiliaryRoutes oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api) ||
        oldWidget.harness != widget.harness) {
      _read++;
      _snapshot = _base = null;
      _draft = null;
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    _changes.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted && widget.active()) setState(() {});
  }

  Future<void> _load({bool replaceDraft = false}) async {
    final api = widget.api,
        active = widget.active,
        scope = _scope,
        read = ++_read;
    if (api == null || !active()) return;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final next = await api.inspect(scope);
      if (!_current(api, active) || read != _read || !_scope.matches(scope)) {
        return;
      }
      setState(() {
        if (replaceDraft || _base == null || !_dirty) {
          _base = next;
          _draft = next.route;
        }
        _snapshot = next;
      });
    } catch (_) {
      if (_current(api, active) && read == _read) {
        setState(() {
          _snapshot = null;
          _error =
              'Ordered auxiliary routes are unavailable. Existing drafts and unresolved saves are retained; an unreadable route store cannot authorize a legacy replacement.';
        });
      }
    } finally {
      if (_current(api, active) && read == _read) {
        setState(() => _reading = false);
      }
    }
  }

  void _replace(
    AuxiliaryRoutesSnapshot rendered,
    AuxiliaryRoute? prior,
    List<AuxiliaryCandidate> rows,
  ) {
    if (!_editable(rendered) ||
        !identical(_draft, prior) ||
        (!widget.allowConfigure && rows.isNotEmpty)) {
      return;
    }
    setState(() => _draft = rows.isEmpty ? null : AuxiliaryRoute(rows));
  }

  void _save(AuxiliaryRoutesSnapshot rendered, AuxiliaryRoute? renderedDraft) {
    final api = widget.api, active = widget.active;
    if (api == null ||
        !_editable(rendered) ||
        !identical(_draft, renderedDraft) ||
        !_dirty ||
        (!widget.allowConfigure && renderedDraft != null) ||
        !rendered.permits(renderedDraft) ||
        _pending.length >= 16) {
      return;
    }
    final item = _PendingRoute(
      api,
      rendered.change(renderedDraft, const Uuid().v4()),
    );
    _pending.add(item);
    _changes.value++;
    if (!_current(api, active)) {
      _pending.remove(item);
      _changes.value++;
      return;
    }
    unawaited(_send(item, api, first: true));
  }

  void _confirmed(
    _PendingRoute item,
    AuxiliaryRouteReceipt receipt,
    AuxiliaryRoutesApi api,
    bool Function() active,
  ) {
    if (!receipt.matches(item.body)) throw const FormatException();
    _pending.remove(item);
    if (_current(api, active)) {
      final originalScope = AuxiliaryScope.fromJson(item.body['scope']);
      final originalRoute = item.body['route'] == null
          ? null
          : AuxiliaryRoute.fromJson(item.body['route']);
      final ownDraft =
          _scope.matches(originalScope) &&
          auxiliaryRouteSame(_draft, originalRoute);
      setState(() {
        _notice =
            'Route saved at revision ${receipt.revision}. New tasks capture it; readiness has not been checked.';
        if (ownDraft) {
          _base = null;
          _draft = null;
        }
      });
      unawaited(_load(replaceDraft: ownDraft));
    }
  }

  Future<void> _send(
    _PendingRoute item,
    AuxiliaryRoutesApi api, {
    bool first = false,
    bool retry = false,
    bool release = false,
  }) async {
    if (item.inFlight ||
        !api.sameOwner(item.owner) ||
        !_pending.contains(item)) {
      return;
    }
    final active = widget.active;
    item.inFlight = true;
    item.fence = null;
    item.fenceOwner = null;
    item.notice = first
        ? 'Saving the exact ordered route…'
        : 'Looking up the original request…';
    _changes.value++;
    try {
      if (!_current(api, active)) {
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
              'The historical outcome remains unknown. Native authority confirms the unchanged original request can make no new commit.';
          if (release) {
            api.releaseFenced(item.body, lookup);
            _pending.remove(item);
            final originalScope = AuxiliaryScope.fromJson(item.body['scope']);
            final originalRoute = item.body['route'] == null
                ? null
                : AuxiliaryRoute.fromJson(item.body['route']);
            final ownDraft =
                _scope.matches(originalScope) &&
                auxiliaryRouteSame(_draft, originalRoute);
            setState(() {
              _notice =
                  'Local blocking released. The earlier outcome is still unknown; nothing was cancelled or rolled back.';
              if (ownDraft) {
                _base = null;
                _draft = null;
              }
            });
            unawaited(_load(replaceDraft: ownDraft));
          }
          return;
        }
        item.notice =
            'The original outcome remains unknown and its CAS has not been fenced. Check again or retry the unchanged save.';
        if (!retry) return;
      }
      if (!_current(api, active)) return;
      final receipt = await api.change(item.body);
      _confirmed(item, receipt, api, active);
    } on AuxiliaryRouteFailure catch (error) {
      item.dispatched |= error.dispatched;
      if (first && !item.dispatched) {
        _pending.remove(item);
        if (_current(api, active)) {
          setState(
            () => _error =
                'The route save was not dispatched. Your draft is retained.',
          );
        }
      } else {
        item.notice =
            'The save outcome is unconfirmed. Its original scope, body and request key remain retained.';
      }
    } catch (_) {
      item.dispatched = true;
      item.notice =
          'The original save could not be confirmed. Inspect its exact native receipt before retrying.';
    } finally {
      item.inFlight = false;
      _changes.value++;
      if (_current(api, active)) setState(() {});
    }
  }

  Widget _pendingCard(_PendingRoute item) {
    final scope = AuxiliaryScope.fromJson(item.body['scope']);
    final api = widget.api;
    final enabled = api != null && widget.active() && !item.inFlight;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Unconfirmed route save · ${scope.purpose}',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            Text(
              scope.harness == null
                  ? 'Global auxiliary route'
                  : 'Harness ${scope.harness}',
            ),
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
              runSpacing: 8,
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

  Widget _candidate(
    AuxiliaryRoutesSnapshot snapshot,
    AuxiliaryRoute draft,
    int index,
    bool enabled,
  ) {
    final row = draft.candidates[index],
        profile = snapshot.profile(draft.candidates[index]);
    final used = draft.candidates
        .where((e) => !identical(e, row))
        .map((e) => e.binding.identity)
        .toSet();
    final choices = <String, ConverseCatalogProfile>{
      for (final p in snapshot.profiles) p.binding.identity: p,
    };
    final retained = !choices.containsKey(row.binding.identity);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    '${index + 1}. ${profile?.label ?? row.binding.id}',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                IconButton(
                  tooltip: 'Move earlier',
                  onPressed: enabled && index > 0
                      ? () {
                          final rows = List<AuxiliaryCandidate>.of(
                            draft.candidates,
                          );
                          final prior = rows[index - 1];
                          rows[index - 1] = row;
                          rows[index] = prior;
                          _replace(snapshot, draft, rows);
                        }
                      : null,
                  icon: const Icon(Icons.arrow_upward),
                ),
                IconButton(
                  tooltip: 'Move later',
                  onPressed: enabled && index + 1 < draft.candidates.length
                      ? () {
                          final rows = List<AuxiliaryCandidate>.of(
                            draft.candidates,
                          );
                          final next = rows[index + 1];
                          rows[index + 1] = row;
                          rows[index] = next;
                          _replace(snapshot, draft, rows);
                        }
                      : null,
                  icon: const Icon(Icons.arrow_downward),
                ),
                IconButton(
                  tooltip: 'Remove candidate',
                  onPressed: enabled
                      ? () {
                          final rows = List<AuxiliaryCandidate>.of(
                            draft.candidates,
                          )..removeAt(index);
                          _replace(snapshot, draft, rows);
                        }
                      : null,
                  icon: const Icon(Icons.remove_circle_outline),
                ),
              ],
            ),
            DropdownButtonFormField<String>(
              key: ValueKey('route-candidate-$index-${row.binding.identity}'),
              initialValue: row.binding.identity,
              isExpanded: true,
              decoration: const InputDecoration(
                labelText: 'Catalog generation',
                border: OutlineInputBorder(),
              ),
              items: [
                for (final p in choices.values)
                  DropdownMenuItem(
                    value: p.binding.identity,
                    enabled: p.selectable && !used.contains(p.binding.identity),
                    child: Text(
                      '${p.label} · g${p.binding.generation}${p.selectable ? '' : ' · unavailable'}',
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                if (retained)
                  DropdownMenuItem(
                    value: row.binding.identity,
                    enabled: false,
                    child: Text(
                      '${row.binding.id} · g${row.binding.generation} · retained / unavailable',
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
              ],
              onChanged: enabled
                  ? (value) {
                      final p = choices[value];
                      if (p == null || !p.selectable || used.contains(value)) {
                        return;
                      }
                      final rows = List<AuxiliaryCandidate>.of(
                        draft.candidates,
                      );
                      rows[index] = AuxiliaryCandidate(
                        p.binding,
                        null,
                        p.provider.local ? 'local_only' : 'public_synthetic',
                      );
                      _replace(snapshot, draft, rows);
                    }
                  : null,
            ),
            const SizedBox(height: 10),
            SelectableText(
              'Generation ${row.binding.generation} · ${row.binding.configurationSha256}',
            ),
            const SizedBox(height: 10),
            DropdownButtonFormField<String>(
              key: ValueKey('route-reasoning-$index-${row.reasoning}'),
              initialValue: row.reasoning ?? 'configured',
              decoration: const InputDecoration(
                labelText: 'Reasoning',
                border: OutlineInputBorder(),
              ),
              items: [
                const DropdownMenuItem(
                  value: 'configured',
                  child: Text('Provider configuration'),
                ),
                for (final effort in {
                  ...?profile?.reasoningOptions,
                  if (row.reasoning != null) row.reasoning!,
                })
                  DropdownMenuItem(
                    value: effort,
                    enabled: profile?.reasoningOptions.contains(effort) == true,
                    child: Text(effort),
                  ),
              ],
              onChanged: enabled && profile?.selectable == true
                  ? (value) {
                      if (value == null) return;
                      final rows = List<AuxiliaryCandidate>.of(
                        draft.candidates,
                      );
                      rows[index] = AuxiliaryCandidate(
                        row.binding,
                        value == 'configured' ? null : value,
                        row.cloudScope,
                      );
                      _replace(snapshot, draft, rows);
                    }
                  : null,
            ),
            const SizedBox(height: 8),
            Text(
              'Disclosure ceiling: ${row.cloudScope == 'local_only'
                  ? 'Local only'
                  : row.cloudScope == 'public_synthetic'
                  ? 'Public synthetic only'
                  : 'Private cloud · not eligible in this version'}',
            ),
            const Text(
              'This ceiling grants no permission. Native task, source, resource and export checks still apply.',
            ),
            if (profile != null) Text(profile.reason),
            const Text('Qualification not checked · readiness unknown'),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final snapshot = _snapshot, draft = _draft, pending = _ownerPending;
    final enabled = snapshot != null && _editable(snapshot);
    final canDiscard =
        snapshot != null &&
        widget.active() &&
        !_reading &&
        _dirty &&
        pending == null &&
        widget.api?.hasUnconfirmedChange == false;
    final eligible =
        snapshot?.profiles
            .where(
              (p) =>
                  p.selectable &&
                  {'meta', 'lmstudio'}.contains(p.provider.provider),
            )
            .toList() ??
        <ConverseCatalogProfile>[];
    final used =
        draft?.candidates.map((c) => c.binding.identity).toSet() ?? <String>{};
    final additions = eligible
        .where((p) => !used.contains(p.binding.identity))
        .toList();
    return DesignPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            'Auxiliary fallback routes',
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: 8),
          Text(
            widget.harness == null
                ? 'Global routes for new Agent tasks.'
                : 'Routes for new tasks in harness ${widget.harness}.',
          ),
          const Text(
            'An explicit route replaces the single-model choice for its purpose. Up to three exact generations are tried in order, only after measured context overflow or confirmed availability failure before generation. Unknown generation outcomes stop the route.',
          ),
          const SizedBox(height: 16),
          DropdownButtonFormField<String>(
            key: ValueKey('route-purpose-$_purpose'),
            initialValue: _purpose,
            decoration: const InputDecoration(
              labelText: 'Route purpose',
              border: OutlineInputBorder(),
            ),
            items: [
              for (final purpose in auxiliaryPurposes)
                DropdownMenuItem(
                  value: purpose,
                  child: Text(purpose.replaceAll('_', ' ')),
                ),
            ],
            onChanged: _reading || _dirty || !widget.active()
                ? null
                : (value) {
                    if (value == null) return;
                    setState(() {
                      _purpose = value;
                      _base = _snapshot = null;
                      _draft = null;
                      _notice = null;
                    });
                    unawaited(_load());
                  },
          ),
          if (_reading) const LinearProgressIndicator(),
          if (_error != null) Text(_error!),
          if (_notice != null) Text(_notice!),
          if (pending != null) _pendingCard(pending),
          if (snapshot != null) ...[
            const SizedBox(height: 12),
            Text(
              'Source: ${snapshot.source} · native revision ${snapshot.revision}',
            ),
            if (!snapshot.writable)
              const Text(
                'This service instance has no current write authority. Use the active native service.',
              ),
            if (snapshot.catalogError != null) Text(snapshot.catalogError!),
            if (draft == null) ...[
              Text(
                snapshot.scope.harness == null
                    ? 'Inherit the existing single-model choice.'
                    : 'Inherit the global route, or existing single-model choice when no global route is configured.',
              ),
              if (snapshot.effective != null)
                Text(
                  'Effective inherited order: ${snapshot.effective!.candidates.map((c) => '${c.binding.id} g${c.binding.generation}').join(' → ')}',
                ),
            ] else
              for (var i = 0; i < draft.candidates.length; i++)
                _candidate(
                  snapshot,
                  draft,
                  i,
                  enabled && widget.allowConfigure,
                ),
            if (!widget.allowConfigure)
              const Text(
                'This harness is unavailable for new choices. You can inspect or remove its saved route override.',
              ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton.icon(
                  onPressed:
                      enabled &&
                          widget.allowConfigure &&
                          (draft?.candidates.length ?? 0) < 3 &&
                          additions.isNotEmpty
                      ? () {
                          final p = additions.first;
                          _replace(snapshot, draft, [
                            ...?draft?.candidates,
                            AuxiliaryCandidate(
                              p.binding,
                              null,
                              p.provider.local
                                  ? 'local_only'
                                  : 'public_synthetic',
                            ),
                          ]);
                        }
                      : null,
                  icon: const Icon(Icons.add),
                  label: const Text('Add candidate'),
                ),
                TextButton(
                  onPressed: enabled && draft != null
                      ? () => _replace(snapshot, draft, [])
                      : null,
                  child: Text(
                    widget.harness == null
                        ? 'Use single-model choice'
                        : 'Inherit global/default',
                  ),
                ),
                TextButton(
                  onPressed: canDiscard
                      ? () {
                          if (!mounted ||
                              !widget.active() ||
                              _reading ||
                              !identical(_snapshot, snapshot) ||
                              !identical(_draft, draft) ||
                              _ownerPending != null ||
                              widget.api?.hasUnconfirmedChange != false) {
                            return;
                          }
                          setState(() {
                            _base = snapshot;
                            _draft = snapshot.route;
                          });
                        }
                      : null,
                  child: const Text('Discard draft'),
                ),
                FilledButton(
                  onPressed:
                      enabled &&
                          _dirty &&
                          (widget.allowConfigure || draft == null) &&
                          snapshot.permits(draft)
                      ? () => _save(snapshot, draft)
                      : null,
                  child: const Text('Save route'),
                ),
              ],
            ),
            if (draft != null && !snapshot.permits(draft))
              const Text(
                'A retained candidate is unavailable or ineligible. Remove it or explicitly choose a current supported generation before saving.',
              ),
            if (!enabled && _dirty && pending == null)
              const Text(
                'The native revision changed. Refresh and explicitly discard this draft before saving a new choice.',
              ),
          ],
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: !_reading && widget.active()
                  ? () => unawaited(_load())
                  : null,
              icon: const Icon(Icons.refresh),
              label: const Text('Refresh route metadata'),
            ),
          ),
        ],
      ),
    );
  }
}
