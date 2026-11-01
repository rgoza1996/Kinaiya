/// Strict native text-to-speech selections; metadata is not qualification.
library;

import 'dart:convert';
import 'tool_permissions.dart' show permissionJsonDecode;

const ttsModelRevisionLimit = 9007199254740990;
const ttsModelBoundary = 'settled_converse_operations';
const ttsOpenRouterModel = 'hexgrad/kokoro-82m';
const ttsOpenRouterProfile = 'Kokoro 82M (OpenRouter)';
const ttsOpenRouterVoice = 'af_heart';
Never _bad() => throw const FormatException('Invalid TTS model metadata');
Map<String, Object?> _object(Object? value, Set<String> keys) {
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !keys.containsAll(value.keys)) {
    _bad();
  }
  return value;
}

String _match(Object? value, String pattern) {
  if (value is! String ||
      RegExp(pattern).matchAsPrefix(value)?.end != value.length) {
    _bad();
  }
  return value;
}

String _hash(Object? value) => _match(value, r'[a-f0-9]{64}');
String _authority(Object? value) => _match(value, r'[a-f0-9]{32}');
String ttsModelKey(Object? value) => _match(value, r'[A-Za-z0-9:._-]{1,128}');
int _integer(Object? value, int min, [int max = ttsModelRevisionLimit]) {
  if (value is! int || value < min || value > max) {
    _bad();
  }
  return value;
}

bool _boolean(Object? value) {
  if (value is! bool) {
    _bad();
  }
  return value;
}

String _text(Object? value, int bytes) {
  if (value is! String ||
      value.isEmpty ||
      utf8.encode(value).length > bytes ||
      value.codeUnits.any((c) => c < 32 || c == 127)) {
    _bad();
  }
  for (var i = 0; i < value.length; i++) {
    final c = value.codeUnitAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      if (++i >= value.length ||
          value.codeUnitAt(i) < 0xdc00 ||
          value.codeUnitAt(i) > 0xdfff) {
        _bad();
      }
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      _bad();
    }
  }
  return value;
}

String? _optionalText(Object? value) =>
    value == null ? null : _text(value, 256);

String? _displayText(Object? value) {
  if (value == null) return null;
  final text = _text(value, 256);
  if (text.startsWith('/') ||
      text.startsWith('.') ||
      text.contains(r'\') ||
      text.contains('://') ||
      RegExp(r'^[A-Za-z]:/').hasMatch(text) ||
      text.split('/').contains('..') ||
      text.runes.any((c) => c >= 127 && c <= 159)) {
    _bad();
  }
  return text;
}

class TtsCatalogBinding {
  TtsCatalogBinding.fromJson(Object? value) {
    final raw = _object(value, {'id', 'generation', 'configuration_sha256'});
    id = _match(raw['id'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}');
    generation = _integer(raw['generation'], 1, 64);
    configurationSha256 = _hash(raw['configuration_sha256']);
  }
  late final String id, configurationSha256;
  late final int generation;
  String get identity => '$id:$generation:$configurationSha256';
  Map<String, Object?> get body => Map.unmodifiable({
    'id': id,
    'generation': generation,
    'configuration_sha256': configurationSha256,
  });
}

class TtsModelSelection {
  TtsModelSelection.fromJson(Object? value) {
    final raw = _object(value, {'source', 'catalog_binding'});
    if (!{'host', 'catalog'}.contains(raw['source'])) {
      _bad();
    }
    source = raw['source'] as String;
    catalog = raw['catalog_binding'] == null
        ? null
        : TtsCatalogBinding.fromJson(raw['catalog_binding']);
    if ((source == 'host') != (catalog == null)) {
      _bad();
    }
  }
  factory TtsModelSelection.host() =>
      TtsModelSelection.fromJson({'source': 'host', 'catalog_binding': null});
  factory TtsModelSelection.catalog(TtsCatalogBinding binding) =>
      TtsModelSelection.fromJson({
        'source': 'catalog',
        'catalog_binding': binding.body,
      });
  late final String source;
  late final TtsCatalogBinding? catalog;
  String get identity => catalog?.identity ?? 'host';
  bool matches(TtsModelSelection other) => identity == other.identity;
  Map<String, Object?> get body =>
      Map.unmodifiable({'source': source, 'catalog_binding': catalog?.body});
}

class TtsModelProvider {
  TtsModelProvider.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'provider',
      'model',
      'local',
      'streaming',
    });
    if (raw['schema_version'] != '1.0') {
      _bad();
    }
    provider = _text(raw['provider'], 256);
    model = _text(raw['model'], 256);
    local = _boolean(raw['local']);
    streaming = _boolean(raw['streaming']);
  }
  late final String provider, model;
  late final bool local, streaming;
  bool get isOpenRouterSpeech =>
      provider == 'openrouter' &&
      model == ttsOpenRouterModel &&
      !local &&
      streaming;
}

class TtsCatalogProfile {
  TtsCatalogProfile.fromJson(Object? value) {
    final raw = _object(value, {
      'id',
      'label',
      'catalog_binding',
      'selectable',
      'provider',
      'profile',
      'voice',
      'reason',
      'qualification',
      'readiness',
    });
    id = _match(raw['id'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}');
    label = _text(raw['label'], 128);
    binding = TtsCatalogBinding.fromJson(raw['catalog_binding']);
    selectable = _boolean(raw['selectable']);
    provider = TtsModelProvider.fromJson(raw['provider']);
    profile = _displayText(raw['profile']) ?? _bad();
    voice = _displayText(raw['voice']) ?? _bad();
    reason = _text(raw['reason'], 256);
    if (binding.id != id ||
        raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown' ||
        profile != ttsOpenRouterProfile ||
        voice != ttsOpenRouterVoice ||
        !provider.isOpenRouterSpeech) {
      _bad();
    }
  }
  late final String id, label, reason, profile, voice;
  late final TtsCatalogBinding binding;
  late final TtsModelProvider provider;
  late final bool selectable;
}

class TtsModelsSnapshot {
  TtsModelsSnapshot.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'role',
      'backend_id',
      'instance_id',
      'revision',
      'state_token',
      'selection',
      'selected_available',
      'selected_info',
      'selected_profile',
      'selected_voice',
      'selected_error',
      'host',
      'catalog_profiles',
      'catalog_error',
      'budget',
      'busy',
      'operation_state',
      'change_boundary',
      'qualification',
      'readiness',
      'limits',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['role'] != 'tts' ||
        raw['change_boundary'] != ttsModelBoundary ||
        raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown') {
      _bad();
    }
    backendId = _authority(raw['backend_id']);
    instanceId = _authority(raw['instance_id']);
    revision = _integer(raw['revision'], 0);
    stateToken = _hash(raw['state_token']);
    selection = TtsModelSelection.fromJson(raw['selection']);
    selectedAvailable = _boolean(raw['selected_available']);
    selectedInfo = raw['selected_info'] == null
        ? null
        : TtsModelProvider.fromJson(raw['selected_info']);
    selectedError = _optionalText(raw['selected_error']);
    selectedProfile = _displayText(raw['selected_profile']);
    selectedVoice = _displayText(raw['selected_voice']);
    if (selection.source == 'catalog' &&
        selectedAvailable &&
        (selectedInfo?.isOpenRouterSpeech != true ||
            selectedProfile != ttsOpenRouterProfile ||
            selectedVoice != ttsOpenRouterVoice)) {
      _bad();
    }
    final host = _object(raw['host'], {
      'provider',
      'selectable',
      'profile',
      'voice',
    });
    hostInfo = host['provider'] == null
        ? null
        : TtsModelProvider.fromJson(host['provider']);
    hostSelectable = _boolean(host['selectable']);
    hostProfile = _displayText(host['profile']);
    hostVoice = _displayText(host['voice']);
    final rows = raw['catalog_profiles'];
    if (rows is! List || rows.length > 32) {
      _bad();
    }
    profiles = List.unmodifiable(rows.map(TtsCatalogProfile.fromJson));
    if (profiles.map((p) => p.id).toSet().length != profiles.length) {
      _bad();
    }
    catalogError = _optionalText(raw['catalog_error']);
    final budget = _object(raw['budget'], {'configured', 'source', 'reason'});
    budgetConfigured = _boolean(budget['configured']);
    budgetReason = _text(budget['reason'], 256);
    if (budget['source'] !=
            (budgetConfigured ? 'host_configuration' : 'unavailable') ||
        (!budgetConfigured &&
            (profiles.any((profile) => profile.selectable) ||
                (selection.source == 'catalog' && selectedAvailable)))) {
      _bad();
    }
    busy = _boolean(raw['busy']);
    final operations = _object(raw['operation_state'], {
      'preparing',
      'text',
      'text_admitting',
      'windows',
      'speech',
      'stt',
      'stt_cleanup',
      'playback_pending',
      'memory_pending',
      'leases',
      'cleanup_pending',
      'cleanup_failed',
      'cleanup_unknown',
    });
    operationState = Map.unmodifiable({
      for (final key in operations.keys) key: _integer(operations[key], 0),
    });
    if (operationState.values.any((v) => v > 0) && !busy) {
      _bad();
    }
    final limits = _object(raw['limits'], {'leases', 'receipts'});
    if (limits['leases'] != 64 ||
        limits['receipts'] != 10000 ||
        limits.values.any((v) => v is! int)) {
      _bad();
    }
  }
  late final String backendId, instanceId, stateToken;
  late final int revision;
  late final TtsModelSelection selection;
  late final bool selectedAvailable, hostSelectable, busy, budgetConfigured;
  late final TtsModelProvider? selectedInfo, hostInfo;
  late final String? selectedError, catalogError;
  late final String? selectedProfile, selectedVoice, hostProfile, hostVoice;
  late final String budgetReason;
  late final List<TtsCatalogProfile> profiles;
  late final Map<String, int> operationState;
  bool sameBase(TtsModelsSnapshot other) =>
      backendId == other.backendId &&
      instanceId == other.instanceId &&
      revision == other.revision &&
      stateToken == other.stateToken;
  bool permits(TtsModelSelection choice) => choice.source == 'host'
      ? hostSelectable
      : profiles.any(
          (p) => p.selectable && p.binding.identity == choice.catalog?.identity,
        );
  Map<String, Object?> change(TtsModelSelection choice, String key) =>
      ttsModelChange({
        'schema_version': '1.0',
        'expected_backend': backendId,
        'expected_instance': instanceId,
        'expected_revision': revision,
        'expected_state': stateToken,
        'idempotency_key': key,
        'selection': choice.body,
      });
}

Map<String, Object?> ttsModelChange(Object? value) {
  final raw = _object(value, {
    'schema_version',
    'expected_backend',
    'expected_instance',
    'expected_revision',
    'expected_state',
    'idempotency_key',
    'selection',
  });
  if (raw['schema_version'] != '1.0') {
    _bad();
  }
  return Map.unmodifiable({
    'schema_version': '1.0',
    'expected_backend': _authority(raw['expected_backend']),
    'expected_instance': _authority(raw['expected_instance']),
    'expected_revision': _integer(
      raw['expected_revision'],
      0,
      ttsModelRevisionLimit - 1,
    ),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': ttsModelKey(raw['idempotency_key']),
    'selection': TtsModelSelection.fromJson(raw['selection']).body,
  });
}

class TtsModelReceipt {
  TtsModelReceipt.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'expected_backend',
      'expected_instance',
      'expected_revision',
      'expected_state',
      'idempotency_key',
      'selection',
      'status',
      'role',
      'revision',
      'change_boundary',
    });
    if (raw['status'] != 'tts_model_selection_saved' ||
        raw['role'] != 'tts' ||
        raw['change_boundary'] != ttsModelBoundary) {
      _bad();
    }
    request = ttsModelChange({
      for (final key in {
        'schema_version',
        'expected_backend',
        'expected_instance',
        'expected_revision',
        'expected_state',
        'idempotency_key',
        'selection',
      })
        key: raw[key],
    });
    revision = _integer(raw['revision'], 1);
    if (revision != (request['expected_revision'] as int) + 1) {
      _bad();
    }
  }
  late final Map<String, Object?> request;
  late final int revision;
  String get key => request['idempotency_key'] as String;
  bool matches(Map<String, Object?> body) =>
      jsonEncode(request) == jsonEncode(ttsModelChange(body));
}

class TtsModelLookup {
  TtsModelLookup.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'role',
      'status',
      'request',
      'backend_id',
      'instance_id',
      'current_revision',
      'mutation_fenced',
      'receipt',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['role'] != 'tts' ||
        !{'recorded', 'unobserved'}.contains(raw['status'])) {
      _bad();
    }
    request = ttsModelChange(raw['request']);
    backendId = _authority(raw['backend_id']);
    instanceId = _authority(raw['instance_id']);
    currentRevision = _integer(raw['current_revision'], 0);
    fenced = _boolean(raw['mutation_fenced']);
    receipt = raw['receipt'] == null
        ? null
        : TtsModelReceipt.fromJson(raw['receipt']);
    if (backendId != request['expected_backend'] ||
        (raw['status'] == 'recorded') != (receipt != null) ||
        fenced !=
            (instanceId != request['expected_instance'] ||
                currentRevision > (request['expected_revision'] as int)) ||
        (receipt != null &&
            (!receipt!.matches(request) ||
                currentRevision < receipt!.revision))) {
      _bad();
    }
  }
  late final String backendId, instanceId;
  late final int currentRevision;
  late final Map<String, Object?> request;
  late final bool fenced;
  late final TtsModelReceipt? receipt;
  bool matches(Map<String, Object?> body) =>
      jsonEncode(request) == jsonEncode(ttsModelChange(body));
}

Object? ttsModelJsonDecode(List<int> bytes) => permissionJsonDecode(bytes);
