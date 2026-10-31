import 'dart:async';

import 'package:flutter/material.dart';

import '../api/agent_model_settings.dart';
import '../api/agent_workspace.dart';
import '../api/client.dart';
import '../api/converse_setup.dart';
import '../config/options_catalog.dart';
import '../config/options_navigation.dart';
import '../presentation/controller.dart';
import '../state/app_state.dart';
import 'converse_memory.dart';
import 'design_tokens.dart';
import 'mcp_settings.dart';
import 'options_model_editor.dart';
import 'presentation_settings.dart';
import 'settings_controls.dart';
import 'setup_dialog.dart';
import 'widgets.dart';
import 'motion_transition.dart';

// An unresolved write survives closing Options and same-host reconnects. Only
// its exact native receipt or a definite refusal settles it. Retrying reuses
// the identical key and body; navigation never retries a write.
class _ModelChange {
  _ModelChange(this.body);
  final Map<String, Object?> body;
  int inFlight = 0;
  bool timedOut = false;
  bool get sending => inFlight > 0;
  bool get canRetry => inFlight < 2 && (timedOut || inFlight == 0);
  String? notice;
}

final _modelChanges = <String, _ModelChange>{};

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
  String _query = '';
  final _search = TextEditingController();
  final _scaffold = GlobalKey<ScaffoldState>();
  ConverseSetup? _converse;
  Map<String, Object?>? _agent;
  AgentModelsSnapshot? _models;
  String? _readError, _modelError, _notice;
  bool _loading = false, _savingPreference = false;
  int _read = 0, _connection = -1;

  String? get _endpoint => widget.state.client?.agentBase;
  _ModelChange? get _pending => _modelChanges[_endpoint];

  @override
  void initState() {
    super.initState();
    _page = widget.initialPage;
    _connection = widget.state.connectionGeneration;
    widget.state.addListener(_changed);
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
      _readError = _modelError = null;
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
          final models = result['models'] == null
              ? null
              : AgentModelsSnapshot.fromJson(result['models']);
          if (current()) {
            setState(() {
              _agent = result;
              _models = models;
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

  bool _validReceipt(_ModelChange operation, Map<String, Object?> result) {
    final body = operation.body, binding = result['binding'];
    return result['schema_version'] == '1.0' &&
        result['change_boundary'] == 'new_tasks' &&
        result['idempotency_key'] == body['idempotency_key'] &&
        result['revision'] == (body['expected_revision'] as int) + 1 &&
        result['harness_version'] == body['harness_version'] &&
        (body['profile_id'] == null
            ? binding == null
            : binding is Map &&
                  binding['profile_id'] == body['profile_id'] &&
                  binding['reasoning'] == body['reasoning']);
  }

  Future<void> _saveModel(String? profile, String? reasoning) async {
    final endpoint = _endpoint, models = _models;
    if (endpoint == null || models == null || _pending != null) {
      return;
    }
    if (_modelChanges.length >= 16) {
      setState(
        () => _notice =
            'Resolve an earlier model save before starting another change.',
      );
      return;
    }
    final operation = _ModelChange({
      'schema_version': '1.0',
      'expected_revision': models.revision,
      'idempotency_key': newIdempotencyKey(),
      'harness_version': _page == OptionsPage.harnesses ? _harness : null,
      'profile_id': profile,
      'reasoning': reasoning,
    });
    _modelChanges[endpoint] = operation;
    await _sendModel(endpoint, operation);
  }

  Future<void> _sendModel(String endpoint, _ModelChange operation) async {
    final owner = widget.state, api = widget.state.client;
    final generation = owner.connectionGeneration;
    if (api == null || api.agentBase != endpoint || operation.inFlight >= 2) {
      return;
    }
    operation.inFlight++;
    operation.timedOut = false;
    operation.notice = 'Saving model settings…';
    owner.invalidateAgentInference();
    if (mounted) setState(() {});
    bool current() =>
        mounted &&
        identical(owner, widget.state) &&
        owner.ownsConnection(generation, api);
    // Retain the actual request after the display deadline. A timeout is not
    // evidence that the native transaction failed.
    final request = api.saveAgentModels(operation.body);
    unawaited(
      request.then<void>(
        (result) async {
          operation.inFlight--;
          if (!identical(_modelChanges[endpoint], operation)) return;
          if (!_validReceipt(operation, result)) {
            operation.notice =
                'Save result is unconfirmed. Check its native receipt.';
          } else {
            if (identical(_modelChanges[endpoint], operation)) {
              _modelChanges.remove(endpoint);
            }
            if (current()) _notice = 'Model settings saved for new tasks.';
          }
          if (current()) {
            setState(() {});
            await _refresh();
            await owner.revalidateAgentInference();
          }
        },
        onError: (Object error) async {
          operation.inFlight--;
          if (!identical(_modelChanges[endpoint], operation)) return;
          if (error is ApiException &&
              const [400, 409, 422].contains(error.status) &&
              operation.inFlight == 0) {
            if (identical(_modelChanges[endpoint], operation)) {
              _modelChanges.remove(endpoint);
            }
            if (current()) {
              _notice = error.status == 409
                  ? 'Settings changed before this save. Refresh, review the values, then save again.'
                  : 'The host refused this change. Refresh the configured profiles.';
            }
          } else {
            operation.notice =
                'Save result is unconfirmed. Check its receipt before another change.';
          }
          if (current()) {
            setState(() {});
            await _refresh();
            await owner.revalidateAgentInference();
          }
        },
      ),
    );
    try {
      await request.timeout(const Duration(seconds: 8));
    } catch (_) {
      if (operation.sending) {
        operation.timedOut = true;
        operation.notice =
            'Still waiting for the host. The change may have been saved.';
        if (current()) setState(() {});
      }
    }
  }

  Future<void> _checkModelSave() async {
    final operation = _pending, api = widget.state.client, endpoint = _endpoint;
    final generation = widget.state.connectionGeneration;
    if (operation == null || api == null || endpoint == null) return;
    try {
      final result = await api.agentModelReceipt(
        operation.body['idempotency_key'] as String,
      );
      if (!_validReceipt(operation, result)) throw const FormatException();
      if (identical(_modelChanges[endpoint], operation)) {
        _modelChanges.remove(endpoint);
      }
      if (mounted && widget.state.ownsConnection(generation, api)) {
        setState(
          () => _notice = 'The host confirmed the model settings were saved.',
        );
        await _refresh();
        await widget.state.revalidateAgentInference();
      }
    } catch (_) {
      operation.notice =
          'No confirmed receipt yet. Retry uses the same request so it cannot apply twice.';
      if (mounted && widget.state.ownsConnection(generation, api)) {
        setState(() {});
      }
    }
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
                            Text(
                              pending.notice ??
                                  'A model change is awaiting confirmation.',
                            ),
                            TextButton(
                              onPressed: _checkModelSave,
                              child: const Text('Check save status'),
                            ),
                            if (pending.canRetry)
                              TextButton(
                                onPressed: () =>
                                    _sendModel(_endpoint!, pending),
                                child: const Text('Retry the same save'),
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
        SelectableText(value),
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
    if (_models case final models?)
      OptionsModelEditor(
        key: const ValueKey('agent-default-model-editor'),
        models: models,
        busy: _pending != null,
        onSave: _saveModel,
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
        if (_models != null && harness != null && harness.selectable)
          OptionsModelEditor(
            key: ValueKey('model-editor-$_harness'),
            models: _models!,
            harness: _harness,
            busy: _pending != null,
            onSave: _saveModel,
          )
        else
          _unavailable(
            'Model settings unavailable',
            harness?.selectable == false
                ? 'This harness is not approved for new work. Model settings cannot promote it.'
                : 'Refresh the native harness catalog and model profiles.',
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
                    ? 'Custom · ${_models!.effective(row.versionId).profileId}'
                    : 'Inherits Agent default',
              ),
              Text('${row.kind} · ${row.eligibility}'),
            ],
          ),
        ),
    ]);
  }

  Widget _providers() {
    final profiles = _models?.profiles ?? const <AgentModelProfile>[];
    return _scroll([
      const Text(
        'Configured model profiles and supported connection methods. Credentials stay on the host.',
      ),
      ..._errors,
      for (final profile in profiles)
        DesignPanel(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _heading(profile.label),
              _field(
                'Provider',
                profile.provider ?? 'Configuration unavailable',
              ),
              _field('Model', profile.model ?? 'Unavailable'),
              Text(
                profile.local == true
                    ? 'Local endpoint · Configuration is not a model load receipt.'
                    : profile.local == false
                    ? 'Cloud endpoint · Input disclosure rules still apply.'
                    : 'Profile could not be initialized.',
              ),
            ],
          ),
        ),
      _heading('Provider setup'),
      for (final row in const [
        (
          'Meta Model API',
          'API key on the host. Meta account sign-in is not implemented; a Muse subscription does not configure this API.',
        ),
        (
          'LM Studio',
          'Local native Converse endpoint with an already loaded model and exact prompt counting. Agent profiles need an adapter supporting complete Agent requests.',
        ),
        (
          'OpenAI-compatible',
          'Owner-configured endpoint, model and optional secret reference. Compatibility alone does not establish exact Agent prompt counting.',
        ),
      ])
        DesignPanel(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _heading(row.$1),
              const SizedBox(height: 8),
              Text(row.$2),
              const SizedBox(height: 8),
              const Text(
                'Configure in the host settings, then restart the affected service.',
              ),
            ],
          ),
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
      'Model defaults and harness overrides are in Models & providers. Task limits are host-managed.',
    ),
  ]);
  Widget _permissions() {
    final permissions = _agent?['permissions'];
    return _scroll([
      const Text(
        'Effective host grants. A model choice does not grant access or approve actions.',
      ),
      ..._errors,
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
                    : 'Not granted'
              : 'Unavailable',
        ),
      const Text(
        'Use the owner Agent permission controls to change grants. Existing approval and isolation checks remain in force.',
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
