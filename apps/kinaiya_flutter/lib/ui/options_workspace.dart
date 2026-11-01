import 'dart:async';

import 'package:flutter/material.dart';

import '../api/agent_model_settings.dart';
import '../api/agent_model_client.dart';
import '../api/agent_runtime_settings.dart';
import '../api/agent_workspace.dart';
import '../api/client.dart';
import '../api/converse_setup.dart';
import '../config/options_catalog.dart';
import '../config/options_navigation.dart';
import '../presentation/controller.dart';
import '../state/app_state.dart';
import '../state/agent_model_writes.dart';
import 'converse_memory.dart';
import 'design_tokens.dart';
import 'mcp_settings.dart';
import 'options_model_editor.dart';
import 'options_runtime_editor.dart';
import 'agent_runtime_writes.dart';
import 'options_provider_editor.dart';
import 'options_tool_permissions.dart';
import 'presentation_settings.dart';
import 'settings_controls.dart';
import 'setup_dialog.dart';
import 'widgets.dart';
import 'motion_transition.dart';

class OptionsWorkspace extends StatefulWidget {
  const OptionsWorkspace({
    super.key,
    required this.state,
    required this.presentation,
    required this.converseControls,
    required this.onClose,
    this.initialPage = OptionsPage.overview,
  });
  final AppState state;
  final PresentationController presentation;
  final ConverseViewControls converseControls;
  final VoidCallback onClose;
  final OptionsPage initialPage;
  @override
  State<OptionsWorkspace> createState() => _OptionsWorkspaceState();
}

class _OptionsWorkspaceState extends State<OptionsWorkspace> {
  late OptionsPage _page;
  String? _harness;
  String? _auxiliaryPurpose;
  String _query = '';
  final _search = TextEditingController();
  final _scaffold = GlobalKey<ScaffoldState>();
  ConverseSetup? _converse;
  Map<String, Object?>? _agent;
  AgentModelsSnapshot? _models;
  AgentRuntimeSnapshot? _runtime;
  AgentRuntimeWriteScope? _runtimeScope;
  OptionsRuntimeDraft? _legacyRuntimeDraft;
  OptionsRuntimeDraft? get _runtimeDraft =>
      _runtimeScope?.draft ?? _legacyRuntimeDraft;
  String? _readError, _modelError, _runtimeError, _notice;
  ({AgentModelApi owner, String backendId, String text})? _modelNotice;
  bool _loading = false, _savingPreference = false;
  int _read = 0, _connection = -1;

  PendingAgentModelChange? get _pending {
    final api = widget.state.client?.modelSettings;
    return api == null
        ? null
        : agentModelWrites.visible(api, _models?.writeAuthority);
  }

  String? get _visibleModelNotice {
    final notice = _modelNotice, api = widget.state.client?.modelSettings;
    return notice != null &&
            api != null &&
            api.sameOwner(notice.owner) &&
            _models?.writeAuthority?.backendId == notice.backendId
        ? notice.text
        : null;
  }

  bool get _modelBusy =>
      _loading ||
      _pending != null ||
      _models?.writeAuthority?.writable != true ||
      widget.state.client?.modelSettings.hasCapacity != true;
  String? get _modelReadOnlyReason => _models == null
      ? null
      : _models!.writeAuthority == null
      ? 'Model settings are read-only. This host does not advertise supported bound writes.'
      : !_models!.writeAuthority!.writable
      ? 'Model settings are read-only. This host instance no longer owns model writes.'
      : null;
  PendingAgentRuntimeChange? get _runtimePending => _runtimeScope?.pending;
  bool get _runtimeBusy =>
      _loading ||
      _runtimePending != null ||
      _runtime?.writeAuthority?.writable != true ||
      _runtimeScope == null ||
      widget.state.client?.runtimeSettings.hasCapacity != true;
  String? get _runtimeReadOnlyReason => _runtime == null
      ? null
      : _runtime!.writeAuthority == null
      ? 'Execution settings are read-only. This host does not advertise supported bound writes.'
      : !_runtime!.writeAuthority!.writable
      ? 'Execution settings are read-only. This host instance no longer owns execution writes.'
      : _runtimeScope == null
      ? 'Execution settings are read-only. Resolve or review retained drafts before opening another settings scope.'
      : null;

  @override
  void initState() {
    super.initState();
    _page = widget.initialPage;
    _connection = widget.state.connectionGeneration;
    widget.state.addListener(_changed);
    agentModelWrites.addListener(_modelWritesChanged);
    agentRuntimeWrites.addListener(_modelWritesChanged);
    widget.presentation.addListener(_presentationChanged);
    // Catalog refresh notifies the shared shell, so start after this mount.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _refresh();
    });
  }

  @override
  void didUpdateWidget(covariant OptionsWorkspace oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.state, widget.state)) {
      oldWidget.state.removeListener(_changed);
      widget.state.addListener(_changed);
      _harness = null;
      _runtimeScope = null;
      _legacyRuntimeDraft = null;
      _connection = widget.state.connectionGeneration;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _refresh();
      });
    }
    if (!identical(oldWidget.presentation, widget.presentation)) {
      oldWidget.presentation.removeListener(_presentationChanged);
      widget.presentation.addListener(_presentationChanged);
    }
    if (oldWidget.initialPage != widget.initialPage) {
      _select(widget.initialPage);
    }
  }

  @override
  void dispose() {
    _read++;
    _search.dispose();
    widget.state.removeListener(_changed);
    agentModelWrites.removeListener(_modelWritesChanged);
    agentRuntimeWrites.removeListener(_modelWritesChanged);
    widget.presentation.removeListener(_presentationChanged);
    super.dispose();
  }

  void _changed() {
    if (!mounted) return;
    if (_connection != widget.state.connectionGeneration) {
      _harness = null;
      _refresh();
    } else {
      setState(() {});
    }
  }

  void _modelWritesChanged() {
    if (mounted) setState(() {});
  }

  void _presentationChanged() {
    if (mounted) setState(() {});
  }

  Future<void> _refresh() async {
    final owner = widget.state, api = widget.state.client;
    final generation = owner.connectionGeneration, request = ++_read;
    _connection = generation;
    setState(() {
      _converse = null;
      _agent = null;
      _models = null;
      _runtime = null;
      _runtimeScope = null;
      _legacyRuntimeDraft = null;
      _readError = _modelError = _runtimeError = null;
      _loading = api != null;
    });
    if (api == null) return;
    bool current() =>
        mounted &&
        identical(owner, widget.state) &&
        request == _read &&
        owner.ownsConnection(generation, api);
    // Independently failing services retain their own unknown state.
    final reads = <Future<void>>[
      () async {
        try {
          final result = await api.converseSetup().timeout(
            const Duration(seconds: 8),
          );
          if (current()) setState(() => _converse = result);
        } catch (_) {
          if (current()) {
            setState(
              () =>
                  _readError = 'Converse model declarations could not be read.',
            );
          }
        }
      }(),
      () async {
        try {
          final result = await api.agentOptions();
          if (result['schema_version'] != '1.0' ||
              (result['provider'] != null && result['provider'] is! Map) ||
              result['permissions'] is! Map ||
              result['tools'] is! List) {
            throw const FormatException();
          }
          AgentModelsSnapshot? models;
          AgentRuntimeSnapshot? runtime;
          String? modelError, runtimeError;
          try {
            if (result['models'] != null) {
              models = AgentModelsSnapshot.fromJson(result['models']);
            }
          } catch (_) {
            modelError = 'Agent model settings could not be read.';
          }
          try {
            if (result['runtime_settings'] != null) {
              runtime = AgentRuntimeSnapshot.fromJson(
                result['runtime_settings'],
              );
            }
          } catch (_) {
            runtimeError = 'Agent execution settings could not be read.';
          }
          if (current()) {
            setState(() {
              _agent = result;
              _models = models;
              _modelError = modelError;
              _runtime = runtime;
              _runtimeError = runtimeError;
              if (runtime != null) {
                _runtimeScope = agentRuntimeWrites.open(
                  api.runtimeSettings,
                  runtime,
                );
                if (_runtimeScope == null) {
                  _legacyRuntimeDraft = AgentRuntimeDraft(runtime);
                }
              }
            });
          }
        } catch (_) {
          if (current()) {
            setState(
              () => _modelError =
                  'Agent options could not be read. Check the host connection and version.',
            );
          }
        }
      }(),
      owner.refreshAgentHarnesses(),
    ];
    await Future.wait(reads);
    if (current()) setState(() => _loading = false);
  }

  void _select(OptionsPage page) {
    _scaffold.currentState?.closeDrawer();
    setState(() {
      _page = page;
      _harness = null;
      _query = '';
      _search.clear();
    });
  }

  OptionsCategory? get _category {
    for (final item in optionsCategories) {
      if (item.pages.contains(_page)) return item;
    }
    return null;
  }

  String get _pageTitle {
    if (_harness != null) {
      for (final row in widget.state.agentHarnesses) {
        if (row.versionId == _harness) return '${row.name} · Model settings';
      }
      return 'Unavailable harness · Model settings';
    }
    return _page.label;
  }

  Future<void> _preference(PresentationPreferences value) async {
    if (_savingPreference) return;
    final controller = widget.presentation;
    setState(() {
      _savingPreference = true;
      _notice = null;
    });
    try {
      await controller.setPreferences(value);
    } catch (_) {
      if (mounted) {
        setState(
          () => _notice = 'This preference could not be confirmed as saved.',
        );
      }
    } finally {
      if (mounted) setState(() => _savingPreference = false);
    }
  }

  Future<void> _saveModel(
    String? profile,
    String? reasoning, {
    String? auxiliaryTask,
    AgentCatalogBinding? catalogBinding,
  }) async {
    final api = widget.state.client?.modelSettings, models = _models;
    final authority = models?.writeAuthority;
    if (api == null || models == null || authority == null || _modelBusy) {
      return;
    }
    if (profile != null && catalogBinding != null) return;
    final request = AgentModelChange.fromJson({
      'schema_version': '1.1',
      'expected_backend': authority.backendId,
      'expected_instance': authority.instanceId,
      'expected_revision': models.revision,
      'idempotency_key': newIdempotencyKey(),
      'harness_version': _page == OptionsPage.harnesses ? _harness : null,
      'profile_id': profile,
      if (catalogBinding != null) 'catalog_binding': catalogBinding.toJson(),
      'reasoning': reasoning,
      if (auxiliaryTask != null) 'auxiliary_task': auxiliaryTask,
    });
    final operation = agentModelWrites.begin(api, authority, request);
    if (operation == null) {
      setState(
        () => _modelNotice = (
          owner: api,
          backendId: authority.backendId,
          text:
              'Model recovery capacity is full. Resolve an earlier save before starting another.',
        ),
      );
      return;
    }
    await _retryModel(operation);
  }

  Future<void> _modelResult(
    PendingAgentModelChange operation,
    Future<AgentModelOutcome?> Function() action,
  ) async {
    final owner = widget.state, api = owner.client;
    final generation = owner.connectionGeneration;
    if (api == null) return;
    final result = await action();
    if (!mounted ||
        !identical(owner, widget.state) ||
        !owner.ownsConnection(generation, api) ||
        _models?.writeAuthority?.backendId != operation.request.backendId ||
        result == null) {
      return;
    }
    setState(
      () => _modelNotice = (
        owner: api.modelSettings,
        backendId: operation.request.backendId,
        text: result == AgentModelOutcome.recorded
            ? 'The model save is confirmed. Current defaults are refreshed below.'
            : 'The host confirmed that the earlier save did not commit and can no longer apply. Review current values before saving again.',
      ),
    );
    await _refresh();
    if (mounted &&
        identical(owner, widget.state) &&
        owner.ownsConnection(generation, api)) {
      await owner.revalidateAgentInference();
    }
  }

  Future<void> _retryModel(PendingAgentModelChange operation) async {
    final api = widget.state.client?.modelSettings,
        authority = _models?.writeAuthority;
    if (api == null || !agentModelWrites.canRetry(operation, api, authority)) {
      return;
    }
    widget.state.invalidateAgentInference();
    await _modelResult(
      operation,
      () => agentModelWrites.retry(operation, api, authority),
    );
  }

  Future<void> _checkModelSave() async {
    final operation = _pending, api = widget.state.client?.modelSettings;
    if (operation == null || api == null) return;
    await _modelResult(
      operation,
      () => agentModelWrites.lookup(operation, api, _models?.writeAuthority),
    );
  }

  Future<void> _saveRuntime(OptionsRuntimeDraft draft) async {
    final api = widget.state.client?.runtimeSettings;
    final runtime = _runtime, scope = _runtimeScope, settings = draft.settings;
    final authority = runtime?.writeAuthority;
    if (api == null ||
        runtime == null ||
        scope == null ||
        authority == null ||
        settings == null ||
        _runtimeBusy ||
        runtime.busy ||
        !identical(scope.draft, draft) ||
        runtime.stateToken != draft.base.stateToken) {
      return;
    }
    final request = AgentRuntimeChange.fromJson({
      'schema_version': '1.1',
      'expected_backend': authority.backendId,
      'expected_instance': authority.instanceId,
      'expected_revision': runtime.revision,
      'expected_state': runtime.stateToken,
      'idempotency_key': newIdempotencyKey(),
      'settings': settings,
      'yolo': draft.yolo,
    });
    final operation = agentRuntimeWrites.begin(scope, api, request);
    if (operation == null) return;
    await _retryRuntime(operation);
  }

  Future<void> _runtimeAction(
    Future<AgentRuntimeOutcome?> Function() action,
  ) async {
    final owner = widget.state, api = owner.client;
    final generation = owner.connectionGeneration, scope = _runtimeScope;
    final outcome = await action();
    if (outcome != null &&
        mounted &&
        identical(owner, widget.state) &&
        identical(scope, _runtimeScope) &&
        api != null &&
        owner.ownsConnection(generation, api)) {
      await _refresh();
    }
  }

  Future<void> _retryRuntime(PendingAgentRuntimeChange operation) async {
    final api = widget.state.client?.runtimeSettings;
    if (api == null) return;
    await _runtimeAction(
      () => agentRuntimeWrites.retry(operation, api, _runtime?.writeAuthority),
    );
  }

  Future<void> _checkRuntimeSave() async {
    final api = widget.state.client?.runtimeSettings,
        operation = _runtimePending;
    if (api == null || operation == null) return;
    await _runtimeAction(
      () => agentRuntimeWrites.lookup(operation, api, _runtime?.writeAuthority),
    );
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, bounds) {
      final wide =
          bounds.maxWidth /
              MediaQuery.textScalerOf(context).scale(1).clamp(1, 2) >=
          880;
      return Scaffold(
        key: _scaffold,
        backgroundColor: Theme.of(context).scaffoldBackgroundColor,
        drawer: wide ? null : Drawer(child: SafeArea(child: _navigation())),
        body: Row(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (wide) SizedBox(width: 264, child: _navigation()),
            if (wide) const VerticalDivider(width: 1),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(24, 20, 16, 12),
                    child: Row(
                      children: [
                        if (!wide)
                          IconButton(
                            tooltip: 'Options navigation',
                            icon: const Icon(Icons.menu),
                            onPressed: () =>
                                _scaffold.currentState?.openDrawer(),
                          ),
                        Expanded(
                          child: Text(
                            _pageTitle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: Theme.of(context).textTheme.headlineSmall
                                ?.copyWith(fontWeight: FontWeight.w700),
                          ),
                        ),
                        IconButton(
                          tooltip: 'Refresh options',
                          onPressed: _loading ? null : _refresh,
                          icon: const Icon(Icons.refresh),
                        ),
                        IconButton(
                          key: const Key('settings-close'),
                          tooltip: 'Close options',
                          onPressed: widget.onClose,
                          icon: const Icon(Icons.close),
                        ),
                      ],
                    ),
                  ),
                  if (_loading) const LinearProgressIndicator(minHeight: 2),
                  if (_notice != null)
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 24,
                        vertical: 8,
                      ),
                      child: Semantics(liveRegion: true, child: Text(_notice!)),
                    ),
                  if (_visibleModelNotice case final notice?)
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 24,
                        vertical: 8,
                      ),
                      child: Semantics(liveRegion: true, child: Text(notice)),
                    ),
                  if (_pending case final pending?)
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 24,
                        vertical: 8,
                      ),
                      child: DesignPanel(
                        padding: const EdgeInsets.all(12),
                        child: Wrap(
                          crossAxisAlignment: WrapCrossAlignment.center,
                          spacing: 12,
                          runSpacing: 8,
                          children: [
                            Text(pending.notice),
                            TextButton(
                              onPressed:
                                  agentModelWrites.canLookup(
                                    pending,
                                    widget.state.client!.modelSettings,
                                    _models?.writeAuthority,
                                  )
                                  ? _checkModelSave
                                  : null,
                              child: const Text('Check save status'),
                            ),
                            if (agentModelWrites.canRetry(
                              pending,
                              widget.state.client!.modelSettings,
                              _models?.writeAuthority,
                            ))
                              TextButton(
                                onPressed: () => _retryModel(pending),
                                child: const Text('Retry the same save'),
                              ),
                          ],
                        ),
                      ),
                    ),
                  if (_runtimeScope?.notice case final notice?)
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 24,
                        vertical: 8,
                      ),
                      child: Semantics(liveRegion: true, child: Text(notice)),
                    ),
                  if (_runtimePending case final pending?)
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 24,
                        vertical: 8,
                      ),
                      child: DesignPanel(
                        padding: const EdgeInsets.all(12),
                        child: Wrap(
                          crossAxisAlignment: WrapCrossAlignment.center,
                          spacing: 12,
                          runSpacing: 8,
                          children: [
                            Text(pending.notice),
                            TextButton(
                              onPressed:
                                  agentRuntimeWrites.canLookup(
                                    pending,
                                    widget.state.client!.runtimeSettings,
                                    _runtime?.writeAuthority,
                                  )
                                  ? _checkRuntimeSave
                                  : null,
                              child: const Text('Check execution save status'),
                            ),
                            if (agentRuntimeWrites.canRetry(
                              pending,
                              widget.state.client!.runtimeSettings,
                              _runtime?.writeAuthority,
                            ))
                              TextButton(
                                onPressed: () => _retryRuntime(pending),
                                child: const Text(
                                  'Retry the same execution save',
                                ),
                              ),
                          ],
                        ),
                      ),
                    ),
                  Expanded(
                    child: Padding(
                      padding: EdgeInsets.fromLTRB(
                        wide ? 32 : 16,
                        8,
                        wide ? 32 : 16,
                        24,
                      ),
                      child: UiFadeTransition(
                        alignment: Alignment.topLeft,
                        enabled:
                            widget
                                .presentation
                                .preferences
                                .animateTransitions &&
                            !widget.presentation.preferences.reducedMotion,
                        transitionKey: '${_page.name}:${_harness ?? ''}',
                        child: _content(),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      );
    },
  );

  Widget _navigation() {
    final category = _category, searching = _query.trim().isNotEmpty;
    return Material(
      color: Theme.of(context).colorScheme.surfaceContainerLow,
      child: ListView(
        padding: EdgeInsets.zero,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 24, 20, 18),
            child: Text(
              'Options',
              style: Theme.of(context).textTheme.titleLarge,
            ),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: TextField(
              key: const Key('settings-search'),
              controller: _search,
              maxLength: 128,
              decoration: InputDecoration(
                hintText: 'Search options',
                counterText: '',
                prefixIcon: const Icon(Icons.search),
                border: const OutlineInputBorder(),
                suffixIcon: _query.isEmpty
                    ? null
                    : IconButton(
                        tooltip: 'Clear search',
                        onPressed: () => setState(() {
                          _query = '';
                          _search.clear();
                        }),
                        icon: const Icon(Icons.clear),
                      ),
              ),
              onChanged: (value) => setState(() => _query = value),
            ),
          ),
          const SizedBox(height: 12),
          Padding(
            padding: const EdgeInsets.all(8),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (searching) ...[
                  for (final item in optionsCategories)
                    for (final page in item.pages)
                      if ('${item.label} ${page.label}'.toLowerCase().contains(
                        _query.toLowerCase().trim(),
                      ))
                        ListTile(
                          key: ValueKey('options-search-${page.name}'),
                          title: Text(page.label),
                          subtitle: Text(item.label),
                          leading: Icon(item.icon),
                          onTap: () => _select(page),
                        ),
                  if (!optionsCategories.any(
                    (item) => item.pages.any(
                      (page) => '${item.label} ${page.label}'
                          .toLowerCase()
                          .contains(_query.toLowerCase().trim()),
                    ),
                  ))
                    const ListTile(title: Text('No matching options')),
                ] else if (category != null) ...[
                  for (final page in category.pages)
                    ListTile(
                      key: ValueKey('options-${page.name}'),
                      selected: page == _page,
                      selectedColor: Theme.of(
                        context,
                      ).colorScheme.onPrimaryContainer,
                      selectedTileColor: Theme.of(
                        context,
                      ).colorScheme.primaryContainer,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(8),
                      ),
                      title: Text(page.label),
                      onTap: () => _select(page),
                    ),
                  const Divider(),
                  ListTile(
                    key: const Key('options-back'),
                    leading: const Icon(Icons.arrow_back),
                    title: const Text('Back'),
                    onTap: () => _select(OptionsPage.overview),
                  ),
                ] else ...[
                  for (final item in optionsCategories)
                    ListTile(
                      key: ValueKey(
                        'options-category-${item.pages.first.name}',
                      ),
                      leading: Icon(item.icon, size: 20),
                      title: Text(item.label),
                      onTap: () => _select(item.pages.first),
                    ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _content() => switch (_page) {
    OptionsPage.overview => SingleChildScrollView(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Choose a category to get started.'),
          const SizedBox(height: 32),
          if (widget.presentation.preferences.showCharacter)
            if (widget.presentation.selected?.packageJson == null)
              Image.asset(
                'assets/design/kinaiya-options-gear-v1.png',
                semanticLabel:
                    'Kinaiya cradles a gear in front of the full moon',
                fit: BoxFit.contain,
              )
            else if (widget.presentation.selected case final selection?)
              Center(
                child: MascotWidget(
                  pack: selection.pack,
                  bundle: selection.bundle,
                  mood: 'idle',
                  size: 320,
                ),
              ),
        ],
      ),
    ),
    OptionsPage.models => _modelDefaults(),
    OptionsPage.harnesses => _harnesses(),
    OptionsPage.providers => _providers(),
    OptionsPage.voice => _voice(),
    OptionsPage.audio => _audio(),
    OptionsPage.appearance => PresentationSettings(
      key: const ValueKey('options-appearance'),
      controller: widget.presentation,
      embedded: true,
      section: PresentationSection.appearance,
    ),
    OptionsPage.character => PresentationSettings(
      key: const ValueKey('options-character'),
      controller: widget.presentation,
      embedded: true,
      section: PresentationSection.character,
    ),
    OptionsPage.accessibility => PresentationSettings(
      key: const ValueKey('options-accessibility'),
      controller: widget.presentation,
      embedded: true,
      section: PresentationSection.accessibility,
    ),
    OptionsPage.connection => SetupDialog(
      key: const ValueKey('options-connection'),
      state: widget.state,
      embedded: true,
    ),
    OptionsPage.privacy => SetupDialog(
      key: const ValueKey('options-privacy'),
      state: widget.state,
      embedded: true,
      privacyOnly: true,
    ),
    OptionsPage.memory => ConverseMemoryDialog(
      state: widget.state,
      embedded: true,
    ),
    OptionsPage.integrations => McpSettings(
      state: widget.state,
      embedded: true,
    ),
    OptionsPage.agent => _agentSettings(),
    OptionsPage.permissions => _permissions(),
    OptionsPage.workspace || OptionsPage.execution => _runtimeSettings(),
    OptionsPage.tools => _tools(),
    OptionsPage.data => _data(),
    OptionsPage.tasks => _tasks(),
    OptionsPage.resources => _resources(),
    OptionsPage.about => _scroll([
      _heading('Kinaiya'),
      const Text(
        'Local conversation. Independent work. Inspectable decisions.',
      ),
      const SizedBox(height: 24),
      Text(const SettingsBuildInfo.fromEnvironment().label),
      if (const SettingsBuildInfo.fromEnvironment().revision.isNotEmpty)
        SelectableText(const SettingsBuildInfo.fromEnvironment().revision),
    ]),
  };

  Widget _scroll(List<Widget> children) => SingleChildScrollView(
    child: Align(
      alignment: Alignment.topLeft,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 1100),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            for (final item in children)
              Padding(padding: const EdgeInsets.only(bottom: 16), child: item),
          ],
        ),
      ),
    ),
  );
  Widget _heading(String text) =>
      Text(text, style: Theme.of(context).textTheme.titleLarge);
  Widget _field(String label, String value) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 8),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: Theme.of(context).textTheme.labelLarge?.copyWith(
            color: Theme.of(context).colorScheme.onSurfaceVariant,
          ),
        ),
        const SizedBox(height: 6),
        SelectableText(value, semanticsLabel: value),
      ],
    ),
  );
  Widget _unavailable(String title, String detail) => DesignPanel(
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [_heading(title), const SizedBox(height: 8), Text(detail)],
    ),
  );
  List<Widget> get _errors => [
    if (widget.state.client == null)
      const Text('Connect to your host to inspect models and services.'),
    if (_readError != null) Text(_readError!),
    if (_modelError != null) Text(_modelError!),
    if (_runtimeError != null) Text(_runtimeError!),
  ];

  Widget _role(String id, String label) {
    final role = _converse?.roles[id];
    return DesignPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _heading(label),
          const SizedBox(height: 12),
          _field(
            'Configured model',
            role?.label ?? (role == null ? 'Unavailable' : 'Not configured'),
          ),
          if (id == 'conversation')
            _field('Reasoning', _converse?.reasoning ?? 'Unavailable'),
          if (role != null) Text(role.reason),
          const SizedBox(height: 8),
          const Text(
            'Host configuration · Changing this role requires a host update.',
          ),
        ],
      ),
    );
  }

  Widget _modelDefaults() => _scroll([
    const Text(
      'Model defaults by role. Harness-specific choices live in Harness Customization.',
    ),
    ..._errors,
    _role('conversation', 'Converse'),
    if (_modelReadOnlyReason case final reason?) Text(reason),
    if (_models case final models?)
      OptionsModelEditor(
        key: const ValueKey('agent-default-model-editor'),
        models: models,
        busy: _modelBusy,
        onSave: _saveModel,
        onSaveCatalog: (catalog, reasoning) =>
            _saveModel(null, reasoning, catalogBinding: catalog),
      )
    else
      _unavailable(
        'Agent default',
        'Connect to an updated Agent host to manage model profiles.',
      ),
    _role('decisions', 'Decisions'),
    const Text(
      'Decisions classifies typed questions. It has no conversational reasoning or voice setting.',
    ),
    if (_models case final models?)
      OptionsAuxiliaryModels(
        models: models,
        initialPurpose: _auxiliaryPurpose,
        onPurposeChanged: (value) => _auxiliaryPurpose = value,
        busy: _modelBusy,
        onSave: (purpose, profile, reasoning) =>
            _saveModel(profile, reasoning, auxiliaryTask: purpose),
        onSaveCatalog: (purpose, catalog, reasoning) => _saveModel(
          null,
          reasoning,
          auxiliaryTask: purpose,
          catalogBinding: catalog,
        ),
      ),
  ]);

  Widget _harnesses() {
    final rows = widget.state.agentHarnesses;
    if (_harness != null) {
      AgentHarness? selected;
      for (final row in rows) {
        if (row.versionId == _harness) selected = row;
      }
      final harness = selected;
      return _scroll([
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            onPressed: () => setState(() => _harness = null),
            icon: const Icon(Icons.arrow_back),
            label: const Text('All harnesses'),
          ),
        ),
        _heading(harness?.name ?? 'Harness unavailable'),
        _field('Harness version', _harness!),
        if (harness != null) Text(harness.description),
        ..._errors,
        if (_modelReadOnlyReason case final reason?) Text(reason),
        if (_models != null && harness != null && harness.selectable)
          OptionsModelEditor(
            key: ValueKey('model-editor-$_harness'),
            models: _models!,
            harness: _harness,
            busy: _modelBusy,
            onSave: _saveModel,
            onSaveCatalog: (catalog, reasoning) =>
                _saveModel(null, reasoning, catalogBinding: catalog),
          )
        else
          _unavailable(
            'Model settings unavailable',
            harness?.selectable == false
                ? 'This harness is not approved for new work. Model settings cannot promote it.'
                : 'Refresh the native harness catalog and model profiles.',
          ),
        if (_models != null && harness != null && harness.selectable)
          OptionsAuxiliaryModels(
            key: ValueKey('harness-auxiliary-$_harness'),
            models: _models!,
            initialPurpose: _auxiliaryPurpose,
            onPurposeChanged: (value) => _auxiliaryPurpose = value,
            harness: _harness,
            busy: _modelBusy,
            onSave: (purpose, profile, reasoning) =>
                _saveModel(profile, reasoning, auxiliaryTask: purpose),
            onSaveCatalog: (purpose, catalog, reasoning) => _saveModel(
              null,
              reasoning,
              auxiliaryTask: purpose,
              catalogBinding: catalog,
            ),
          ),
      ]);
    }
    return _scroll([
      const Text(
        'Choose the model used for new tasks in each registered harness.',
      ),
      ..._errors,
      if (widget.state.agentHarnessError != null)
        Text(widget.state.agentHarnessError!),
      if (widget.state.agentHarnessesLoading) const LinearProgressIndicator(),
      if (rows.isEmpty && !widget.state.agentHarnessesLoading)
        const Text(
          'No harness catalog is available. Example harnesses are not added to your host.',
        ),
      for (final row in rows)
        DesignPanel(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: _heading(row.name)),
                  OutlinedButton(
                    key: ValueKey('harness-edit-${row.versionId}'),
                    onPressed: () => setState(() => _harness = row.versionId),
                    child: const Text('Edit'),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              Text(row.description),
              _field(
                'Model setting',
                _models == null
                    ? 'Unavailable'
                    : _models!.harnesses.containsKey(row.versionId)
                    ? 'Custom · ${_models!.bindingLabel(_models!.effective(row.versionId))}'
                    : 'Inherits Agent default',
              ),
              Text('${row.kind} · ${row.eligibility}'),
            ],
          ),
        ),
    ]);
  }

  Widget _providers() {
    final client = widget.state.client;
    final generation = widget.state.connectionGeneration;
    return _scroll([
      OptionsProviderEditor(
        key: ObjectKey(client),
        api: client?.providers,
        active: () =>
            mounted &&
            identical(widget.state.client, client) &&
            widget.state.connectionGeneration == generation,
      ),
    ]);
  }

  Widget _voice() => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      Expanded(
        child:
            widget.converseControls.buildVoiceOptions?.call() ??
            const Text(
              'Voice options are unavailable until the Converse view is mounted.',
            ),
      ),
    ],
  );
  Widget _audio() => _scroll([
    const Text('Speech providers remain independent of the dialogue model.'),
    ..._errors,
    _role('stt', 'Speech to text'),
    _role('tts', 'Text to speech'),
    const Text(
      'This host currently accepts text dialogue through transcription. Direct audio input and disabling transcription require a compatible native pipeline.',
    ),
  ]);
  Widget _agentSettings() => _scroll([
    const Text(
      'Trusted Agent runtime. Generated context selectors do not replace permissions or the protected core.',
    ),
    ..._errors,
    _field(
      'Default model',
      _agent?['provider'] is Map
          ? '${(_agent!['provider'] as Map)['model']}'
          : 'Unavailable',
    ),
    _field(
      'Task step limit',
      _agent?['max_steps']?.toString() ?? 'Unavailable',
    ),
    const Text(
      'Model defaults and harness overrides are in Models & providers. '
      'Execution contains task step and native command limits.',
    ),
  ]);
  Widget _permissions() {
    final owner = widget.state, client = widget.state.client;
    final generation = owner.connectionGeneration;
    final permissions = _agent?['permissions'];
    final snapshot = _runtime, draft = _runtimeDraft;
    return _scroll([
      OptionsToolPermissions(
        key: ObjectKey(client),
        api: client?.toolPermissions,
        active: () =>
            mounted &&
            identical(widget.state, owner) &&
            client != null &&
            owner.ownsConnection(generation, client),
        currentSessionId: owner.agentSession?.sessionId,
        currentTaskIds: owner.tasks.map((task) => task.taskId).toList(),
      ),
      ..._errors,
      if (_runtimeReadOnlyReason case final reason?) Text(reason),
      if (snapshot != null && draft != null)
        OptionsRuntimeEditor(
          key: ObjectKey(draft),
          snapshot: snapshot,
          draft: draft,
          page: _page,
          saving: _runtimeBusy,
          onSave: _saveRuntime,
          onReset: () {
            final scope = _runtimeScope;
            if (scope != null &&
                identical(scope.draft, draft) &&
                identical(scope.snapshot, snapshot)) {
              agentRuntimeWrites.reset(scope);
            }
          },
        )
      else ...[
        const Text(
          'Service-wide host grants. A model choice does not grant access or approve actions.',
        ),
        for (final row in const [
          ('allow_write', 'File writes'),
          ('allow_shell', 'Shell commands'),
          ('network', 'Network access'),
          ('yolo', 'YOLO approval mode'),
        ])
          _field(
            row.$2,
            permissions is Map && permissions[row.$1] is bool
                ? permissions[row.$1] == true
                      ? 'Allowed by host policy'
                      : 'Not granted by this host flag'
                : 'Unavailable',
          ),
        const Text(
          'Service-wide execution controls are unavailable from this host. Existing approval and isolation checks remain in force.',
        ),
      ],
    ]);
  }

  Widget _runtimeSettings() {
    final snapshot = _runtime, draft = _runtimeDraft;
    return _scroll([
      ..._errors,
      if (_runtimeReadOnlyReason case final reason?) Text(reason),
      if (snapshot != null && draft != null)
        OptionsRuntimeEditor(
          key: ObjectKey(draft),
          snapshot: snapshot,
          draft: draft,
          page: _page,
          saving: _runtimeBusy,
          onSave: _saveRuntime,
          onReset: () {
            final scope = _runtimeScope;
            if (scope != null) agentRuntimeWrites.reset(scope);
          },
        )
      else
        _unavailable(
          'Execution settings unavailable',
          'Connect to an updated Agent host with owner execution settings enabled.',
        ),
    ]);
  }

  Widget _tools() => _scroll([
    const Text(
      'Native tools currently advertised by the Agent host. Availability is separate from permission to execute.',
    ),
    ..._errors,
    for (final name in (_agent?['tools'] as List? ?? const []))
      _field('Tool', '$name'),
    if (_agent == null) const Text('Tool registry unavailable.'),
  ]);
  Widget _data() => _scroll([
    const Text(
      'The authoritative transcript is separate from optional Converse memory.',
    ),
    _field(
      'Session service',
      widget.state.connection?.sessionUrl ?? 'Not configured',
    ),
    _field(
      'Converse session',
      widget.state.converseSession?.sessionId ?? 'No selected session',
    ),
    _field(
      'Agent session',
      widget.state.agentSession?.sessionId ?? 'No selected session',
    ),
    const Text(
      'Transcript retention, bulk export and deletion are host-managed. Forgetting a memory note does not delete the transcript.',
    ),
  ]);
  Widget _tasks() => _scroll([
    const Text('Task execution bounds'),
    ..._errors,
    _field(
      'Maximum steps per task',
      _agent?['max_steps']?.toString() ?? 'Unavailable',
    ),
    const Text(
      'Changing a model affects new tasks. It does not restart, cancel or rewrite existing work.',
    ),
    for (final task in widget.state.tasks)
      DesignPanel(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _field('Task', task.taskId),
            _field('State', task.state),
            _field(
              'Harness',
              task.harnessVersion ?? 'Historical identity unavailable',
            ),
          ],
        ),
      ),
  ]);
  Widget _resources() => _scroll([
    const Text('Resource policy and model residency are owned by the host.'),
    _field(
      'Converse',
      widget.state.converseOnline ? 'Service reachable' : 'Service unavailable',
    ),
    _field(
      'Agent',
      widget.state.agentOnline ? 'Service reachable' : 'Service unavailable',
    ),
    const Text(
      'A reachable service is not proof that a model is loaded or that GPU memory is available. Resource handoff controls remain in Agent.',
    ),
    SwitchListTile(
      contentPadding: EdgeInsets.zero,
      title: const Text('Intelligent UI'),
      subtitle: const Text(
        'Display structured tables, charts and interactive artifacts in the conversation.',
      ),
      value: widget.presentation.preferences.intelligentUi,
      onChanged: _savingPreference
          ? null
          : (value) => _preference(
              widget.presentation.preferences.copyWith(intelligentUi: value),
            ),
    ),
  ]);
}
