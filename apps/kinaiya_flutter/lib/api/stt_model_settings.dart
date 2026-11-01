/// Strict native speech-to-text selections; metadata is not qualification.
library;

import 'dart:convert';
import 'tool_permissions.dart' show permissionJsonDecode;

const sttModelRevisionLimit = 9007199254740990;
const sttModelBoundary = 'settled_converse_operations';
const sttMetaModel = 'muse-voice-transcribe-1.0';
Never _bad() => throw const FormatException('Invalid STT model metadata');
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
String sttModelKey(Object? value) => _match(value, r'[A-Za-z0-9:._-]{1,128}');
int _integer(Object? value, int min, [int max = sttModelRevisionLimit]) {
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

class SttCatalogBinding {
  SttCatalogBinding.fromJson(Object? value) {
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

class SttModelSelection {
  SttModelSelection.fromJson(Object? value) {
    final raw = _object(value, {'source', 'catalog_binding'});
    if (!{'host', 'catalog'}.contains(raw['source'])) {
      _bad();
    }
    source = raw['source'] as String;
    catalog = raw['catalog_binding'] == null
        ? null
        : SttCatalogBinding.fromJson(raw['catalog_binding']);
    if ((source == 'host') != (catalog == null)) {
      _bad();
    }
  }
  factory SttModelSelection.host() =>
      SttModelSelection.fromJson({'source': 'host', 'catalog_binding': null});
  factory SttModelSelection.catalog(SttCatalogBinding binding) =>
      SttModelSelection.fromJson({
        'source': 'catalog',
        'catalog_binding': binding.body,
      });
  late final String source;
  late final SttCatalogBinding? catalog;
  String get identity => catalog?.identity ?? 'host';
  bool matches(SttModelSelection other) => identity == other.identity;
  Map<String, Object?> get body =>
      Map.unmodifiable({'source': source, 'catalog_binding': catalog?.body});
}

class SttModelProvider {
  SttModelProvider.fromJson(Object? value) {
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
  bool get isMetaTranscription =>
      provider == 'meta' && model == sttMetaModel && !local && !streaming;
}

class SttCatalogProfile {
  SttCatalogProfile.fromJson(Object? value) {
    final raw = _object(value, {
      'id',
      'label',
      'catalog_binding',
      'selectable',
      'provider',
      'reason',
      'qualification',
      'readiness',
    });
    id = _match(raw['id'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}');
    label = _text(raw['label'], 128);
    binding = SttCatalogBinding.fromJson(raw['catalog_binding']);
    selectable = _boolean(raw['selectable']);
    provider = SttModelProvider.fromJson(raw['provider']);
    reason = _text(raw['reason'], 256);
    if (binding.id != id ||
        raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown' ||
        (selectable && !provider.isMetaTranscription)) {
      _bad();
    }
  }
  late final String id, label, reason;
  late final SttCatalogBinding binding;
  late final SttModelProvider provider;
  late final bool selectable;
}

class SttModelsSnapshot {
  SttModelsSnapshot.fromJson(Object? value) {
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
      'selected_error',
      'host',
      'catalog_profiles',
      'catalog_error',
      'busy',
      'operation_state',
      'change_boundary',
      'qualification',
      'readiness',
      'limits',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['role'] != 'stt' ||
        raw['change_boundary'] != sttModelBoundary ||
        raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown') {
      _bad();
    }
    backendId = _authority(raw['backend_id']);
    instanceId = _authority(raw['instance_id']);
    revision = _integer(raw['revision'], 0);
    stateToken = _hash(raw['state_token']);
    selection = SttModelSelection.fromJson(raw['selection']);
    selectedAvailable = _boolean(raw['selected_available']);
    selectedInfo = raw['selected_info'] == null
        ? null
        : SttModelProvider.fromJson(raw['selected_info']);
    selectedError = _optionalText(raw['selected_error']);
    if (selection.source == 'catalog' &&
        selectedAvailable &&
        selectedInfo?.isMetaTranscription != true) {
      _bad();
    }
    final host = _object(raw['host'], {'provider', 'selectable'});
    hostInfo = host['provider'] == null
        ? null
        : SttModelProvider.fromJson(host['provider']);
    hostSelectable = _boolean(host['selectable']);
    final rows = raw['catalog_profiles'];
    if (rows is! List || rows.length > 32) {
      _bad();
    }
    profiles = List.unmodifiable(rows.map(SttCatalogProfile.fromJson));
    if (profiles.map((p) => p.id).toSet().length != profiles.length) {
      _bad();
    }
    catalogError = _optionalText(raw['catalog_error']);
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
  late final SttModelSelection selection;
  late final bool selectedAvailable, hostSelectable, busy;
  late final SttModelProvider? selectedInfo, hostInfo;
  late final String? selectedError, catalogError;
  late final List<SttCatalogProfile> profiles;
  late final Map<String, int> operationState;
  bool sameBase(SttModelsSnapshot other) =>
      backendId == other.backendId &&
      instanceId == other.instanceId &&
      revision == other.revision &&
      stateToken == other.stateToken;
  bool permits(SttModelSelection choice) => choice.source == 'host'
      ? hostSelectable
      : profiles.any(
          (p) => p.selectable && p.binding.identity == choice.catalog?.identity,
        );
  Map<String, Object?> change(SttModelSelection choice, String key) =>
      sttModelChange({
        'schema_version': '1.0',
        'expected_backend': backendId,
        'expected_instance': instanceId,
        'expected_revision': revision,
        'expected_state': stateToken,
        'idempotency_key': key,
        'selection': choice.body,
      });
}

Map<String, Object?> sttModelChange(Object? value) {
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
      sttModelRevisionLimit - 1,
    ),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': sttModelKey(raw['idempotency_key']),
    'selection': SttModelSelection.fromJson(raw['selection']).body,
  });
}

class SttModelReceipt {
  SttModelReceipt.fromJson(Object? value) {
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
    if (raw['status'] != 'stt_model_selection_saved' ||
        raw['role'] != 'stt' ||
        raw['change_boundary'] != sttModelBoundary) {
      _bad();
    }
    request = sttModelChange({
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
      jsonEncode(request) == jsonEncode(sttModelChange(body));
}

class SttModelLookup {
  SttModelLookup.fromJson(Object? value) {
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
        raw['role'] != 'stt' ||
        !{'recorded', 'unobserved'}.contains(raw['status'])) {
      _bad();
    }
    request = sttModelChange(raw['request']);
    backendId = _authority(raw['backend_id']);
    instanceId = _authority(raw['instance_id']);
    currentRevision = _integer(raw['current_revision'], 0);
    fenced = _boolean(raw['mutation_fenced']);
    receipt = raw['receipt'] == null
        ? null
        : SttModelReceipt.fromJson(raw['receipt']);
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
  late final SttModelReceipt? receipt;
  bool matches(Map<String, Object?> body) =>
      jsonEncode(request) == jsonEncode(sttModelChange(body));
}

Object? sttModelJsonDecode(List<int> bytes) => permissionJsonDecode(bytes);
