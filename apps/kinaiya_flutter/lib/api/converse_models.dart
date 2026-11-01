/// Exact native Converse text choices. Metadata is not model qualification.
library;

import 'dart:convert';
import 'tool_permissions.dart' show permissionJsonDecode;

const converseModelRevisionLimit = 9007199254740990;
const converseModelBoundary = 'settled_converse_operations';
const converseReasoning = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'];

Never _bad() => throw const FormatException('Invalid Converse model metadata');
Map<String, Object?> _object(Object? value) {
  if (value is! Map<String, Object?>) _bad();
  return value;
}

void _keys(Map<String, Object?> value, Set<String> fields) {
  if (value.length != fields.length || !fields.containsAll(value.keys)) _bad();
}

String _text(Object? value, int bytes) {
  if (value is! String ||
      value.isEmpty ||
      utf8.encode(value).length > bytes ||
      value.codeUnits.any((c) => c < 32 || c == 127)) {
    _bad();
  }
  return value;
}

String _match(Object? value, String pattern) {
  if (value is! String) _bad();
  final match = RegExp(pattern).firstMatch(value);
  if (match == null || match.start != 0 || match.end != value.length) _bad();
  return value;
}

int _integer(Object? value, int min, int max) {
  if (value is! int || value < min || value > max) _bad();
  return value;
}

bool _boolean(Object? value) {
  if (value is! bool) _bad();
  return value;
}

String _hash(Object? value) => _match(value, r'[a-f0-9]{64}');
String converseModelKey(Object? value) =>
    _match(value, r'[A-Za-z0-9:._-]{1,128}');
String? _effort(Object? value) {
  if (value == null) return null;
  if (!converseReasoning.contains(value)) _bad();
  return value as String;
}

class ConverseCatalogBinding {
  ConverseCatalogBinding.fromJson(Object? value) {
    final raw = _object(value);
    _keys(raw, {'id', 'generation', 'configuration_sha256'});
    id = _match(raw['id'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}');
    generation = _integer(raw['generation'], 1, 64);
    configurationSha256 = _hash(raw['configuration_sha256']);
  }
  late final String id, configurationSha256;
  late final int generation;
  Map<String, Object?> get body => Map.unmodifiable({
    'id': id,
    'generation': generation,
    'configuration_sha256': configurationSha256,
  });
  String get identity => '$id:$generation:$configurationSha256';
  bool matches(ConverseCatalogBinding other) => identity == other.identity;
}

class ConverseModelSelection {
  ConverseModelSelection.fromJson(Object? value) {
    final raw = _object(value);
    _keys(raw, {'source', 'catalog_binding', 'reasoning'});
    if (!{'host', 'catalog'}.contains(raw['source'])) _bad();
    source = raw['source'] as String;
    catalog = raw['catalog_binding'] == null
        ? null
        : ConverseCatalogBinding.fromJson(raw['catalog_binding']);
    reasoning = _effort(raw['reasoning']);
    if (source == 'host'
        ? catalog != null || reasoning != null
        : catalog == null) {
      _bad();
    }
  }
  factory ConverseModelSelection.host() => ConverseModelSelection.fromJson({
    'source': 'host',
    'catalog_binding': null,
    'reasoning': null,
  });
  factory ConverseModelSelection.catalog(
    ConverseCatalogBinding catalog,
    String? reasoning,
  ) => ConverseModelSelection.fromJson({
    'source': 'catalog',
    'catalog_binding': catalog.body,
    'reasoning': reasoning,
  });
  late final String source;
  late final ConverseCatalogBinding? catalog;
  late final String? reasoning;
  Map<String, Object?> get body => Map.unmodifiable({
    'source': source,
    'catalog_binding': catalog?.body,
    'reasoning': reasoning,
  });
  String get targetIdentity => catalog?.identity ?? 'host';
  bool matches(ConverseModelSelection other) =>
      targetIdentity == other.targetIdentity && reasoning == other.reasoning;
}

class ConverseModelProvider {
  ConverseModelProvider.fromJson(Object? value) {
    final raw = _object(value);
    _keys(raw, {'schema_version', 'provider', 'model', 'local', 'streaming'});
    if (raw['schema_version'] != '1.0') _bad();
    provider = _text(raw['provider'], 256);
    model = _text(raw['model'], 256);
    local = _boolean(raw['local']);
    streaming = _boolean(raw['streaming']);
  }
  late final String provider, model;
  late final bool local, streaming;
  Map<String, Object?> get body => Map.unmodifiable({
    'schema_version': '1.0',
    'provider': provider,
    'model': model,
    'local': local,
    'streaming': streaming,
  });
}

class ConverseCatalogProfile {
  ConverseCatalogProfile.fromJson(Object? value) {
    final raw = _object(value);
    _keys(raw, {
      'id',
      'label',
      'catalog_binding',
      'selectable',
      'provider',
      'reasoning',
      'reasoning_options',
      'reason',
      'qualification',
      'readiness',
    });
    id = _match(raw['id'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}');
    label = _text(raw['label'], 128);
    binding = ConverseCatalogBinding.fromJson(raw['catalog_binding']);
    if (binding.id != id) _bad();
    selectable = _boolean(raw['selectable']);
    provider = ConverseModelProvider.fromJson(raw['provider']);
    reasoning = _effort(raw['reasoning']);
    final levels = raw['reasoning_options'];
    if (levels is! List ||
        levels.length > 6 ||
        levels.any((e) => !converseReasoning.contains(e)) ||
        levels.toSet().length != levels.length) {
      _bad();
    }
    reasoningOptions = List.unmodifiable(levels.cast<String>());
    reason = _text(raw['reason'], 256);
    if (raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown') {
      _bad();
    }
  }
  late final String id, label, reason;
  late final ConverseCatalogBinding binding;
  late final ConverseModelProvider provider;
  late final bool selectable;
  late final String? reasoning;
  late final List<String> reasoningOptions;
}

class ConverseModelsSnapshot {
  ConverseModelsSnapshot.fromJson(Object? value) {
    final raw = _object(value);
    _keys(raw, {
      'schema_version',
      'instance_id',
      'revision',
      'state_token',
      'selection',
      'selected_info',
      'selected_reasoning',
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
        raw['change_boundary'] != converseModelBoundary ||
        raw['qualification'] != 'not_checked' ||
        raw['readiness'] != 'unknown') {
      _bad();
    }
    instanceId = _match(
      raw['instance_id'],
      r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
    );
    revision = _integer(raw['revision'], 0, converseModelRevisionLimit);
    stateToken = _hash(raw['state_token']);
    selection = ConverseModelSelection.fromJson(raw['selection']);
    selectedInfo = raw['selected_info'] == null
        ? null
        : ConverseModelProvider.fromJson(raw['selected_info']);
    selectedReasoning = raw['selected_reasoning'] == null
        ? null
        : _text(raw['selected_reasoning'], 16);
    selectedError = raw['selected_error'] == null
        ? null
        : _text(raw['selected_error'], 256);
    if (selectedInfo == null
        ? selectedError == null || selectedReasoning != null
        : selectedError != null) {
      _bad();
    }
    final host = _object(raw['host']);
    _keys(host, {'provider', 'reasoning', 'selectable'});
    hostInfo = host['provider'] == null
        ? null
        : ConverseModelProvider.fromJson(host['provider']);
    hostReasoning = host['reasoning'] == null
        ? null
        : _text(host['reasoning'], 16);
    hostSelectable = _boolean(host['selectable']);
    if (hostSelectable != (hostInfo != null)) _bad();
    final profiles = raw['catalog_profiles'];
    if (profiles is! List || profiles.length > 32) _bad();
    catalogProfiles = List.unmodifiable(
      profiles.map(ConverseCatalogProfile.fromJson),
    );
    if (catalogProfiles.map((p) => p.id).toSet().length !=
        catalogProfiles.length) {
      _bad();
    }
    catalogError = raw['catalog_error'] == null
        ? null
        : _text(raw['catalog_error'], 256);
    busy = _boolean(raw['busy']);
    final operations = _object(raw['operation_state']);
    _keys(operations, {
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
    });
    operationState = Map.unmodifiable({
      for (final entry in operations.entries)
        entry.key: _integer(
          entry.value,
          0,
          {'leases', 'cleanup_pending', 'cleanup_failed'}.contains(entry.key)
              ? 64
              : converseModelRevisionLimit,
        ),
    });
    if ((!busy && operationState.values.any((count) => count != 0)) ||
        operationState['cleanup_pending']! + operationState['cleanup_failed']! >
            operationState['leases']!) {
      _bad();
    }
    final limits = _object(raw['limits']);
    _keys(limits, {'leases', 'receipts'});
    if (limits['leases'] != 64 || limits['receipts'] != 10000) _bad();
  }
  late final String instanceId, stateToken;
  late final int revision;
  late final ConverseModelSelection selection;
  late final ConverseModelProvider? selectedInfo, hostInfo;
  late final String? selectedReasoning,
      selectedError,
      hostReasoning,
      catalogError;
  late final bool hostSelectable, busy;
  late final List<ConverseCatalogProfile> catalogProfiles;
  late final Map<String, int> operationState;
  ConverseCatalogProfile? profile(ConverseModelSelection choice) {
    for (final profile in catalogProfiles) {
      if (choice.catalog?.matches(profile.binding) == true) return profile;
    }
    return null;
  }

  bool permits(ConverseModelSelection choice) => choice.source == 'host'
      ? hostSelectable
      : profile(choice)?.selectable == true &&
            (choice.reasoning == null ||
                profile(choice)!.reasoningOptions.contains(choice.reasoning));
  Map<String, Object?> change(ConverseModelSelection choice, String key) =>
      converseModelChange({
        'schema_version': '1.0',
        'expected_revision': revision,
        'expected_state': stateToken,
        'idempotency_key': key,
        'selection': choice.body,
      });
}

Map<String, Object?> converseModelChange(Object? value) {
  final raw = _object(value);
  _keys(raw, {
    'schema_version',
    'expected_revision',
    'expected_state',
    'idempotency_key',
    'selection',
  });
  if (raw['schema_version'] != '1.0') _bad();
  return Map.unmodifiable({
    'schema_version': '1.0',
    'expected_revision': _integer(
      raw['expected_revision'],
      0,
      converseModelRevisionLimit - 1,
    ),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': converseModelKey(raw['idempotency_key']),
    'selection': ConverseModelSelection.fromJson(raw['selection']).body,
  });
}

class ConverseModelReceipt {
  ConverseModelReceipt.fromJson(Object? value) {
    final raw = _object(value);
    _keys(raw, {
      'schema_version',
      'status',
      'role',
      'idempotency_key',
      'expected_revision',
      'expected_state',
      'revision',
      'selection',
      'change_boundary',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['status'] != 'model_selection_saved' ||
        raw['role'] != 'converse' ||
        raw['change_boundary'] != converseModelBoundary) {
      _bad();
    }
    key = converseModelKey(raw['idempotency_key']);
    expectedRevision = _integer(
      raw['expected_revision'],
      0,
      converseModelRevisionLimit - 1,
    );
    revision = _integer(raw['revision'], 1, converseModelRevisionLimit);
    if (revision != expectedRevision + 1) _bad();
    expectedState = _hash(raw['expected_state']);
    selection = ConverseModelSelection.fromJson(raw['selection']);
  }
  late final String key, expectedState;
  late final int expectedRevision, revision;
  late final ConverseModelSelection selection;
  bool matches(Map<String, Object?> request) =>
      key == request['idempotency_key'] &&
      expectedRevision == request['expected_revision'] &&
      expectedState == request['expected_state'] &&
      selection.matches(ConverseModelSelection.fromJson(request['selection']));
}

/// Reuse the existing bounded integer-only wire reader (256 KiB, depth 16,
/// 8192 nodes, 64 array items), before the closed Converse DTO validators.
Object? converseModelJsonDecode(List<int> bytes) => permissionJsonDecode(bytes);
