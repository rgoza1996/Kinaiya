import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:uuid/uuid.dart';

import '../api/provider_client.dart';
import '../api/provider_settings.dart';
import 'design_tokens.dart';

/// Embedded owner catalog. The caller's active fence binds its connection epoch.
class OptionsProviderEditor extends StatefulWidget {
  const OptionsProviderEditor({
    super.key,
    required this.api,
    required this.active,
    this.startNewProfile = false,
    this.onNewProfileStarted,
  });
  final ProviderApi? api;
  final bool Function() active;
  final bool startNewProfile;
  final VoidCallback? onNewProfileStarted;
  @override
  State<OptionsProviderEditor> createState() => _OptionsProviderEditorState();
}

class _PendingProviderChange {
  _PendingProviderChange(this.owner, this.body);
  final ProviderApi owner;
  final Map<String, Object?> body;
  bool inFlight = false, uncertain = false, confirmed = false;
  String message = 'Saving provider catalog change…';
  String get key => body['idempotency_key'] as String;
  String get operation => body['operation'] as String;
}

class _OptionsProviderEditorState extends State<OptionsProviderEditor> {
  // Unresolved exact requests only, never durable storage. A confirmed receipt
  // removes all body references, including an imported key, even after closure.
  static final _pending = <String, _PendingProviderChange>{};
  static final _pendingEvents = ValueNotifier<int>(0);
  _PendingProviderChange? _observedPending;
  final _form = GlobalKey<FormState>();
  final _id = TextEditingController(), _label = TextEditingController();
  final _endpoint = TextEditingController(), _model = TextEditingController();
  final _voice = TextEditingController();
  final _max = TextEditingController(), _context = TextEditingController();
  final _timeout = TextEditingController(), _maxLen = TextEditingController();
  final _headLen = TextEditingController(), _artifact = TextEditingController();
  final _confidence = TextEditingController();
  final _credentialId = TextEditingController(), _key = TextEditingController();
  final _customName = TextEditingController(),
      _customBaseUrl = TextEditingController(),
      _customChatPath = TextEditingController(text: '/chat/completions'),
      _customModelsPath = TextEditingController(text: '/models'),
      _customEndpoints = TextEditingController(),
      _customMaxOutputTokens = TextEditingController(),
      _customApiKey = TextEditingController(),
      _customModel = TextEditingController();
  ProviderCatalogSnapshot? _catalog;
  CustomProvidersSnapshot _customCatalog =
      const CustomProvidersSnapshot.empty();
  CustomProviderProfile? _editingCustom;
  String? _customRoleReasoningOverride;
  bool _customEnabled = true,
      _customAllowRemoteHttp = false,
      _customClearKey = false,
      _customBusy = false;
  String? _customDiscoveryBanner;
  ProviderProfile? _editing;
  ProviderCredential? _editingCredential;
  String _screen = 'list', _tab = 'profiles', _kind = 'compatible';
  String? _effort, _credential, _reference;
  bool _cloud = false, _import = true, _reading = false;
  String? _error, _notice, _formError;
  int _readVersion = 0;
  int _refreshSeq = 0;
  bool _newProfileRequested = false;

  bool _current(ProviderApi api, bool Function() capturedActive) =>
      mounted &&
      identical(widget.api, api) &&
      capturedActive() &&
      widget.active();
  _PendingProviderChange? get _operation =>
      widget.api == null ? null : _pending[widget.api!.baseUrl];
  bool get _enabled =>
      widget.api != null &&
      widget.active() &&
      _catalog != null &&
      _operation == null &&
      _pending.length < 16;

  @override
  void initState() {
    super.initState();
    _newProfileRequested = widget.startNewProfile;
    _observedPending = _operation;
    _pendingEvents.addListener(_pendingChanged);
    unawaited(_refresh());
  }

  @override
  void didUpdateWidget(covariant OptionsProviderEditor oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api)) {
      _readVersion++;
      _refreshSeq++;
      _key.clear();
      _customApiKey.clear();
      _catalog = null;
      _customCatalog = const CustomProvidersSnapshot.empty();
      _editingCustom = null;
      _customDiscoveryBanner = null;
      _error = null;
      _notice = null;
      _screen = 'list';
      _editing = null;
      _editingCredential = null;
      _observedPending = _operation;
      unawaited(_refresh());
    }
  }

  @override
  void dispose() {
    _readVersion++;
    _refreshSeq++;
    _pendingEvents.removeListener(_pendingChanged);
    _key.clear();
    _customApiKey.clear();
    for (final controller in [
      _id,
      _label,
      _endpoint,
      _model,
      _voice,
      _max,
      _context,
      _timeout,
      _maxLen,
      _headLen,
      _artifact,
      _confidence,
      _credentialId,
      _key,
      _customName,
      _customBaseUrl,
      _customChatPath,
      _customModelsPath,
      _customEndpoints,
      _customMaxOutputTokens,
      _customApiKey,
      _customModel,
    ]) {
      controller.dispose();
    }
    super.dispose();
  }

  void _pendingChanged() {
    if (!mounted || !widget.active()) return;
    final previous = _observedPending, current = _operation, api = widget.api;
    _observedPending = current;
    setState(() {});
    if (previous != null &&
        current == null &&
        previous.confirmed &&
        api != null &&
        api.sameOwner(previous.owner)) {
      setState(() {
        _screen = 'list';
        _key.clear();
        _editing = null;
        _editingCredential = null;
        _notice =
            'Confirmed saved catalog change. Active role state is separate.';
      });
      unawaited(_refresh());
    }
  }

  Future<void> _refresh() async {
    final api = widget.api, version = ++_readVersion;
    final active = widget.active;
    if (api == null || !widget.active()) return;
    setState(() {
      _reading = true;
      _error = null;
    });
    try {
      final snapshot = await api.inspect();
      if (_current(api, active) && version == _readVersion) {
        setState(() {
          _catalog = snapshot;
        });
      }
    } catch (_) {
      if (_current(api, active) && version == _readVersion) {
        setState(() {
          _error =
              'Provider catalog could not be read. Existing role settings have not been changed.';
        });
      }
    } finally {
      if (_current(api, active) && version == _readVersion) {
        setState(() {
          _reading = false;
        });
        if (_newProfileRequested && _enabled) {
          _newProfileRequested = false;
          _profileForm();
          widget.onNewProfileStarted?.call();
        }
      }
    }
  }

  void _clearPending(_PendingProviderChange pending) {
    if (identical(_pending[pending.owner.baseUrl], pending)) {
      _pending.remove(pending.owner.baseUrl);
      pending.body
          .clear(); // No retained credential after confirmation/refusal.
      _pendingEvents.value++;
    }
  }

  Future<void> _send(_PendingProviderChange pending, ProviderApi api) async {
    if (!api.sameOwner(pending.owner) || pending.inFlight) return;
    final active = widget.active;
    final wasUncertain = pending.uncertain;
    pending.inFlight = true;
    _pendingEvents.value++;
    if (_current(api, active)) setState(() {});
    try {
      final receipt = await api.change(pending.body);
      if (!receipt.matches(pending.body)) {
        throw const ProviderFailure(
          'Provider receipt did not match',
          unconfirmed: true,
        );
      }
      pending.confirmed = true;
      _clearPending(pending);
      if (_current(api, active)) {
        setState(() {
          _screen = 'list';
          _editing = null;
          _editingCredential = null;
          _notice = 'Provider settings saved. Active models have not changed.';
          _formError = null;
        });
      }
    } on ProviderFailure catch (failure) {
      if (!failure.unconfirmed && !wasUncertain) {
        _clearPending(pending);
        if (_current(api, active)) {
          setState(() {
            _formError = failure.status == 409
                ? 'The catalog changed. Refresh and review the current generation before saving again.'
                : 'Change refused. Check the supported fields, permissions and bounds. Imported keys have been cleared.';
          });
        }
      } else {
        pending.uncertain = true;
        pending.message =
            'Save outcome is unconfirmed. Reconcile this exact request before making another change.';
      }
    } catch (_) {
      pending.uncertain = true;
      pending.message =
          'Save outcome is unconfirmed. Reconcile the exact request.';
    } finally {
      pending.inFlight = false;
      _pendingEvents.value++;
      if (_current(api, active)) setState(() {});
    }
  }

  Future<void> _checkReceipt() async {
    final api = widget.api, pending = _operation;
    final active = widget.active;
    if (api == null ||
        pending == null ||
        pending.inFlight ||
        !api.sameOwner(pending.owner)) {
      return;
    }
    pending.inFlight = true;
    _pendingEvents.value++;
    setState(() {});
    try {
      final receipt = await api.receipt(pending.key);
      if (!receipt.matches(pending.body)) throw const FormatException();
      if (pending.operation == 'credential_import' ||
          pending.operation == 'credential_reference') {
        pending.message =
            'A receipt exists. Retry the same request to verify its exact credential registration; this cannot create a second generation.';
      } else {
        pending.confirmed = true;
        _clearPending(pending);
        if (_current(api, active)) {
          setState(() {
            _screen = 'list';
            _notice =
                'Confirmed saved catalog change. Active role state is separate.';
          });
        }
      }
    } on ProviderFailure catch (failure) {
      pending.message = failure.status == 404
          ? 'No committed receipt is visible yet. The earlier request may still commit. Retry only this same request.'
          : 'Receipt inspection is unavailable. The save outcome remains unconfirmed.';
    } catch (_) {
      pending.message =
          'Receipt did not establish this save. Keep the exact request for reconciliation.';
    } finally {
      pending.inFlight = false;
      _pendingEvents.value++;
      if (_current(api, active)) setState(() {});
    }
  }

  void _begin(Map<String, Object?> fields) {
    final api = widget.api, catalog = _catalog;
    if (api == null ||
        catalog == null ||
        !_enabled ||
        catalog.revision >= providerMaxRevision) {
      return;
    }
    final body = <String, Object?>{
      'schema_version': '1.0',
      'expected_revision': catalog.revision,
      'idempotency_key': const Uuid().v4(),
      ...fields,
    };
    final pending = _PendingProviderChange(api, body);
    _pending[api.baseUrl] = pending;
    _pendingEvents.value++;
    _key.clear(); // Only the unresolved exact request now retains an import.
    _notice = null;
    _formError = null;
    unawaited(_send(pending, api));
  }

  void _profileForm([ProviderProfile? profile]) {
    final config = profile?.config;
    setState(() {
      _screen = 'profile';
      _editing = profile;
      _formError = null;
      _id.text = profile?.id ?? '';
      _label.text = profile?.label ?? '';
      _kind = profile?.kind ?? 'compatible';
      _endpoint.text = config?['base_url'] as String? ?? '';
      _model.text = profile?.model ?? '';
      _voice.text = config?['voice'] as String? ?? '';
      _cloud = config?['allow_cloud'] == true;
      _effort = config?['reasoning_effort'] as String?;
      _max.text = config?['max_tokens'] != null
          ? '${config!['max_tokens']}'
          : '';
      _context.text = '${config?['context_tokens'] ?? ''}';
      _timeout.text = '${config?['timeout_s'] ?? 90}';
      _maxLen.text = '${config?['max_len'] ?? ''}';
      _headLen.text = '${config?['head_max_len'] ?? ''}';
      _artifact.text = config?['expected_artifact_sha256'] as String? ?? '';
      _confidence.text = '${config?['min_confidence'] ?? ''}';
      _credential = profile?.credential?.value;
    });
  }

  void _credentialForm([ProviderCredential? credential]) {
    setState(() {
      _screen = 'credential';
      _editingCredential = credential;
      _formError = null;
      _credentialId.text = credential?.id ?? '';
      _key.clear();
      _import = credential == null || credential.source == 'imported';
      _reference = credential?.source == 'imported'
          ? null
          : '${credential?.source ?? 'muse'}:${credential?.reference ?? ''}';
    });
  }

  String? _identifier(String? value, {bool credential = false}) {
    try {
      providerIdentifier(value);
    } on FormatException {
      return 'Use 1–64 letters, digits, dots, underscores or hyphens.';
    }
    if (credential ? _editingCredential == null : _editing == null) {
      final exists = credential
          ? _catalog!.credentials.any((c) => c.id == value)
          : _catalog!.profiles.any((p) => p.id == value);
      if (exists) return 'This ID exists. Edit or rotate its current entry.';
    }
    return null;
  }

  String? _number(String? value, int min, int max, {bool optional = false}) {
    if (optional && (value == null || value.isEmpty)) return null;
    final n = int.tryParse(value ?? '');
    return n == null || n < min || n > max
        ? 'Enter a whole number from $min to $max.'
        : null;
  }

  String? _bounded(String? value, int max) {
    if (value == null ||
        value.trim().isEmpty ||
        value != value.trim() ||
        utf8.encode(value).length > max ||
        value.codeUnits.any((c) => c < 32 || c == 127)) {
      return 'Enter 1–$max UTF-8 bytes without controls or outer spaces.';
    }
    return null;
  }

  Map<String, Object?> _config() {
    final binding = _credential;
    Map<String, Object?>? credential;
    if (binding != null) {
      final split = binding.indexOf(':');
      credential = ProviderCredentialBinding(
        binding.substring(0, split),
        binding.substring(split + 1),
      ).toJson();
    }
    return providerConfig({
      'kind': _kind,
      'base_url': _endpoint.text,
      'model': _model.text,
      if (_kind == 'openrouter_tts') 'voice': _voice.text,
      'allow_cloud': _cloud,
      if (_kind != 'lmstudio') 'credential': credential,
      if (_kind != 'laya' &&
          _kind != 'native_d1' &&
          _kind != 'meta_stt' &&
          _kind != 'openrouter_tts') ...{
        'max_tokens': _max.text.trim().isEmpty
            ? (_kind == 'lmstudio' ? 128 : 2048)
            : int.tryParse(_max.text.trim()),
        'reasoning_effort': _effort,
      },
      if (_kind == 'meta' || _kind == 'compatible')
        'context_tokens': _context.text.isEmpty
            ? null
            : int.tryParse(_context.text),
      if (_kind != 'meta') 'timeout_s': int.tryParse(_timeout.text),
      if (_kind == 'native_d1')
        'min_confidence': double.tryParse(_confidence.text),
      if (_kind == 'laya') ...{
        'max_len': _maxLen.text.isEmpty ? null : int.tryParse(_maxLen.text),
        'head_max_len': _headLen.text.isEmpty
            ? null
            : int.tryParse(_headLen.text),
        'expected_artifact_sha256': _artifact.text.isEmpty
            ? null
            : _artifact.text,
      },
    });
  }

  void _saveProfile() {
    if (!_enabled || !(_form.currentState?.validate() ?? false)) return;
    try {
      _begin({
        'operation': 'profile_upsert',
        'profile_id': _id.text,
        'label': _label.text,
        'config': _config(),
      });
    } on FormatException {
      setState(() {
        _formError =
            'Check the endpoint, required credential and native limits. Cloud origins require HTTPS and explicit authorization. URLs cannot contain credentials, queries or fragments.';
      });
    }
  }

  void _saveCredential() {
    if (!_enabled || !(_form.currentState?.validate() ?? false)) return;
    if (_import) {
      _begin({
        'operation': 'credential_import',
        'credential_id': _credentialId.text,
        'key': _key.text,
      });
    } else {
      final reference = _catalog!.knownReferences
          .where((r) => r.id == _reference)
          .firstOrNull;
      if (reference == null) {
        setState(() {
          _formError = 'Choose a known host credential reference.';
        });
        return;
      }
      _begin({
        'operation': 'credential_reference',
        'credential_id': _credentialId.text,
        'source': reference.source,
        if (reference.reference != null) 'reference': reference.reference,
      });
    }
  }

  Widget _panel(List<Widget> children) => DesignPanel(
    child: Material(
      type: MaterialType.transparency,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (var i = 0; i < children.length; i++) ...[
            if (i > 0) const SizedBox(height: 16),
            children[i],
          ],
        ],
      ),
    ),
  );
  Widget _title(String text) =>
      Text(text, style: Theme.of(context).textTheme.titleLarge);
  Widget _field(
    TextEditingController controller,
    String label, {
    int maxLength = 128,
    bool enabled = true,
    String? Function(String?)? validator,
    String? help,
    bool numeric = false,
    bool decimal = false,
    bool secret = false,
  }) => TextFormField(
    controller: controller,
    enabled: enabled && _enabled,
    maxLength: maxLength,
    obscureText: secret,
    autocorrect: !secret,
    enableSuggestions: !secret,
    enableIMEPersonalizedLearning: !secret,
    autofillHints: secret ? const [] : null,
    keyboardType: decimal
        ? const TextInputType.numberWithOptions(decimal: true)
        : numeric
        ? TextInputType.number
        : secret
        ? TextInputType.visiblePassword
        : TextInputType.text,
    inputFormatters: numeric ? [FilteringTextInputFormatter.digitsOnly] : null,
    decoration: InputDecoration(
      labelText: label,
      helperText: help,
      border: const OutlineInputBorder(),
    ),
    validator: validator,
  );

  Widget _pendingPanel(_PendingProviderChange pending) {
    final api = widget.api!;
    if (!api.sameOwner(pending.owner)) {
      return _panel([
        const Text(
          'An unresolved change belongs to another owner connection. Reconnect that owner to reconcile it before editing this endpoint.',
        ),
      ]);
    }
    return _panel([
      _title(
        pending.inFlight
            ? 'Saving catalog change'
            : 'Confirm this catalog change',
      ),
      Text(pending.message),
      Text('Request ID: ${pending.key}'),
      const Text(
        'A timeout does not prove failure. Pending data stays in this app’s memory only.',
      ),
      Wrap(
        spacing: 12,
        runSpacing: 8,
        children: [
          OutlinedButton(
            onPressed: pending.inFlight || !widget.active()
                ? null
                : _checkReceipt,
            child: const Text('Check receipt'),
          ),
          FilledButton(
            onPressed: pending.inFlight || !widget.active()
                ? null
                : () => unawaited(_send(pending, api)),
            child: const Text('Retry same request'),
          ),
        ],
      ),
    ]);
  }

  Widget _profileEditor() {
    final activeCredentials = _catalog!.credentials
        .where((c) => !c.removed)
        .toList();
    final selected = _credential;
    final known = activeCredentials.any((c) => c.binding.value == selected);
    final current = _editing == null
        ? null
        : _catalog!.profiles.where((p) => p.id == _editing!.id).firstOrNull;
    final stale =
        _editing != null &&
        (current?.generation != _editing!.generation ||
            current?.removed != _editing!.removed);
    return Form(
      key: _form,
      child: _panel([
        _title(
          _editing == null ? 'Add provider profile' : 'Edit ${_editing!.label}',
        ),
        const Text(
          'Save connection settings for this provider. Active models will not change.',
        ),
        if (stale) ...[
          const Text(
            'This profile changed since editing began. Reload it before saving.',
          ),
          OutlinedButton(
            onPressed: _enabled && current != null
                ? () => _profileForm(current)
                : null,
            child: const Text('Reload current generation'),
          ),
        ],
        _field(
          _id,
          'Profile ID',
          enabled: _editing == null,
          maxLength: 64,
          validator: (v) => _identifier(v),
        ),
        _field(_label, 'Display name', validator: (v) => _bounded(v, 128)),
        DropdownButtonFormField<String>(
          key: ValueKey('provider-kind-$_kind'),
          initialValue: _kind,
          decoration: const InputDecoration(
            labelText: 'Provider type',
            border: OutlineInputBorder(),
          ),
          items: const [
            DropdownMenuItem(value: 'meta', child: Text('Meta API')),
            DropdownMenuItem(
              value: 'meta_stt',
              child: Text('Meta · speech to text'),
            ),
            DropdownMenuItem(
              value: 'openrouter_tts',
              child: Text('OpenRouter · text to speech'),
            ),
            DropdownMenuItem(
              value: 'lmstudio',
              child: Text('LM Studio · native local'),
            ),
            DropdownMenuItem(
              value: 'compatible',
              child: Text('OpenAI-compatible endpoint'),
            ),
            DropdownMenuItem(
              value: 'laya',
              child: Text('Laya · typed Decisions'),
            ),
            DropdownMenuItem(
              value: 'native_d1',
              child: Text('D1 · native local Decisions'),
            ),
          ],
          onChanged: _enabled
              ? (kind) {
                  if (kind != null) {
                    setState(() {
                      _kind = kind;
                      _cloud =
                          kind == 'meta' ||
                          kind == 'meta_stt' ||
                          kind == 'openrouter_tts';
                      if (kind == 'meta_stt') {
                        _model.text = 'muse-voice-transcribe-1.0';
                      }
                      if (kind == 'openrouter_tts') {
                        _model.text = 'hexgrad/kokoro-82m';
                        _voice.text = 'af_heart';
                      } else {
                        _voice.clear();
                      }
                      _effort = kind == 'meta'
                          ? 'xhigh'
                          : kind == 'lmstudio'
                          ? 'none'
                          : null;
                      _max.text = kind == 'lmstudio' ? '128' : '2048';
                      _timeout.text =
                          kind == 'meta_stt' || kind == 'openrouter_tts'
                          ? '120'
                          : kind == 'laya' || kind == 'native_d1'
                          ? '5'
                          : kind == 'lmstudio'
                          ? '60'
                          : '90';
                      _context.clear();
                      _maxLen.clear();
                      _headLen.clear();
                      _artifact.clear();
                      _confidence.clear();
                      _endpoint.clear();
                      if (kind == 'lmstudio') _credential = null;
                    });
                  }
                }
              : null,
        ),
        _field(
          _endpoint,
          'Endpoint URL',
          maxLength: 2048,
          validator: (v) => _bounded(v, 2048),
          help: _kind == 'meta_stt'
              ? 'Explicit Meta transcription origin ending in /v1. Remote origins require HTTPS.'
              : _kind == 'openrouter_tts'
              ? 'Exact https://openrouter.ai/api/v1 origin, or an explicit loopback HTTP /api/v1 gateway.'
              : _kind == 'lmstudio'
              ? 'Explicit loopback native endpoint with a port, such as http://127.0.0.1:1234.'
              : _kind == 'native_d1'
              ? 'Local root URL with an explicit port, such as http://127.0.0.1:8816. No /v1 path.'
              : 'No embedded credentials, queries or fragments. Remote origins require HTTPS.',
        ),
        _field(
          _model,
          'Exact model ID or native loaded alias',
          enabled: _kind != 'meta_stt' && _kind != 'openrouter_tts',
          maxLength: 256,
          validator: (v) => _bounded(v, 256),
        ),
        if (_kind == 'meta_stt')
          const Text(
            'Buffered Meta speech-to-text only. Saving this declaration does not capture audio, grant cloud disclosure, test readiness, or select it for a role.',
          ),
        if (_kind == 'openrouter_tts') ...[
          _field(
            _voice,
            'Voice ID',
            enabled: false,
            maxLength: 64,
            help:
                'af_heart is the only voice supported by this native adapter.',
          ),
          const Text(
            'Kokoro speech output uses 24 kHz mono PCM. A protected native budget must be configured before this profile is eligible. Saving never creates a budget allowance, synthesizes speech, grants disclosure, or checks readiness.',
          ),
        ],
        if (_kind == 'meta')
          const Text(
            'Meta is a cloud provider, including when reached through a local budget gateway. Contributor disclosure restrictions still apply.',
          ),
        if (_kind != 'lmstudio' && _kind != 'native_d1')
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: _cloud,
            title: const Text('Authorize this profile to use a cloud endpoint'),
            subtitle: const Text(
              'Input, transcript, tool and personality disclosure are checked independently for every request.',
            ),
            onChanged:
                _enabled &&
                    _kind != 'meta' &&
                    _kind != 'meta_stt' &&
                    _kind != 'openrouter_tts'
                ? (value) => setState(() {
                    _cloud = value;
                  })
                : null,
          ),
        if (_kind != 'lmstudio')
          DropdownButtonFormField<String>(
            key: ValueKey('provider-credential-$_credential'),
            initialValue: _credential ?? '',
            isExpanded: true,
            decoration: const InputDecoration(
              labelText: 'Exact credential generation',
              border: OutlineInputBorder(),
            ),
            items: [
              const DropdownMenuItem(value: '', child: Text('No credential')),
              if (selected != null && !known)
                DropdownMenuItem(
                  value: selected,
                  enabled: false,
                  child: const Text(
                    'Retired or unavailable credential generation',
                  ),
                ),
              for (final credential in activeCredentials)
                DropdownMenuItem(
                  value: credential.binding.value,
                  child: Text(
                    '${credential.id} · ${credential.generation.substring(0, 8)}',
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
            ],
            validator: (value) =>
                value != null &&
                    value.isNotEmpty &&
                    !activeCredentials.any((c) => c.binding.value == value)
                ? 'Choose a current credential generation.'
                : (_kind == 'meta' ||
                          _kind == 'meta_stt' ||
                          _kind == 'openrouter_tts') &&
                      (value == null || value.isEmpty)
                ? 'This provider requires a stored credential generation.'
                : null,
            onChanged: _enabled
                ? (value) => setState(() {
                    _credential = value == null || value.isEmpty ? null : value;
                  })
                : null,
          ),
        if (_kind != 'lmstudio')
          const Text(
            'To register or rotate a key, use Credentials. Rotation retires the old generation for new use; update each affected profile explicitly.',
          ),
        if (_kind != 'laya' &&
            _kind != 'native_d1' &&
            _kind != 'meta_stt' &&
            _kind != 'openrouter_tts') ...[
          _field(
            _max,
            'Maximum output tokens',
            maxLength: 5,
            numeric: true,
            help:
                'Optional — leave blank to let the model decide its own maximum output tokens.',
            validator: (v) => _number(
              v,
              16,
              _kind == 'lmstudio' ? 2048 : 65536,
              optional: true,
            ),
          ),
          const Text(
            'Reasoning effort is configured globally or per model in Models (not per provider).',
          ),
        ],
        if (_kind == 'meta' || _kind == 'compatible')
          _field(
            _context,
            'Declared context tokens · optional',
            maxLength: 16,
            numeric: true,
            validator: (v) =>
                _number(v, 64, providerMaxRevision, optional: true),
            help:
                'An explicit declaration is not an actual prompt count or model qualification.',
          ),
        if (_kind != 'meta')
          _field(
            _timeout,
            'Request timeout · seconds',
            maxLength: 3,
            numeric: true,
            validator: (v) => _number(
              v,
              1,
              _kind == 'laya' || _kind == 'native_d1'
                  ? 120
                  : _kind == 'meta_stt'
                  ? 300
                  : _kind == 'openrouter_tts'
                  ? 120
                  : 900,
            ),
          ),
        if (_kind == 'native_d1') ...[
          _field(
            _confidence,
            'Minimum decision confidence',
            maxLength: 24,
            decimal: true,
            validator: (value) {
              final floor = double.tryParse(value ?? '');
              return floor == null || !floor.isFinite || floor < 0 || floor > 1
                  ? 'Enter a number from 0 to 1.'
                  : null;
            },
            help:
                'Choose your refusal threshold. This is not measured accuracy.',
          ),
          const Text(
            'Native d1 provides text Decisions only. Lower-confidence decisions are refused. Saving does not run or load a model, check readiness, or enable audio.',
          ),
        ],
        if (_kind == 'laya') ...[
          const Text(
            'Laya returns typed choices. It has no conversational reasoning or voice controls.',
          ),
          _field(
            _maxLen,
            'Token window · optional',
            maxLength: 4,
            numeric: true,
            validator: (v) => _number(v, 2, 8192, optional: true),
          ),
          _field(
            _headLen,
            'Head token window · optional',
            maxLength: 4,
            numeric: true,
            validator: (v) => _number(v, 1, 8192, optional: true),
          ),
          _field(
            _artifact,
            'Expected artifact SHA-256 · optional',
            maxLength: 64,
            validator: (v) =>
                v == null || v.isEmpty || RegExp(r'^[0-9a-f]{64}$').hasMatch(v)
                ? null
                : 'Enter exactly 64 lowercase hexadecimal characters.',
          ),
        ],
        if (_kind == 'compatible')
          const Text(
            'Compatible API shape alone does not provide native measured Agent/Converse admission. The role owner must report support; this profile is unqualified.',
          ),
        const Text('Connection and model readiness have not been checked.'),
        if (_formError != null) Text(_formError!),
        Wrap(
          spacing: 12,
          runSpacing: 8,
          children: [
            FilledButton(
              onPressed: _enabled && !stale ? _saveProfile : null,
              child: Text(
                _editing?.removed == true
                    ? 'Save restored generation'
                    : 'Save profile',
              ),
            ),
            OutlinedButton(
              onPressed: _operation == null
                  ? () => setState(() {
                      _screen = 'list';
                      _formError = null;
                    })
                  : null,
              child: const Text('Cancel'),
            ),
          ],
        ),
      ]),
    );
  }

  Widget _credentialEditor() {
    final refs = _catalog!.knownReferences;
    final validRef = refs.any((r) => r.id == _reference);
    final current = _editingCredential == null
        ? null
        : _catalog!.credentials
              .where((c) => c.id == _editingCredential!.id)
              .firstOrNull;
    final stale =
        _editingCredential != null &&
        (current?.generation != _editingCredential!.generation ||
            current?.removed != _editingCredential!.removed);
    return Form(
      key: _form,
      child: _panel([
        _title(
          _editingCredential == null
              ? 'Register credential'
              : 'Rotate ${_editingCredential!.id}',
        ),
        const Text(
          'Save a protected key on this computer. Rotation retires the previous key for new use; update any profiles that used it. Existing tasks keep their original connection.',
        ),
        if (stale) ...[
          const Text(
            'This credential changed since editing began. Reload it before saving.',
          ),
          OutlinedButton(
            onPressed: _enabled && current != null
                ? () => _credentialForm(current)
                : null,
            child: const Text('Reload current generation'),
          ),
        ],
        _field(
          _credentialId,
          'Credential ID',
          maxLength: 64,
          enabled: _editingCredential == null,
          validator: (v) => _identifier(v, credential: true),
        ),
        SegmentedButton<bool>(
          segments: const [
            ButtonSegment(value: true, label: Text('Import key')),
            ButtonSegment(value: false, label: Text('Known host reference')),
          ],
          selected: {_import},
          onSelectionChanged: _enabled
              ? (values) => setState(() {
                  _import = values.single;
                  _key.clear();
                  if (_reference == null && refs.isNotEmpty) {
                    _reference = refs.first.id;
                  }
                })
              : null,
        ),
        if (_import)
          _field(
            _key,
            'API key / access token',
            maxLength: 16384,
            secret: true,
            help:
                'Sent once to the authenticated native owner. Never stored in browser preferences or exports.',
            validator: (value) =>
                value == null ||
                    value.isEmpty ||
                    value.length > 16384 ||
                    value.codeUnits.any((c) => c <= 32 || c >= 127)
                ? 'Enter a bounded ASCII token without spaces or controls.'
                : null,
          ),
        if (!_import) ...[
          DropdownButtonFormField<String>(
            key: ValueKey('known-provider-reference-$_reference'),
            initialValue: validRef ? _reference : null,
            isExpanded: true,
            decoration: const InputDecoration(
              labelText: 'Known native credential source',
              border: OutlineInputBorder(),
            ),
            items: [
              for (final reference in refs)
                DropdownMenuItem(
                  value: reference.id,
                  child: Text(reference.label, overflow: TextOverflow.ellipsis),
                ),
            ],
            validator: (value) =>
                value == null ? 'Choose a known host reference.' : null,
            onChanged: _enabled
                ? (value) => setState(() {
                    _reference = value;
                  })
                : null,
          ),
          const Text(
            'Registration explicitly reads this known source and snapshots its key into a protected immutable generation. External file/environment rotation requires registering a new generation. No arbitrary path or environment variable is accepted.',
          ),
        ],
        const Text(
          'Stored key availability does not prove provider login, account access, billing or model readiness. No OAuth flow or inference test runs here.',
        ),
        if (_formError != null) Text(_formError!),
        Wrap(
          spacing: 12,
          runSpacing: 8,
          children: [
            FilledButton(
              onPressed: _enabled && !stale ? _saveCredential : null,
              child: const Text('Store new credential generation'),
            ),
            OutlinedButton(
              onPressed: _operation == null
                  ? () => setState(() {
                      _key.clear();
                      _screen = 'list';
                      _formError = null;
                    })
                  : null,
              child: const Text('Cancel'),
            ),
          ],
        ),
      ]),
    );
  }

  Future<void> _refreshCustomProviders() async {
    final api = widget.api, seq = ++_refreshSeq;
    final active = widget.active;
    if (api == null || !widget.active()) return;
    setState(() {
      _customBusy = true;
      _error = null;
    });
    try {
      final snap = await api.inspectCustomProviders();
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _customCatalog = snap;
        });
      }
    } catch (_) {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _error = 'Custom .env provider store could not be read from host.';
        });
      }
    } finally {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _customBusy = false;
        });
      }
    }
  }

  void _customApiForm([CustomProviderProfile? profile]) {
    setState(() {
      _tab = 'custom_apis';
      _screen = 'custom_api';
      _editingCustom = profile;
      _formError = null;
      _notice = null;
      _customDiscoveryBanner = null;
      _customName.text = profile?.name ?? '';
      _customBaseUrl.text = profile?.baseUrl ?? '';
      _customChatPath.text =
          profile?.chatCompletionsPath ?? '/chat/completions';
      _customModelsPath.text = profile?.modelsPath ?? '/models';
      _customEndpoints.text = profile?.customEndpoints ?? '';
      _customMaxOutputTokens.text = profile?.maxOutputTokens != null
          ? '${profile!.maxOutputTokens}'
          : '';
      _customApiKey.clear();
      _customModel.text = profile?.selectedModel ?? '';
      _customEnabled = profile?.enabled ?? true;
      _customAllowRemoteHttp = profile?.allowRemoteHttp ?? false;
      _customClearKey = false;
    });
  }

  Future<void> _saveCustomApi() async {
    final api = widget.api;
    final active = widget.active;
    if (api == null || !_enabled || _customBusy) return;
    final name = _customName.text.trim();
    final baseUrl = _customBaseUrl.text.trim();
    final chatPath = _customChatPath.text.trim().isEmpty
        ? '/chat/completions'
        : _customChatPath.text.trim();
    final modelsPath = _customModelsPath.text.trim().isEmpty
        ? '/models'
        : _customModelsPath.text.trim();
    final customEndpoints = _customEndpoints.text.trim();
    final maxTokensRaw = _customMaxOutputTokens.text.trim();
    int? maxOutputTokens;
    if (maxTokensRaw.isNotEmpty) {
      maxOutputTokens = int.tryParse(maxTokensRaw);
      if (maxOutputTokens == null || maxOutputTokens < 1) {
        setState(
          () => _formError =
              'Maximum Output Tokens must be a positive integer or left blank for model default.',
        );
        return;
      }
    }
    final apiKey = _customApiKey.text.trim();
    final modelId = _customModel.text.trim();
    if (name.isEmpty) {
      setState(() => _formError = 'Enter a Custom Provider Name.');
      return;
    }
    if (baseUrl.isEmpty) {
      setState(() => _formError = 'Enter an OpenAI-Compatible Base URL.');
      return;
    }
    final seq = ++_refreshSeq;
    setState(() {
      _customBusy = true;
      _formError = null;
      _error = null;
    });
    try {
      final editing = _editingCustom;
      final payload = editing == null
          ? <String, Object?>{
              'action': 'add_custom_provider',
              'name': name,
              'base_url': baseUrl,
              'chat_completions_path': chatPath,
              'models_path': modelsPath,
              'custom_endpoints': customEndpoints,
              'max_output_tokens': maxOutputTokens,
              'api_key': apiKey,
              'enabled': _customEnabled,
              'selected_model': modelId,
              'allow_remote_http': _customAllowRemoteHttp,
            }
          : <String, Object?>{
              'action': 'update_custom_provider',
              'profile_id': editing.profileId,
              'name': name,
              'base_url': baseUrl,
              'chat_completions_path': chatPath,
              'models_path': modelsPath,
              'custom_endpoints': customEndpoints,
              'max_output_tokens': maxOutputTokens,
              'clear_max_output_tokens': maxOutputTokens == null,
              if (apiKey.isNotEmpty) 'api_key': apiKey,
              'clear_api_key': _customClearKey,
              'enabled': _customEnabled,
              'selected_model': modelId,
              'allow_remote_http': _customAllowRemoteHttp,
            };
      final snap = await api.changeCustomProvider(payload);
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _customCatalog = snap;
          _customApiKey.clear();
          _editingCustom = null;
          _screen = 'list';
          _notice = editing == null
              ? 'Saved custom API "$name" to ${snap.envFileName} (0600).'
              : 'Updated custom API "$name" in ${snap.envFileName} (0600).';
        });
      }
    } catch (_) {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _formError =
              'Custom API settings were rejected. Verify Base URL (HTTPS or loopback HTTP), endpoint paths, and Model ID.';
        });
      }
    } finally {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() => _customBusy = false);
      }
    }
  }

  Future<void> _setGlobalReasoningEffort(String effort) async {
    final api = widget.api;
    final active = widget.active;
    if (api == null || !_enabled || _customBusy) return;
    final seq = ++_refreshSeq;
    setState(() => _customBusy = true);
    try {
      final snap = await api.changeCustomProvider({
        'action': 'set_global_reasoning_effort',
        'reasoning_effort': effort,
      });
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _customCatalog = snap;
          _notice = 'Updated global reasoning effort to $effort.';
        });
      }
    } catch (_) {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _error = 'Could not update global reasoning effort.';
        });
      }
    } finally {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() => _customBusy = false);
      }
    }
  }

  Future<void> _discoverCustomModels(CustomProviderProfile profile) async {
    final api = widget.api;
    final active = widget.active;
    if (api == null || !_enabled || _customBusy) return;
    final seq = ++_refreshSeq;
    setState(() {
      _customBusy = true;
      _error = null;
      _customDiscoveryBanner =
          'Querying GET ${profile.baseUrl}${profile.modelsPath} for ${profile.name}…';
    });
    try {
      final result = await api.discoverCustomModels(profile.profileId);
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _customCatalog = result.snapshot;
          _customDiscoveryBanner = result.message;
          final refreshed = result.snapshot.profiles
              .where((p) => p.profileId == profile.profileId)
              .firstOrNull;
          if (_editingCustom?.profileId == profile.profileId &&
              refreshed != null) {
            _editingCustom = refreshed;
            if (_customModel.text.trim().isEmpty &&
                refreshed.selectedModel.isNotEmpty) {
              _customModel.text = refreshed.selectedModel;
            }
          }
        });
      }
    } catch (_) {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _customDiscoveryBanner =
              'Could not query /models for ${profile.name}. You can still save a manual Model ID.';
        });
      }
    } finally {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() => _customBusy = false);
      }
    }
  }

  Future<void> _toggleCustomEnabled(
    CustomProviderProfile profile,
    bool enabled,
  ) async {
    final api = widget.api;
    final active = widget.active;
    if (api == null || !_enabled || _customBusy) return;
    final seq = ++_refreshSeq;
    setState(() => _customBusy = true);
    try {
      final snap = await api.changeCustomProvider({
        'action': 'set_custom_provider_enabled',
        'profile_id': profile.profileId,
        'enabled': enabled,
      });
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _customCatalog = snap;
          _notice =
              '${profile.name} (${profile.profileId}) is now ${enabled ? 'enabled' : 'disabled'}.';
        });
      }
    } catch (_) {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _error = 'Could not update enabled state for ${profile.name}.';
        });
      }
    } finally {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() => _customBusy = false);
      }
    }
  }

  Future<void> _bindCustomRole(
    CustomProviderProfile profile,
    String role,
  ) async {
    final api = widget.api;
    final active = widget.active;
    if (api == null || !_enabled || _customBusy) return;
    final modelId = profile.selectedModel.trim().isNotEmpty
        ? profile.selectedModel.trim()
        : (profile.discoveredModels.isNotEmpty
              ? profile.discoveredModels.first
              : '');
    if (modelId.isEmpty) {
      setState(() {
        _error =
            'Select or enter a Model ID on ${profile.name} before binding it to $role.';
      });
      return;
    }
    final seq = ++_refreshSeq;
    setState(() => _customBusy = true);
    try {
      final snap = await api.changeCustomProvider({
        'action': 'bind_custom_role_model',
        'role': role,
        'profile_id': profile.profileId,
        'model_id': modelId,
        if (_customRoleReasoningOverride != null)
          'reasoning_effort': _customRoleReasoningOverride,
      });
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _customCatalog = snap;
          _notice =
              'Saved proposed $role selection for ${profile.name} (${profile.profileId}:$modelId) · proposed_unavailable pending native role-runtime admission.';
        });
      }
    } catch (_) {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() {
          _error = 'Could not bind ${profile.name} to $role.';
        });
      }
    } finally {
      if (_current(api, active) && seq == _refreshSeq) {
        setState(() => _customBusy = false);
      }
    }
  }

  Future<void> _removeCustomProfile(
    CustomProviderProfile profile, {
    bool confirmed = false,
  }) => _confirmAndRemoveCustomProfile(profile, confirmed: confirmed);

  Future<void> _confirmAndRemoveCustomProfile(
    CustomProviderProfile profile, {
    bool confirmed = false,
  }) async {
    final api = widget.api;
    final generation = widget.active;
    if (api == null || !_enabled || _customBusy) return;
    final capturedRevision = _customCatalog.revision;
    final capturedStateSha256 = _customCatalog.stateSha256;
    final capturedProfileId = profile.profileId;
    final capturedName = profile.name;
    final capturedBaseUrl = profile.baseUrl;
    final capturedSelectedModel = profile.selectedModel;
    final capturedReferencedBy = List<String>.unmodifiable(
      profile.referencedBy,
    );
    if (capturedReferencedBy.isNotEmpty && !confirmed) {
      final approve = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          key: const Key('custom-api-remove-confirm-dialog'),
          title: Text('Remove $capturedName ($capturedProfileId)?'),
          content: Text(
            'This custom provider is currently referenced by: ${capturedReferencedBy.join(', ')}.\n\n'
            'Removing it retains those role bindings as unavailable (needs reselection) in ${_customCatalog.envFileName} without renumbering remaining custom provider IDs.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              key: const Key('confirm-remove-custom-api-button'),
              onPressed: () => Navigator.of(dialogContext).pop(true),
              child: const Text('Remove anyway'),
            ),
          ],
        ),
      );
      if (approve != true || !mounted) return;
      if (!_current(api, generation) || !_enabled || _customBusy) return;
      final currentProfile = _customCatalog.profiles
          .where((p) => p.profileId == capturedProfileId)
          .firstOrNull;
      if (currentProfile == null ||
          _customCatalog.revision != capturedRevision ||
          _customCatalog.stateSha256 != capturedStateSha256 ||
          currentProfile.name != capturedName ||
          currentProfile.baseUrl != capturedBaseUrl ||
          currentProfile.selectedModel != capturedSelectedModel) {
        setState(() {
          _error =
              'Custom provider state changed while confirmation was open. Refresh and review before removing $capturedName.';
        });
        return;
      }
      confirmed = true;
    }
    final seq = ++_refreshSeq;
    setState(() => _customBusy = true);
    try {
      final snap = await api.changeCustomProvider({
        'action': 'remove_custom_provider',
        'profile_id': capturedProfileId,
        'confirm_referenced': confirmed,
        if (capturedRevision > 0) 'expected_revision': capturedRevision,
      });
      if (_current(api, generation) && seq == _refreshSeq) {
        setState(() {
          _customCatalog = snap;
          _notice =
              'Removed $capturedName ($capturedProfileId). Remaining profile IDs were not renumbered.';
        });
      }
    } catch (_) {
      if (_current(api, generation) && seq == _refreshSeq) {
        setState(() {
          _error = 'Could not remove custom provider $capturedName.';
        });
      }
    } finally {
      if (_current(api, generation) && seq == _refreshSeq) {
        setState(() => _customBusy = false);
      }
    }
  }

  Widget _customApiEditor() {
    final editing = _editingCustom;
    final discovered = editing?.discoveredModels ?? const <String>[];
    final currentModel = _customModel.text.trim();
    return _panel([
      _title(
        editing == null
            ? 'Add Custom OpenAI-Compatible API (.env)'
            : 'Edit ${editing.name} (${editing.profileId})',
      ),
      Text(
        'Provider Type: Custom OpenAI-compatible API (.env) · Persisted in user-local ${_customCatalog.envFileName} with 0600 permissions. '
        'Unlimited custom OpenAI-compatible endpoints are supported without renumbering profile IDs.',
      ),
      _field(
        _customName,
        'Custom Provider Name',
        maxLength: 96,
        help:
            'Display name (e.g. OpenRouter, Together AI, Groq, Local llama.cpp, LM Studio).',
      ),
      _field(
        _customBaseUrl,
        'OpenAI-Compatible Base URL',
        maxLength: 512,
        help:
            'Normalized automatically so GET {base_url}/models never duplicates /v1 or /models.',
      ),
      _field(
        _customChatPath,
        'Chat Completions Endpoint Path',
        maxLength: 160,
        help: 'Default: /chat/completions (appended to Base URL).',
      ),
      _field(
        _customModelsPath,
        'Models Discovery Endpoint Path',
        maxLength: 160,
        help: 'Default: /models (used by Discover / Refresh Models).',
      ),
      _field(
        _customEndpoints,
        'Custom / Engine-Specific Endpoints · optional',
        maxLength: 256,
        help:
            'Optional extra routes supported by this inference engine — e.g. /v1/systemone for llama.cpp; leave blank for LM Studio.',
      ),
      _field(
        _customMaxOutputTokens,
        'Maximum Output Tokens · optional (blank = up to model)',
        maxLength: 7,
        numeric: true,
        help:
            'Leave blank so the model uses its own native maximum output tokens. LLMs vary widely in max output tokens.',
      ),
      _field(
        _customApiKey,
        'API Key (.env)',
        maxLength: 512,
        secret: true,
        help: editing != null && editing.hasApiKey
            ? 'Stored key: ${editing.maskedApiKey} — leave blank to preserve existing key, or enter a new key to rotate.'
            : 'Stored in KINAIYA_PROVIDER_<ID>_API_KEY (0600). Leave blank for unauthenticated local servers.',
      ),
      if (editing != null && editing.hasApiKey)
        SwitchListTile(
          contentPadding: EdgeInsets.zero,
          value: _customClearKey,
          title: const Text('Clear stored API key on save'),
          onChanged: _enabled && !_customBusy
              ? (val) => setState(() => _customClearKey = val)
              : null,
        ),
      if (discovered.isNotEmpty)
        DropdownButtonFormField<String>(
          key: ValueKey('custom-discovered-model-$currentModel'),
          initialValue: discovered.contains(currentModel) ? currentModel : null,
          decoration: const InputDecoration(
            labelText: 'Discovered /models (unverified capabilities)',
            border: OutlineInputBorder(),
          ),
          items: [
            for (final mid in discovered)
              DropdownMenuItem(value: mid, child: Text('$mid · unverified')),
          ],
          onChanged: _enabled && !_customBusy
              ? (val) {
                  if (val != null) {
                    setState(() => _customModel.text = val);
                  }
                }
              : null,
        ),
      _field(
        _customModel,
        'Manual or Selected Model ID',
        maxLength: 160,
        help:
            'Qualified as (profile_id, model_id) so identical model IDs across providers never collide. Works even when GET /models is unsupported.',
      ),
      if (editing != null)
        Align(
          alignment: Alignment.centerLeft,
          child: OutlinedButton.icon(
            key: Key('editor-discover-models-${editing.profileId}'),
            onPressed: _enabled && !_customBusy
                ? () => unawaited(_discoverCustomModels(editing))
                : null,
            icon: const Icon(Icons.travel_explore),
            label: const Text('Discover / Refresh Models (GET /models)'),
          ),
        ),
      if (_customDiscoveryBanner != null)
        Text(
          _customDiscoveryBanner!,
          key: const Key('custom-discovery-banner'),
        ),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        value: _customEnabled,
        title: const Text('Enable this custom provider profile'),
        onChanged: _enabled && !_customBusy
            ? (val) => setState(() => _customEnabled = val)
            : null,
      ),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        value: _customAllowRemoteHttp,
        title: const Text('Allow remote HTTP (non-loopback without TLS)'),
        subtitle: const Text(
          'Remote endpoints require HTTPS by default; enable only for trusted LAN gateways.',
        ),
        onChanged: _enabled && !_customBusy
            ? (val) => setState(() => _customAllowRemoteHttp = val)
            : null,
      ),
      const Text(
        'Reasoning effort is configured globally or per model in Models (not per provider). '
        'Capability reporting: chat, streaming, reasoning, multimodal, tool-use, and Agent qualification are reported as unverified for custom /models endpoints.',
      ),
      if (_formError != null) Text(_formError!),
      Wrap(
        spacing: 12,
        runSpacing: 8,
        children: [
          FilledButton(
            key: const Key('save-custom-api-button'),
            onPressed: _enabled && !_customBusy
                ? () => unawaited(_saveCustomApi())
                : null,
            child: const Text('Save Custom API'),
          ),
          OutlinedButton(
            onPressed: !_customBusy
                ? () => setState(() {
                    _customApiKey.clear();
                    _editingCustom = null;
                    _screen = 'list';
                    _formError = null;
                  })
                : null,
            child: const Text('Cancel'),
          ),
        ],
      ),
    ]);
  }

  Widget _catalogEntry(String title, String subtitle, List<Widget> actions) =>
      LayoutBuilder(
        builder: (context, constraints) {
          final details = Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title, style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 6),
              SelectableText(subtitle),
            ],
          );
          final controls = Wrap(spacing: 4, runSpacing: 4, children: actions);
          return Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: constraints.maxWidth < 650
                ? Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [details, const SizedBox(height: 8), controls],
                  )
                : Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(child: details),
                      const SizedBox(width: 16),
                      controls,
                    ],
                  ),
          );
        },
      );

  Widget _catalogList() => _panel([
    Row(
      children: [
        Expanded(
          child: _title(
            _tab == 'profiles'
                ? 'Provider profiles'
                : _tab == 'credentials'
                ? 'Credential references'
                : 'Custom OpenAI-Compatible APIs (.env)',
          ),
        ),
        FilledButton.icon(
          key: _tab == 'custom_apis'
              ? const Key('add-custom-api-button')
              : null,
          onPressed: _enabled
              ? () => _tab == 'profiles'
                    ? _profileForm()
                    : _tab == 'credentials'
                    ? _credentialForm()
                    : _customApiForm()
              : null,
          icon: const Icon(Icons.add),
          label: Text(
            _tab == 'profiles'
                ? 'Add profile'
                : _tab == 'credentials'
                ? 'Register key'
                : 'Add Custom API',
          ),
        ),
      ],
    ),
    if (_tab == 'custom_apis') ...[
      Text(
        'Unlimited user-local profiles in ${_customCatalog.envFileName} (0600) · '
        '${_customCatalog.profiles.length} configured · Capabilities reported as unverified',
      ),
      Row(
        children: [
          Expanded(
            child: DropdownButtonFormField<String>(
              key: ValueKey(
                'custom-global-reasoning-effort-${_customCatalog.globalReasoningEffort}',
              ),
              initialValue:
                  const [
                    'none',
                    'minimal',
                    'low',
                    'medium',
                    'high',
                  ].contains(_customCatalog.globalReasoningEffort)
                  ? _customCatalog.globalReasoningEffort
                  : 'medium',
              decoration: const InputDecoration(
                labelText: 'Global Reasoning Effort (default across models)',
                helperText:
                    'Applies globally unless overridden per model when binding a role.',
                border: OutlineInputBorder(),
              ),
              items: const [
                DropdownMenuItem(value: 'none', child: Text('none')),
                DropdownMenuItem(value: 'minimal', child: Text('minimal')),
                DropdownMenuItem(value: 'low', child: Text('low')),
                DropdownMenuItem(
                  value: 'medium',
                  child: Text('medium (Global default)'),
                ),
                DropdownMenuItem(value: 'high', child: Text('high')),
              ],
              onChanged: _enabled && !_customBusy
                  ? (val) {
                      if (val != null) {
                        unawaited(_setGlobalReasoningEffort(val));
                      }
                    }
                  : null,
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: DropdownButtonFormField<String>(
              key: ValueKey(
                'custom-per-model-reasoning-override-${_customRoleReasoningOverride ?? 'global'}',
              ),
              initialValue: _customRoleReasoningOverride ?? 'global',
              decoration: const InputDecoration(
                labelText: 'Per-Model Reasoning Override (for role binding)',
                helperText:
                    'Optional per-model reasoning effort when clicking Use for Converse / Agent.',
                border: OutlineInputBorder(),
              ),
              items: const [
                DropdownMenuItem(
                  value: 'global',
                  child: Text('Inherit global reasoning effort'),
                ),
                DropdownMenuItem(value: 'none', child: Text('none')),
                DropdownMenuItem(value: 'minimal', child: Text('minimal')),
                DropdownMenuItem(value: 'low', child: Text('low')),
                DropdownMenuItem(value: 'medium', child: Text('medium')),
                DropdownMenuItem(value: 'high', child: Text('high')),
              ],
              onChanged: _enabled && !_customBusy
                  ? (val) {
                      setState(() {
                        _customRoleReasoningOverride =
                            (val == null || val == 'global') ? null : val;
                      });
                    }
                  : null,
            ),
          ),
        ],
      ),
      if (_customDiscoveryBanner != null)
        Text(
          _customDiscoveryBanner!,
          key: const Key('custom-discovery-banner'),
        ),
      if (_customCatalog.roleBindings.values.any((b) => b != null))
        SelectableText(
          'Proposed custom role selections (pending native role-runtime admission · not active in live runtime): '
          '${_customCatalog.roleBindings.entries.where((e) => e.value != null).map((e) => '${e.key} → ${e.value!.providerName} (${e.value!.qualifiedModelId} · status: ${e.value!.status}${e.value!.unavailableReason != null ? ' [${e.value!.unavailableReason}]' : ''} · reasoning: ${e.value!.effectiveReasoningEffort})').join(' · ')}',
          key: const Key('custom-role-bindings-summary'),
        ),
      if (_customCatalog.profiles.isEmpty)
        const Text(
          'No custom .env APIs saved yet. Click "Add Custom API" to configure OpenRouter, Together, Groq, vLLM, Ollama, llama.cpp, or any OpenAI-compatible endpoint.',
        ),
      for (final custom in _customCatalog.profiles)
        _catalogEntry(
          '${custom.name} (${custom.profileId})',
          '${custom.baseUrl} · ${custom.enabled ? 'Enabled' : 'Disabled'} · '
              '${custom.hasApiKey ? 'API key ${custom.maskedApiKey}' : 'No API key'}\n'
              'Endpoints: chat ${custom.chatCompletionsPath} · models ${custom.modelsPath}'
              '${custom.customEndpoints.isEmpty ? '' : ' · custom ${custom.customEndpoints}'}\n'
              'Max output tokens: ${custom.maxOutputTokens == null ? 'Up to model (blank)' : '${custom.maxOutputTokens}'} · '
              'Model: ${custom.selectedModel.isEmpty ? 'None selected' : '${custom.qualifiedModelId} (unverified)'}'
              '${custom.discoveredModels.isEmpty ? '' : ' · ${custom.discoveredModels.length} discovered via /models'}'
              '${custom.referencedBy.isEmpty ? '' : '\nReferenced by: ${custom.referencedBy.join(', ')}'}',
          [
            Switch(
              key: Key('custom-api-enabled-${custom.profileId}'),
              value: custom.enabled,
              onChanged: _enabled && !_customBusy
                  ? (val) => unawaited(_toggleCustomEnabled(custom, val))
                  : null,
            ),
            TextButton(
              key: Key('custom-api-discover-${custom.profileId}'),
              onPressed: _enabled && !_customBusy
                  ? () => unawaited(_discoverCustomModels(custom))
                  : null,
              child: const Text('Discover / Refresh Models'),
            ),
            if (custom.enabled &&
                (custom.selectedModel.isNotEmpty ||
                    custom.discoveredModels.isNotEmpty)) ...[
              TextButton(
                key: Key('custom-api-bind-converse-${custom.profileId}'),
                onPressed: _enabled && !_customBusy
                    ? () => unawaited(_bindCustomRole(custom, 'converse'))
                    : null,
                child: const Text('Use for Converse'),
              ),
              TextButton(
                key: Key('custom-api-bind-agent-${custom.profileId}'),
                onPressed: _enabled && !_customBusy
                    ? () => unawaited(_bindCustomRole(custom, 'agent'))
                    : null,
                child: const Text('Use for Agent'),
              ),
            ],
            TextButton(
              key: Key('custom-api-edit-${custom.profileId}'),
              onPressed: _enabled && !_customBusy
                  ? () => _customApiForm(custom)
                  : null,
              child: const Text('Edit'),
            ),
            TextButton(
              key: Key('custom-api-remove-${custom.profileId}'),
              onPressed: _enabled && !_customBusy
                  ? () => unawaited(_removeCustomProfile(custom))
                  : null,
              child: const Text('Remove'),
            ),
          ],
        ),
    ],
    if (_tab == 'profiles' && _catalog!.profiles.isEmpty)
      const Text(
        'No saved profiles yet. Add a provider to save its connection settings.',
      ),
    if (_tab == 'profiles')
      for (final profile in _catalog!.profiles)
        _catalogEntry(
          profile.label,
          '${profile.kind} · ${profile.model}\nGeneration ${profile.generation} · ${profile.removed ? 'Removed from new selections' : 'Stored configuration'} · Unqualified / readiness unknown${profile.credentialAvailable ? '' : '\nCredential generation unavailable'}',
          [
            TextButton(
              onPressed: _enabled ? () => _profileForm(profile) : null,
              child: Text(profile.removed ? 'Restore / edit' : 'Edit'),
            ),
            if (!profile.removed)
              TextButton(
                onPressed: _enabled
                    ? () => _begin({
                        'operation': 'profile_remove',
                        'profile_id': profile.id,
                      })
                    : null,
                child: const Text('Remove'),
              ),
          ],
        ),
    if (_tab == 'credentials' && _catalog!.credentials.isEmpty)
      const Text(
        'No keys are registered in this catalog. Register an existing known host reference or import a provider API key.',
      ),
    if (_tab == 'credentials')
      for (final credential in _catalog!.credentials)
        _catalogEntry(
          credential.id,
          '${credential.source}${credential.reference == null ? '' : ' · ${credential.reference}'}\n${credential.removed ? 'Removed from new use' : 'Protected key stored'} · Provider readiness unknown\nGeneration ${credential.generation}',
          [
            TextButton(
              onPressed: _enabled ? () => _credentialForm(credential) : null,
              child: Text(credential.removed ? 'Register again' : 'Rotate'),
            ),
            if (!credential.removed)
              TextButton(
                onPressed: _enabled
                    ? () => _begin({
                        'operation': 'credential_remove',
                        'credential_id': credential.id,
                      })
                    : null,
                child: const Text('Remove'),
              ),
          ],
        ),
    const Text(
      'Removal prevents new selections. Saved versions and existing task connections remain. Revoke a compromised key with its provider.',
    ),
  ]);

  @override
  Widget build(BuildContext context) {
    final operation = _operation;
    return Material(
      type: MaterialType.transparency,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_screen == 'list')
            _panel([
              const Text(
                'Save provider connections and credentials on this computer. Select supported text profiles in Models or Harness Customization, and speech profiles in Audio. Catalog declarations do not grant disclosure or establish readiness.',
              ),
              Wrap(
                spacing: 12,
                runSpacing: 8,
                children: [
                  SegmentedButton<String>(
                    segments: const [
                      ButtonSegment(value: 'profiles', label: Text('Profiles')),
                      ButtonSegment(
                        value: 'credentials',
                        label: Text('Credentials'),
                      ),
                      ButtonSegment(
                        value: 'custom_apis',
                        label: Text('Custom APIs (.env)'),
                      ),
                    ],
                    selected: {_tab},
                    onSelectionChanged: operation == null
                        ? (values) {
                            final nextTab = values.single;
                            setState(() {
                              _tab = nextTab;
                              _screen = 'list';
                              _key.clear();
                              _customApiKey.clear();
                              _formError = null;
                            });
                            if (nextTab == 'custom_apis') {
                              unawaited(_refreshCustomProviders());
                            }
                          }
                        : null,
                  ),
                  OutlinedButton.icon(
                    onPressed:
                        widget.api != null && widget.active() && !_reading
                        ? () {
                            if (_tab == 'custom_apis') {
                              unawaited(_refreshCustomProviders());
                            } else {
                              unawaited(_refresh());
                            }
                          }
                        : null,
                    icon: const Icon(Icons.refresh),
                    label: const Text('Refresh'),
                  ),
                ],
              ),
              if (_catalog != null)
                Text(
                  'Saved revision ${_catalog!.revision} · Active models unchanged',
                ),
            ]),
          if (_screen == 'list') const SizedBox(height: 16),
          if (operation != null) ...[
            _pendingPanel(operation),
            const SizedBox(height: 16),
          ],
          if (_notice != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 16),
              child: Text(_notice!),
            ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 16),
              child: Text(_error!),
            ),
          if (widget.api == null || !widget.active())
            const Text(
              'Connect to an owner host to manage its provider catalog.',
            )
          else if (_catalog == null)
            Text(
              _reading
                  ? 'Reading provider catalog…'
                  : 'Provider management is unavailable on this host.',
            )
          else ...[
            if (_pending.length >= 16 && operation == null)
              const Text(
                'The app retains its maximum unresolved endpoint changes. Reconcile those requests before saving another endpoint.',
              ),
            if (_screen == 'profile')
              _profileEditor()
            else if (_screen == 'credential')
              _credentialEditor()
            else if (_screen == 'custom_api')
              _customApiEditor()
            else
              _catalogList(),
            if (_screen == 'list' && _formError != null)
              Padding(
                padding: const EdgeInsets.only(top: 16),
                child: Text(_formError!),
              ),
          ],
        ],
      ),
    );
  }
}
