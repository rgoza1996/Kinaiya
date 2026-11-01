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
  });
  final ProviderApi? api;
  final bool Function() active;
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
  final _max = TextEditingController(), _context = TextEditingController();
  final _timeout = TextEditingController(), _maxLen = TextEditingController();
  final _headLen = TextEditingController(), _artifact = TextEditingController();
  final _credentialId = TextEditingController(), _key = TextEditingController();
  ProviderCatalogSnapshot? _catalog;
  ProviderProfile? _editing;
  ProviderCredential? _editingCredential;
  String _screen = 'list', _tab = 'profiles', _kind = 'compatible';
  String? _effort, _credential, _reference;
  bool _cloud = false, _import = true, _reading = false;
  String? _error, _notice, _formError;
  int _readVersion = 0;

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
    _observedPending = _operation;
    _pendingEvents.addListener(_pendingChanged);
    unawaited(_refresh());
  }

  @override
  void didUpdateWidget(covariant OptionsProviderEditor oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.api, widget.api)) {
      _readVersion++;
      _key.clear();
      _catalog = null;
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
    _pendingEvents.removeListener(_pendingChanged);
    _key.clear();
    for (final controller in [
      _id,
      _label,
      _endpoint,
      _model,
      _max,
      _context,
      _timeout,
      _maxLen,
      _headLen,
      _artifact,
      _credentialId,
      _key,
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
      _cloud = config?['allow_cloud'] == true;
      _effort = config?['reasoning_effort'] as String?;
      _max.text = '${config?['max_tokens'] ?? 2048}';
      _context.text = '${config?['context_tokens'] ?? ''}';
      _timeout.text = '${config?['timeout_s'] ?? 90}';
      _maxLen.text = '${config?['max_len'] ?? ''}';
      _headLen.text = '${config?['head_max_len'] ?? ''}';
      _artifact.text = config?['expected_artifact_sha256'] as String? ?? '';
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
      'allow_cloud': _cloud,
      if (_kind != 'lmstudio') 'credential': credential,
      if (_kind != 'laya' && _kind != 'meta_stt') ...{
        'max_tokens': int.tryParse(_max.text),
        'reasoning_effort': _effort,
      },
      if (_kind == 'meta' || _kind == 'compatible')
        'context_tokens': _context.text.isEmpty
            ? null
            : int.tryParse(_context.text),
      if (_kind != 'meta') 'timeout_s': int.tryParse(_timeout.text),
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
    keyboardType: numeric
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
          ],
          onChanged: _enabled
              ? (kind) {
                  if (kind != null) {
                    setState(() {
                      _kind = kind;
                      _cloud = kind == 'meta' || kind == 'meta_stt';
                      if (kind == 'meta_stt') {
                        _model.text = 'muse-voice-transcribe-1.0';
                      }
                      _effort = kind == 'meta'
                          ? 'xhigh'
                          : kind == 'lmstudio'
                          ? 'none'
                          : null;
                      _max.text = kind == 'lmstudio' ? '128' : '2048';
                      _timeout.text = kind == 'meta_stt'
                          ? '120'
                          : kind == 'laya'
                          ? '5'
                          : kind == 'lmstudio'
                          ? '60'
                          : '90';
                      _context.clear();
                      _maxLen.clear();
                      _headLen.clear();
                      _artifact.clear();
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
              : _kind == 'lmstudio'
              ? 'Explicit loopback native endpoint with a port, such as http://127.0.0.1:1234.'
              : 'No embedded credentials, queries or fragments. Remote origins require HTTPS.',
        ),
        _field(
          _model,
          'Exact model ID or native loaded alias',
          enabled: _kind != 'meta_stt',
          maxLength: 256,
          validator: (v) => _bounded(v, 256),
        ),
        if (_kind == 'meta_stt')
          const Text(
            'Buffered Meta speech-to-text only. Saving this declaration does not capture audio, grant cloud disclosure, test readiness, or select it for a role.',
          ),
        if (_kind == 'meta')
          const Text(
            'Meta is a cloud provider, including when reached through a local budget gateway. Contributor disclosure restrictions still apply.',
          ),
        if (_kind != 'lmstudio')
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: _cloud,
            title: const Text('Authorize this profile to use a cloud endpoint'),
            subtitle: const Text(
              'Input, transcript, tool and personality disclosure are checked independently for every request.',
            ),
            onChanged: _enabled && _kind != 'meta' && _kind != 'meta_stt'
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
                : (_kind == 'meta' || _kind == 'meta_stt') &&
                      (value == null || value.isEmpty)
                ? 'Meta requires a stored credential generation.'
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
        if (_kind != 'laya' && _kind != 'meta_stt') ...[
          _field(
            _max,
            'Maximum output tokens',
            maxLength: 5,
            numeric: true,
            validator: (v) =>
                _number(v, 16, _kind == 'lmstudio' ? 2048 : 65536),
          ),
          DropdownButtonFormField<String>(
            key: ValueKey('provider-effort-$_kind-$_effort'),
            initialValue: _effort ?? 'default',
            decoration: const InputDecoration(
              labelText: 'Configured reasoning effort',
              border: OutlineInputBorder(),
            ),
            items: [
              if (_kind == 'compatible')
                const DropdownMenuItem(
                  value: 'default',
                  child: Text('Provider default · unspecified'),
                ),
              for (final effort
                  in _kind == 'lmstudio' ? ['none'] : providerEfforts)
                DropdownMenuItem(value: effort, child: Text(effort)),
            ],
            onChanged: _enabled && _kind != 'lmstudio'
                ? (value) => setState(() {
                    _effort = value == 'default' ? null : value;
                  })
                : null,
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
              _kind == 'laya'
                  ? 120
                  : _kind == 'meta_stt'
                  ? 300
                  : 900,
            ),
          ),
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
            _tab == 'profiles' ? 'Provider profiles' : 'Credential references',
          ),
        ),
        FilledButton.icon(
          onPressed: _enabled
              ? () => _tab == 'profiles' ? _profileForm() : _credentialForm()
              : null,
          icon: const Icon(Icons.add),
          label: Text(_tab == 'profiles' ? 'Add profile' : 'Register key'),
        ),
      ],
    ),
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
                'Save provider connections and credentials on this computer. Select supported text profiles in Models or Harness Customization, and transcription profiles in Audio. Text to speech remains host configured.',
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
                    ],
                    selected: {_tab},
                    onSelectionChanged: operation == null
                        ? (values) => setState(() {
                            _tab = values.single;
                            _screen = 'list';
                            _key.clear();
                            _formError = null;
                          })
                        : null,
                  ),
                  OutlinedButton.icon(
                    onPressed:
                        widget.api != null && widget.active() && !_reading
                        ? _refresh
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
