import 'dart:async';

import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../api/decisions_model_client.dart';
import '../api/decisions_model_settings.dart';
import '../api/models.dart' show ContentScope;
import '../api/provider_client.dart';
import '../api/provider_settings.dart';
import 'design_tokens.dart';

class OptionsAgentAdvice extends StatefulWidget {
  const OptionsAgentAdvice({
    super.key,
    required this.api,
    required this.providers,
    required this.active,
  });

  final AgentAdviceSettingsApi? api;
  final ProviderApi? providers;
  final bool Function() active;

  @override
  State<OptionsAgentAdvice> createState() => _OptionsAgentAdviceState();
}

class _AdviceDraft {
  _AdviceDraft(this.owner, this.base)
    : source = base.selection,
      binding = base.nextLaunch.policy?.binding,
      instructions = base.nextLaunch.policy?.instructions ?? '',
      scope = base.nextLaunch.policy?.contentScope ?? ContentScope.localOnly,
      reasoning = base.nextLaunch.policy?.reasoning;

  final AgentAdviceSettingsApi owner;
  final AgentAdviceSettingsSnapshot base;
  AgentAdviceSource source;
  DecisionsCatalogBinding? binding;
  String instructions;
  ContentScope scope;
  String? reasoning;

  bool get dirty {
    if (source != base.selection) return true;
    if (source != AgentAdviceSource.saved) return false;
    final original = base.nextLaunch.policy!;
    return binding?.identity != original.binding.identity ||
        instructions != original.instructions ||
        scope != original.contentScope ||
        reasoning != original.reasoning;
  }

  AgentAdvicePolicy? get policy {
    final model = binding;
    if (source != AgentAdviceSource.saved || model == null) return null;
    try {
      return AgentAdvicePolicy(
        binding: model,
        instructions: instructions,
        contentScope: scope,
        reasoning: reasoning,
      );
    } on FormatException {
      return null;
    }
  }
}

class _OptionsAgentAdviceState extends State<OptionsAgentAdvice> {
  static final _drafts = <_AdviceDraft>[];
  static const _scopeLabels = {
    ContentScope.localOnly: 'Local only',
    ContentScope.publicSynthetic: 'Public or synthetic',
    ContentScope.privateCloudAuthorized: 'Private cloud authorized',
  };
  final _instructions = TextEditingController();
  StreamSubscription<void>? _changes;
  AgentAdviceSettingsSnapshot? _snapshot;
  _AdviceDraft? _draft;
  List<ProviderProfile>? _catalog;
  String? _readError, _notice;
  bool _reading = false;
  int _read = 0;

  @override
  void initState() {
    super.initState();
    _listen();
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant OptionsAgentAdvice oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api) ||
        !identical(oldWidget.providers, widget.providers)) {
      _read++;
      _snapshot = null;
      _draft = null;
      _catalog = null;
      _notice = _readError = null;
      _reading = false;
      _listen();
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _read++;
    unawaited(_changes?.cancel());
    _instructions.dispose();
    super.dispose();
  }

  void _listen() {
    unawaited(_changes?.cancel());
    final api = widget.api;
    if (api == null) return;
    var pending = api.pending;
    _changes = api.changes.listen((_) {
      if (!mounted || !identical(widget.api, api)) return;
      final finished = pending != null && api.pending == null;
      pending = api.pending;
      if (finished && _draft != null && !_drafts.contains(_draft)) {
        unawaited(_load());
      } else {
        setState(() {});
      }
    });
  }

  bool _current(AgentAdviceSettingsApi api, bool Function() active) =>
      mounted && identical(widget.api, api) && active() && widget.active();

  _AdviceDraft? _remember(
    AgentAdviceSettingsApi api,
    AgentAdviceSettingsSnapshot state, {
    bool replace = false,
  }) {
    final old = _drafts
        .where(
          (draft) =>
              api.sameOwner(draft.owner) &&
              draft.base.backendId == state.backendId,
        )
        .firstOrNull;
    if (old != null && old.dirty && !replace) return old;
    _drafts.remove(old);
    if (_drafts.length >= 16) {
      _drafts.removeWhere(
        (draft) => !draft.dirty && draft.owner.pending == null,
      );
    }
    if (_drafts.length >= 16) return null;
    final next = _AdviceDraft(api, state);
    _drafts.add(next);
    return next;
  }

  Future<List<ProviderProfile>?> _readCatalog(ProviderApi? providers) async {
    if (providers == null) return null;
    try {
      final state = await providers.inspect();
      return state.profiles
          .where(
            (profile) =>
                !profile.removed &&
                profile.measuredComplete &&
                profile.declaredRoles.contains('agent'),
          )
          .toList();
    } catch (_) {
      return null;
    }
  }

  Future<void> _load() async {
    final api = widget.api, active = widget.active, ticket = ++_read;
    if (api == null || !active()) return;
    setState(() {
      _reading = true;
      _readError = null;
    });
    final catalog = _readCatalog(widget.providers);
    try {
      final next = await api.inspect();
      final profiles = await catalog;
      if (!_current(api, active) || ticket != _read) return;
      final draft = _remember(api, next);
      if (!identical(_draft, draft)) {
        _instructions.text = draft?.instructions ?? '';
      }
      setState(() {
        _snapshot = next;
        _draft = draft;
        _catalog = profiles;
      });
    } catch (_) {
      if (_current(api, active) && ticket == _read) {
        setState(() {
          _snapshot = null;
          _readError = 'Agent advice settings could not be inspected.';
        });
      }
    } finally {
      if (_current(api, active) && ticket == _read) {
        setState(() => _reading = false);
      }
    }
  }

  void _useInspectedPolicy() {
    final api = widget.api, state = _snapshot;
    if (api == null || state == null) return;
    final draft = _remember(api, state, replace: true);
    _instructions.text = draft?.instructions ?? '';
    setState(() {
      _notice = null;
      _draft = draft;
    });
  }

  Future<void> _save() async {
    final api = widget.api, active = widget.active, draft = _draft;
    if (api == null || draft == null) return;
    bool current() => _current(api, active);
    final original = AgentAdvicePolicyChange(
      base: draft.base,
      key: const Uuid().v4(),
      source: draft.source,
      policy: draft.policy,
    );
    setState(() => _notice = null);
    try {
      await api.save(original, current: current);
      _drafts.remove(draft);
      if (current()) {
        setState(
          () => _notice =
              'Policy saved. Restart Agent and Decisions to apply it.',
        );
        await _load();
      }
    } on DecisionsModelFailure catch (error) {
      if (!current()) return;
      setState(() {
        _notice = api.pending != null
            ? 'The save is unconfirmed. Check the original request.'
            : error.status == 400
            ? 'Decisions refused this model or instruction disclosure selection.'
            : 'Settings could not be saved. Refresh to inspect the current policy.';
      });
      if (error.status == 409 && api.pending == null) await _load();
    }
  }

  Future<void> _lookup() async {
    final api = widget.api,
        active = widget.active,
        original = widget.api?.pending;
    if (api == null || original == null) return;
    bool current() => _current(api, active);
    try {
      final result = await api.lookup(current: current);
      if (result.receipt != null) {
        _drafts.removeWhere(
          (draft) =>
              api.sameOwner(draft.owner) &&
              draft.base.sameRevision(original.base),
        );
      }
      if (!current()) return;
      setState(() {
        _notice = result.receipt != null
            ? 'The original save was recorded.'
            : result.fenced
            ? 'The original save is retired. Use the current policy before saving again.'
            : 'The original save is still unconfirmed.';
      });
      if (result.receipt != null || result.fenced) await _load();
    } on DecisionsModelFailure catch (error) {
      if (!current()) return;
      setState(() {
        _notice = error.status == 409
            ? 'Reconnect to the original Decisions settings backend to check this save.'
            : 'The original save could not be confirmed.';
      });
    }
  }

  DecisionsCatalogBinding _binding(ProviderProfile profile) =>
      DecisionsCatalogBinding.fromJson({
        'id': profile.id,
        'generation': profile.generation,
        'configuration_sha256': profile.configurationSha256,
      });

  String _profileName(ProviderProfile profile) =>
      '${profile.label} · Generation ${profile.generation}';

  String _observed(AgentAdviceObservation state) {
    final source = switch (state.source) {
      'host' => 'Host policy',
      'saved' => 'Saved policy',
      'disabled' => 'Disabled',
      'invalid_host' => 'Invalid host policy',
      'unavailable' => 'Unavailable',
      _ => 'Unconfigured',
    };
    final model = state.policy?.binding;
    if (model == null) return source;
    final profile = _catalog
        ?.where((item) => _binding(item).identity == model.identity)
        .firstOrNull;
    final pin = profile != null
        ? _profileName(profile)
        : '${model.id} · Generation ${model.generation}';
    return '$source · $pin${state.catalogStatus == 'unavailable' ? ' · Core model unavailable' : ''}';
  }

  Widget _policyDetails(String title, AgentAdvicePolicy policy) => Material(
    type: MaterialType.transparency,
    child: ExpansionTile(
      tilePadding: EdgeInsets.zero,
      expandedCrossAxisAlignment: CrossAxisAlignment.stretch,
      title: Text(title),
      children: [
        Text('Reasoning · ${policy.reasoning ?? 'Provider default'}'),
        Text('Instruction disclosure · ${_scopeLabels[policy.contentScope]}'),
        const SizedBox(height: 8),
        const Text('Advice instructions'),
        SelectableText(
          policy.instructions.isEmpty ? 'Empty' : policy.instructions,
        ),
      ],
    ),
  );

  Widget _choice<T>(
    String label,
    T value,
    Map<T, String> options,
    ValueChanged<T?>? onChanged,
  ) => InputDecorator(
    decoration: InputDecoration(
      labelText: label,
      border: const OutlineInputBorder(),
    ),
    child: DropdownButtonHideUnderline(
      child: DropdownButton<T>(
        key: ValueKey('agent-advice-$label'),
        value: value,
        isExpanded: true,
        itemHeight: null,
        items: [
          for (final option in options.entries)
            DropdownMenuItem(value: option.key, child: Text(option.value)),
        ],
        onChanged: onChanged,
      ),
    ),
  );

  List<Widget> _policyFields(_AdviceDraft draft, bool enabled) {
    final model = draft.binding,
        profiles = _catalog ?? const <ProviderProfile>[];
    final options = {
      '': 'Select a Core model',
      for (final profile in profiles)
        _binding(profile).identity: _profileName(profile),
    };
    if (model != null && !options.containsKey(model.identity)) {
      options[model.identity] =
          '${model.id} · Generation ${model.generation} (unavailable)';
    }
    return [
      _choice<String>(
        'Core model',
        model?.identity ?? '',
        options,
        enabled && _catalog != null
            ? (value) => setState(() {
                final selected = profiles
                    .where((profile) => _binding(profile).identity == value)
                    .firstOrNull;
                if (value == '') {
                  draft.binding = null;
                } else if (selected != null) {
                  draft.binding = _binding(selected);
                }
              })
            : null,
      ),
      if (_catalog == null) const Text('Core catalog is unavailable.'),
      if (_catalog != null && profiles.isEmpty)
        const Text('Core has no measured text profiles for Agent advice.'),
      const SizedBox(height: 12),
      _choice<String>(
        'Reasoning',
        draft.reasoning ?? '',
        {
          '': 'Provider default',
          for (final level in agentAdviceReasoning)
            level: '${level[0].toUpperCase()}${level.substring(1)}',
        },
        enabled
            ? (value) =>
                  setState(() => draft.reasoning = value == '' ? null : value)
            : null,
      ),
      const SizedBox(height: 12),
      _choice<ContentScope>(
        'Instruction disclosure',
        draft.scope,
        _scopeLabels,
        enabled ? (value) => setState(() => draft.scope = value!) : null,
      ),
      const SizedBox(height: 12),
      TextField(
        key: const ValueKey('agent-advice-instructions'),
        controller: _instructions,
        enabled: enabled,
        minLines: 3,
        maxLines: 8,
        decoration: InputDecoration(
          labelText: 'Advice instructions',
          border: const OutlineInputBorder(),
          errorText: model != null && draft.policy == null
              ? 'Use valid text within the 8,192-byte limit.'
              : null,
        ),
        onChanged: (value) => setState(() => draft.instructions = value),
      ),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final api = widget.api, state = _snapshot, draft = _draft;
    final pending = api?.pending, busy = api?.busy == true;
    final stale =
        draft != null && state != null && !draft.base.sameRevision(state);
    final editable =
        widget.active() &&
        !_reading &&
        state?.writable == true &&
        pending == null;
    final completeSelection =
        draft != null &&
        (draft.source != AgentAdviceSource.saved ||
            (draft.policy != null &&
                _catalog?.any(
                      (profile) =>
                          _binding(profile).identity == draft.binding?.identity,
                    ) ==
                    true));
    return DesignPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (api == null)
            const Text('Connect to Decisions to manage Agent advice.'),
          if (_readError != null) Text(_readError!),
          if (_reading || busy) const LinearProgressIndicator(),
          if (state != null) ...[
            Text(
              'Active policy',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            Text(
              _observed(state.active),
              key: const ValueKey('agent-advice-active'),
            ),
            if (state.active.policy case final active?)
              _policyDetails('Active policy details', active),
            const SizedBox(height: 12),
            Text('Next launch', style: Theme.of(context).textTheme.titleMedium),
            Text(
              _observed(state.nextLaunch),
              key: const ValueKey('agent-advice-next-launch'),
            ),
            if (state.nextLaunch.policy case final next?)
              _policyDetails('Next-launch policy details', next),
            if (!state.active.sameDeclaration(state.nextLaunch))
              const Text(
                'Restart Agent and Decisions to apply the saved policy.',
              ),
            const SizedBox(height: 20),
            if (draft != null) ...[
              _choice<AgentAdviceSource>(
                'Policy source',
                draft.source,
                const {
                  AgentAdviceSource.host: 'Use host policy',
                  AgentAdviceSource.disabled: 'Disable advice',
                  AgentAdviceSource.saved: 'Use a Core model',
                },
                editable
                    ? (value) => setState(() => draft.source = value!)
                    : null,
              ),
              if (draft.source == AgentAdviceSource.saved) ...[
                const SizedBox(height: 12),
                ..._policyFields(draft, editable),
              ],
            ],
            if (draft == null || !state.writable)
              const Text('This policy is read-only.'),
            if (stale)
              const Text(
                'Settings changed while this draft was open. Use the current policy before saving.',
              ),
          ],
          if (pending != null && !busy)
            const Text('An original save is awaiting confirmation.'),
          if (_notice != null)
            Text(_notice!, key: const ValueKey('agent-advice-notice')),
          if (api != null) ...[
            const SizedBox(height: 16),
            Wrap(
              spacing: 12,
              runSpacing: 12,
              children: [
                if (state != null)
                  FilledButton(
                    onPressed:
                        editable && !stale && completeSelection && draft.dirty
                        ? _save
                        : null,
                    child: const Text('Save for next launch'),
                  ),
                if (pending != null)
                  OutlinedButton(
                    onPressed: !busy && widget.active() ? _lookup : null,
                    child: const Text('Check original save'),
                  ),
                if (stale)
                  OutlinedButton(
                    onPressed: editable ? _useInspectedPolicy : null,
                    child: const Text('Use current policy'),
                  ),
                OutlinedButton(
                  onPressed: !_reading && !busy && widget.active()
                      ? _load
                      : null,
                  child: const Text('Refresh'),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}
