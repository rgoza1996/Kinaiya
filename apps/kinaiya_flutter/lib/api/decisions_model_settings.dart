/// Independent classifier declarations. Metadata never qualifies a model.
library;

import 'dart:convert';

import 'tool_permissions.dart' show permissionJsonDecode;

const decisionsModelRevisionLimit = 9007199254740990;
const decisionsModelBoundary = 'settled_decisions_operations';
Never _bad() => throw const FormatException('Invalid Decisions model metadata');
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
String decisionsModelKey(Object? value) =>
    _match(value, r'[A-Za-z0-9:._-]{1,128}');
int _integer(Object? value, int min, [int max = decisionsModelRevisionLimit]) {
  if (value is! int || value < min || value > max) _bad();
  return value;
}

bool _boolean(Object? value) {
  if (value is! bool) _bad();
  return value;
}

String _text(Object? value, int bytes) {
  if (value is! String ||
      value.isEmpty ||
      utf8.encode(value).length > bytes ||
      value.codeUnits.any((c) => c < 32 || c == 127)) {
    _bad();
  }
  // Reject unpaired surrogates rather than silently replacing wire identity.
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

String? _optionalText(Object? value, int bytes) =>
    value == null ? null : _text(value, bytes);

class DecisionsCatalogBinding {
  DecisionsCatalogBinding.fromJson(Object? value) {
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

class DecisionsModelSelection {
  DecisionsModelSelection.fromJson(Object? value) {
    final raw = _object(value, {'source', 'catalog_binding'});
    if (!{'host', 'catalog'}.contains(raw['source'])) _bad();
    source = raw['source'] as String;
    catalog = raw['catalog_binding'] == null
        ? null
        : DecisionsCatalogBinding.fromJson(raw['catalog_binding']);
    if ((source == 'host') != (catalog == null)) _bad();
  }
  factory DecisionsModelSelection.host() => DecisionsModelSelection.fromJson({
    'source': 'host',
    'catalog_binding': null,
  });
  factory DecisionsModelSelection.catalog(DecisionsCatalogBinding binding) =>
      DecisionsModelSelection.fromJson({
        'source': 'catalog',
        'catalog_binding': binding.body,
      });
  late final String source;
  late final DecisionsCatalogBinding? catalog;
  String get identity => catalog?.identity ?? 'host';
  bool matches(DecisionsModelSelection other) => identity == other.identity;
  Map<String, Object?> get body =>
      Map.unmodifiable({'source': source, 'catalog_binding': catalog?.body});
}

class DecisionsModelProvider {
  DecisionsModelProvider.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'provider',
      'model',
      'local',
      'streaming',
    });
    if (raw['schema_version'] != '1.0') _bad();
    provider = _text(raw['provider'], 256);
    model = _text(raw['model'], 256);
    local = _boolean(raw['local']);
    streaming = _boolean(raw['streaming']);
    if (provider == 'native_d1') {
      _match(model, r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}');
      if (!local || streaming) _bad();
    }
  }
  late final String provider, model;
  late final bool local, streaming;
}

class DecisionsCatalogProfile {
  DecisionsCatalogProfile.fromJson(Object? value) {
    final raw = _object(value, {
      'id',
      'label',
      'catalog_binding',
      'selectable',
      'provider',
      'reason',
      'protocol',
      'qualification',
      'readiness',
    });
    id = _match(raw['id'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}');
    label = _text(raw['label'], 128);
    binding = DecisionsCatalogBinding.fromJson(raw['catalog_binding']);
    selectable = _boolean(raw['selectable']);
    provider = raw['provider'] == null
        ? null
        : DecisionsModelProvider.fromJson(raw['provider']);
    reason = _text(raw['reason'], 256);
    protocol = raw['protocol'] as String?;
    if (binding.id != id ||
        raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown' ||
        (selectable ? !_typedPair(provider, protocol) : protocol != null)) {
      _bad();
    }
  }
  late final String id, label, reason;
  late final String? protocol;
  late final DecisionsCatalogBinding binding;
  late final DecisionsModelProvider? provider;
  late final bool selectable;
}

bool _typedPair(DecisionsModelProvider? provider, String? protocol) =>
    provider?.provider == 'laya' && protocol == 'laya_systemone' ||
    provider?.provider == 'native_d1' &&
        protocol == 'native_d1_systemone' &&
        provider!.local &&
        !provider.streaming;

class DecisionsModelsSnapshot {
  DecisionsModelsSnapshot.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'revision',
      'state_token',
      'selection',
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
        raw['change_boundary'] != decisionsModelBoundary ||
        raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown') {
      _bad();
    }
    backendId = _authority(raw['backend_id']);
    instanceId = _authority(raw['instance_id']);
    revision = _integer(raw['revision'], 0);
    stateToken = _hash(raw['state_token']);
    selection = DecisionsModelSelection.fromJson(raw['selection']);
    selectedInfo = raw['selected_info'] == null
        ? null
        : DecisionsModelProvider.fromJson(raw['selected_info']);
    selectedError = _optionalText(raw['selected_error'], 256);
    final host = _object(raw['host'], {'provider', 'selectable', 'protocol'});
    hostInfo = host['provider'] == null
        ? null
        : DecisionsModelProvider.fromJson(host['provider']);
    hostSelectable = _boolean(host['selectable']);
    hostProtocol = _text(host['protocol'], 32);
    final expectedHostProtocol = hostInfo?.provider == 'laya'
        ? 'laya_systemone'
        : hostInfo?.provider == 'native_d1'
        ? 'native_d1_systemone'
        : 'host_configured';
    if (hostProtocol != expectedHostProtocol ||
        hostInfo?.provider == 'native_d1' &&
            !_typedPair(hostInfo, hostProtocol) ||
        hostSelectable != (hostInfo != null)) {
      _bad();
    }
    final rows = raw['catalog_profiles'];
    if (rows is! List || rows.length > 32) _bad();
    profiles = List.unmodifiable(rows.map(DecisionsCatalogProfile.fromJson));
    if (profiles.map((p) => p.id).toSet().length != profiles.length) _bad();
    catalogError = _optionalText(raw['catalog_error'], 256);
    busy = _boolean(raw['busy']);
    final operations = _object(raw['operation_state'], {
      'preparing',
      'finalized',
      'windows',
      'health',
      'leases',
      'cleanup_pending',
      'cleanup_failed',
      'closed',
    });
    operationClosed = _boolean(operations['closed']);
    operationState = Map.unmodifiable({
      for (final key in operations.keys.where((k) => k != 'closed'))
        key: _integer(operations[key], 0, 100000),
    });
    if ((operationClosed || operationState.values.any((v) => v > 0)) && !busy) {
      _bad();
    }
    final limits = _object(raw['limits'], {'leases', 'receipts'});
    if (limits['leases'] != 64 ||
        limits['receipts'] != 10000 ||
        limits.values.any((v) => v is! int)) {
      _bad();
    }
  }
  late final String backendId, instanceId, stateToken, hostProtocol;
  late final int revision;
  late final DecisionsModelSelection selection;
  late final DecisionsModelProvider? selectedInfo, hostInfo;
  late final String? selectedError, catalogError;
  late final bool hostSelectable, busy, operationClosed;
  late final List<DecisionsCatalogProfile> profiles;
  late final Map<String, int> operationState;
  bool sameBase(DecisionsModelsSnapshot other) =>
      backendId == other.backendId &&
      instanceId == other.instanceId &&
      revision == other.revision &&
      stateToken == other.stateToken;
  bool permits(DecisionsModelSelection choice) => choice.source == 'host'
      ? hostSelectable
      : profiles.any(
          (p) => p.selectable && p.binding.identity == choice.catalog?.identity,
        );
  Map<String, Object?> change(DecisionsModelSelection choice, String key) =>
      decisionsModelChange({
        'schema_version': '1.0',
        'expected_backend': backendId,
        'expected_instance': instanceId,
        'expected_revision': revision,
        'expected_state': stateToken,
        'idempotency_key': key,
        'selection': choice.body,
      });
}

Map<String, Object?> decisionsModelChange(Object? value) {
  final raw = _object(value, {
    'schema_version',
    'expected_backend',
    'expected_instance',
    'expected_revision',
    'expected_state',
    'idempotency_key',
    'selection',
  });
  if (raw['schema_version'] != '1.0') _bad();
  return Map.unmodifiable({
    'schema_version': '1.0',
    'expected_backend': _authority(raw['expected_backend']),
    'expected_instance': _authority(raw['expected_instance']),
    'expected_revision': _integer(
      raw['expected_revision'],
      0,
      decisionsModelRevisionLimit - 1,
    ),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': decisionsModelKey(raw['idempotency_key']),
    'selection': DecisionsModelSelection.fromJson(raw['selection']).body,
  });
}

class DecisionsModelReceipt {
  DecisionsModelReceipt.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'status',
      'role',
      'backend_id',
      'instance_id',
      'idempotency_key',
      'expected_revision',
      'expected_state',
      'selection',
      'revision',
      'change_boundary',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['status'] != 'model_selection_saved' ||
        raw['role'] != 'decisions' ||
        raw['change_boundary'] != decisionsModelBoundary) {
      _bad();
    }
    backendId = _authority(raw['backend_id']);
    instanceId = _authority(raw['instance_id']);
    key = decisionsModelKey(raw['idempotency_key']);
    expectedRevision = _integer(
      raw['expected_revision'],
      0,
      decisionsModelRevisionLimit - 1,
    );
    expectedState = _hash(raw['expected_state']);
    revision = _integer(raw['revision'], 1);
    if (revision != expectedRevision + 1) _bad();
    selection = DecisionsModelSelection.fromJson(raw['selection']);
  }
  late final String backendId, instanceId, key, expectedState;
  late final int expectedRevision, revision;
  late final DecisionsModelSelection selection;
  bool matches(Map<String, Object?> request) =>
      request['schema_version'] == '1.0' &&
      request['expected_backend'] == backendId &&
      request['expected_instance'] == instanceId &&
      request['idempotency_key'] == key &&
      request['expected_revision'] == expectedRevision &&
      request['expected_state'] == expectedState &&
      selection.matches(DecisionsModelSelection.fromJson(request['selection']));
}

class DecisionsModelLookup {
  DecisionsModelLookup.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'backend_id',
      'request',
      'status',
      'receipt',
      'mutation_fenced',
    });
    if (raw['schema_version'] != '1.0' ||
        !{'recorded', 'unobserved'}.contains(raw['status'])) {
      _bad();
    }
    backendId = _authority(raw['backend_id']);
    request = decisionsModelChange(raw['request']);
    receipt = raw['receipt'] == null
        ? null
        : DecisionsModelReceipt.fromJson(raw['receipt']);
    fenced = _boolean(raw['mutation_fenced']);
    if (backendId != request['expected_backend'] ||
        (raw['status'] == 'recorded') != (receipt != null) ||
        (receipt != null && (!receipt!.matches(request) || !fenced))) {
      _bad();
    }
  }
  late final String backendId;
  late final Map<String, Object?> request;
  late final DecisionsModelReceipt? receipt;
  late final bool fenced;
  bool matches(Map<String, Object?> original) =>
      jsonEncode(request) == jsonEncode(decisionsModelChange(original));
}

Object? decisionsModelJsonDecode(List<int> bytes) =>
    permissionJsonDecode(bytes);
