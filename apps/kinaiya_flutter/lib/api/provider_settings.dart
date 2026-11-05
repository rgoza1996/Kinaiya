/// Non-secret provider declarations. Saving these does not activate a role.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart';

const providerMaxRevision = 9007199254740990;
const providerEfforts = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'];
const providerKinds = [
  'meta',
  'lmstudio',
  'compatible',
  'measured_chat',
  'laya',
  'native_d1',
  'meta_stt',
  'openrouter_tts',
];
const providerOperations = [
  'profile_upsert',
  'profile_remove',
  'credential_import',
  'credential_reference',
  'credential_remove',
];

Never _bad() => throw const FormatException('Invalid provider settings');

Map<String, Object?> _map(Object? value) {
  if (value is! Map<String, Object?>) _bad();
  return value;
}

String _text(Object? value, int max) {
  if (value is! String ||
      value.isEmpty ||
      value.length > max ||
      utf8.encode(value).length > max ||
      value.trim() != value ||
      value.codeUnits.any((c) => c < 32 || c == 127)) {
    _bad();
  }
  return value;
}

String providerIdentifier(Object? value) {
  final text = _text(value, 64);
  if (!RegExp(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$').hasMatch(text)) _bad();
  return text;
}

String _generation(Object? value) {
  final text = _text(value, 32);
  if (!RegExp(r'^[0-9a-f]{32}$').hasMatch(text)) _bad();
  return text;
}

int _integer(Object? value, int min, int max) {
  if (value is! int || value < min || value > max) _bad();
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) _bad();
  return value;
}

String _digest(Object? value) {
  final text = _text(value, 64);
  if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(text)) _bad();
  return text;
}

class ProviderCredentialBinding {
  const ProviderCredentialBinding(this.id, this.generation);
  factory ProviderCredentialBinding.fromJson(Object? value) {
    final raw = _map(value);
    if (raw.length != 2) _bad();
    return ProviderCredentialBinding(
      providerIdentifier(raw['id']),
      _generation(raw['generation']),
    );
  }
  final String id, generation;
  String get value => '$id:$generation';
  Map<String, Object?> toJson() => {'id': id, 'generation': generation};
}

/// Normalize exactly the supported native kind fields; unknown controls refuse.
Map<String, Object?> providerConfig(Object? value) {
  final raw = _map(value);
  final kind = raw['kind'];
  if (!providerKinds.contains(kind)) _bad();
  if (kind == 'measured_chat') return _measuredChatConfig(raw);
  if (kind == 'meta_stt') return _metaSttConfig(raw);
  if (kind == 'openrouter_tts') return _openRouterTtsConfig(raw);
  if (kind == 'native_d1') return _nativeD1Config(raw);
  final allowed = <String>{'kind', 'base_url', 'model', 'allow_cloud'};
  if (kind != 'laya') allowed.addAll(['max_tokens', 'reasoning_effort']);
  if (kind != 'lmstudio') allowed.add('credential');
  if (kind == 'meta' || kind == 'compatible') allowed.add('context_tokens');
  if (kind != 'meta') allowed.add('timeout_s');
  if (kind == 'laya') {
    allowed.addAll(['max_len', 'head_max_len', 'expected_artifact_sha256']);
  }
  if (raw.keys.any((key) => !allowed.contains(key))) _bad();
  final endpoint = _text(raw['base_url'], 2048);
  Uri uri;
  try {
    uri = Uri.parse(endpoint);
  } on FormatException {
    _bad();
  }
  final local = {'localhost', '127.0.0.1', '::1'}.contains(uri.host);
  final cloud = _flag(raw['allow_cloud']);
  if (!uri.hasAuthority ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      uri.hasQuery ||
      uri.hasFragment ||
      uri.port < 1 ||
      uri.port > 65535 ||
      RegExp(r'[\s\\\x00-\x1f\x7f]').hasMatch(endpoint) ||
      !(kind == 'lmstudio' ? {'http', 'https', 'ws', 'wss'} : {'http', 'https'})
          .contains(uri.scheme) ||
      (!local && (!cloud || uri.scheme != 'https')) ||
      (kind == 'meta' && !cloud) ||
      (kind == 'lmstudio' &&
          (!local ||
              cloud ||
              !uri.hasPort ||
              !{'', '/', '/v1', '/v1/', '/llm', '/llm/'}.contains(uri.path)))) {
    _bad();
  }
  final model = _text(raw['model'], 256);
  if ((kind == 'lmstudio' &&
          !RegExp(r'^[A-Za-z0-9._/-]{1,128}$').hasMatch(model)) ||
      (kind == 'laya' &&
          !RegExp(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$').hasMatch(model))) {
    _bad();
  }
  final result = <String, Object?>{
    'kind': kind,
    'base_url': endpoint.replaceFirst(RegExp(r'/+$'), ''),
    'model': model,
    'allow_cloud': cloud,
  };
  if (kind != 'lmstudio') {
    final credential = raw['credential'];
    if (kind == 'meta' && credential == null) _bad();
    result['credential'] = credential == null
        ? null
        : ProviderCredentialBinding.fromJson(credential).toJson();
  }
  if (kind != 'laya') {
    result['max_tokens'] = _integer(
      raw['max_tokens'] ?? (kind == 'lmstudio' ? 128 : 2048),
      16,
      kind == 'lmstudio' ? 2048 : 65536,
    );
    final effort =
        raw['reasoning_effort'] ??
        (kind == 'meta'
            ? 'xhigh'
            : kind == 'lmstudio'
            ? 'none'
            : null);
    if ((effort != null && !providerEfforts.contains(effort)) ||
        (kind == 'lmstudio' && effort != 'none')) {
      _bad();
    }
    result['reasoning_effort'] = effort;
  }
  if (kind == 'meta' || kind == 'compatible') {
    final context = raw['context_tokens'];
    result['context_tokens'] = context == null
        ? null
        : _integer(context, 64, providerMaxRevision);
    if (context != null && (result['max_tokens'] as int) >= (context as int)) {
      _bad();
    }
  }
  if (kind != 'meta') {
    result['timeout_s'] = _integer(
      raw['timeout_s'] ??
          (kind == 'lmstudio'
              ? 60
              : kind == 'laya'
              ? 5
              : 90),
      1,
      kind == 'laya' ? 120 : 900,
    );
  }
  if (kind == 'laya') {
    final max = raw['max_len'], head = raw['head_max_len'];
    result['max_len'] = max == null ? null : _integer(max, 2, 8192);
    result['head_max_len'] = head == null ? null : _integer(head, 1, 8192);
    if (max != null && head != null && (head as int) >= (max as int)) _bad();
    result['expected_artifact_sha256'] = raw['expected_artifact_sha256'] == null
        ? null
        : _digest(raw['expected_artifact_sha256']);
  }
  return Map.unmodifiable(result);
}

Map<String, Object?> _measuredChatConfig(Map<String, Object?> raw) {
  if ((raw.containsKey('allow_cloud') && raw['allow_cloud'] != false) ||
      raw['context_tokens'] == null ||
      [
        'reasoning_effort',
        'max_tokens',
        'timeout_s',
      ].any((field) => raw.containsKey(field) && raw[field] == null)) {
    _bad();
  }
  final normalized = providerConfig({
    ...raw,
    'kind': 'compatible',
    'allow_cloud': false,
    'reasoning_effort': raw['reasoning_effort'] ?? 'none',
  });
  final endpoint = Uri.parse(normalized['base_url'] as String);
  final controls = RegExp(r'\p{C}', unicode: true);
  if (!{'127.0.0.1', '::1'}.contains(endpoint.host) ||
      RegExp(
        r'^https?://[^/]*@',
        caseSensitive: false,
      ).hasMatch(normalized['base_url'] as String) ||
      controls.hasMatch(raw['base_url'] as String) ||
      controls.hasMatch(normalized['model'] as String)) {
    _bad();
  }
  return Map.unmodifiable({...normalized, 'kind': 'measured_chat'});
}

Map<String, Object?> _nativeD1Config(Map<String, Object?> raw) {
  const fields = {
    'kind',
    'base_url',
    'model',
    'allow_cloud',
    'credential',
    'timeout_s',
    'min_confidence',
  };
  if (raw.keys.any((key) => !fields.contains(key))) _bad();
  final endpoint = _text(raw['base_url'], 2048);
  final origin = RegExp(
    r'^https?://(?:localhost|127\.0\.0\.1|\[::1\]):([0-9]+)/?$',
    caseSensitive: false,
  ).firstMatch(endpoint);
  final port = origin == null ? null : int.tryParse(origin.group(1)!);
  final cloud = raw.containsKey('allow_cloud') ? raw['allow_cloud'] : false;
  if (origin?.end != endpoint.length ||
      port == null ||
      port < 1 ||
      port > 65535 ||
      cloud != false) {
    _bad();
  }
  final model = _text(raw['model'], 128);
  if (!RegExp(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$').hasMatch(model)) _bad();
  final floor = raw['min_confidence'];
  if (floor is! num || !floor.isFinite || floor < 0 || floor > 1) _bad();
  final credential = raw['credential'];
  return Map.unmodifiable({
    'kind': 'native_d1',
    'base_url': endpoint.replaceFirst(RegExp(r'/$'), ''),
    'model': model,
    'allow_cloud': false,
    'credential': credential == null
        ? null
        : ProviderCredentialBinding.fromJson(credential).toJson(),
    'timeout_s': _integer(
      raw.containsKey('timeout_s') ? raw['timeout_s'] : 5,
      1,
      120,
    ),
    'min_confidence': floor.toDouble(),
  });
}

/// The dedicated audio declaration never acquires text-generation defaults.
Map<String, Object?> _metaSttConfig(Map<String, Object?> raw) {
  const fields = {
    'kind',
    'base_url',
    'model',
    'allow_cloud',
    'credential',
    'timeout_s',
  };
  if (raw.keys.any((key) => !fields.contains(key))) _bad();
  final endpoint = _text(raw['base_url'], 2048);
  final model = _text(raw['model'], 256);
  final cloud = _flag(raw['allow_cloud']);
  final Uri uri;
  try {
    uri = Uri.parse(endpoint);
  } on FormatException {
    _bad();
  }
  final authorityStart = endpoint.indexOf('://') + 3;
  final pathStart = endpoint.indexOf('/', authorityStart);
  final path = pathStart < 0 ? '' : endpoint.substring(pathStart);
  final local = {'localhost', '127.0.0.1', '::1'}.contains(uri.host);
  if (!{'http', 'https'}.contains(uri.scheme) ||
      !uri.hasAuthority ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      uri.hasQuery ||
      uri.hasFragment ||
      uri.port < 1 ||
      uri.port > 65535 ||
      RegExp(r'[\s\\\x00-\x1f\x7f]').hasMatch(endpoint) ||
      endpoint.codeUnits.any((c) => c > 127) ||
      uri.host.codeUnits.any((c) => c > 127) ||
      (!local && uri.scheme != 'https') ||
      !cloud ||
      path.replaceFirst(RegExp(r'/+$'), '') != '/v1' ||
      model != 'muse-voice-transcribe-1.0') {
    _bad();
  }
  final credential = ProviderCredentialBinding.fromJson(raw['credential']);
  return Map.unmodifiable({
    'kind': 'meta_stt',
    'base_url': endpoint.replaceFirst(RegExp(r'/+$'), ''),
    'model': model,
    'allow_cloud': true,
    'credential': credential.toJson(),
    'timeout_s': _integer(
      raw.containsKey('timeout_s') ? raw['timeout_s'] : 120,
      1,
      300,
    ),
  });
}

/// Speech output uses only the native adapter's supported declaration.
Map<String, Object?> _openRouterTtsConfig(Map<String, Object?> raw) {
  const fields = {
    'kind',
    'base_url',
    'model',
    'voice',
    'allow_cloud',
    'credential',
    'timeout_s',
  };
  if (raw.keys.any((key) => !fields.contains(key))) _bad();
  final endpoint = _text(raw['base_url'], 2048);
  final Uri uri;
  try {
    uri = Uri.parse(endpoint);
  } on FormatException {
    _bad();
  }
  final authorityStart = endpoint.indexOf('://') + 3;
  final pathStart = endpoint.indexOf('/', authorityStart);
  final path = pathStart < 0 ? '' : endpoint.substring(pathStart);
  final loopback = {'localhost', '127.0.0.1', '::1'}.contains(uri.host);
  if (!uri.hasAuthority ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      uri.authority.contains('@') ||
      uri.hasQuery ||
      uri.hasFragment ||
      uri.port < 1 ||
      uri.port > 65535 ||
      RegExp(r'[\s\\\x00-\x1f\x7f]').hasMatch(endpoint) ||
      endpoint.codeUnits.any((c) => c > 127) ||
      path.replaceFirst(RegExp(r'/+$'), '') != '/api/v1' ||
      !((uri.scheme == 'https' &&
              uri.host == 'openrouter.ai' &&
              (!uri.hasPort || uri.port == 443)) ||
          (uri.scheme == 'http' && loopback)) ||
      raw['model'] != 'hexgrad/kokoro-82m' ||
      raw['voice'] != 'af_heart' ||
      raw['allow_cloud'] != true) {
    _bad();
  }
  return Map.unmodifiable({
    'kind': 'openrouter_tts',
    'base_url': endpoint.replaceFirst(RegExp(r'/+$'), ''),
    'model': 'hexgrad/kokoro-82m',
    'voice': 'af_heart',
    'allow_cloud': true,
    'credential': ProviderCredentialBinding.fromJson(
      raw['credential'],
    ).toJson(),
    'timeout_s': _integer(
      raw.containsKey('timeout_s') ? raw['timeout_s'] : 120,
      1,
      120,
    ),
  });
}

Object? _canonical(Object? value) => value is Map<String, Object?>
    ? {
        for (final key in value.keys.toList()..sort())
          key: _canonical(value[key]),
      }
    : value is List
    ? value.map(_canonical).toList()
    : value;

// Match the catalog's Python JSON float spelling without changing old kinds.
String _confidenceJson(double value) {
  final text = value.toString();
  final exponentAt = text.indexOf('e');
  if (exponentAt >= 0) {
    final exponent = int.parse(text.substring(exponentAt + 1));
    return '${text.substring(0, exponentAt)}e-${(-exponent).toString().padLeft(2, '0')}';
  }
  if (value > 0 && value < 0.0001) {
    final digits = text.substring(2);
    final first = digits.indexOf(RegExp('[1-9]'));
    final significant = digits.substring(first);
    final coefficient = significant.length == 1
        ? significant
        : '${significant[0]}.${significant.substring(1)}';
    return '${coefficient}e-${(first + 1).toString().padLeft(2, '0')}';
  }
  return text;
}

String providerConfigurationDigest(Map<String, Object?> config) {
  final normalized = _canonical(providerConfig(config)) as Map<String, Object?>;
  final body = normalized['kind'] == 'native_d1'
      ? '{${normalized.entries.map((entry) => '${jsonEncode(entry.key)}:${entry.key == 'min_confidence' ? _confidenceJson(entry.value as double) : jsonEncode(entry.value)}').join(',')}}'
      : jsonEncode(normalized);
  return sha256.convert(utf8.encode(body)).toString();
}

class ProviderProfile {
  ProviderProfile.fromJson(Object? value) {
    final raw = _map(value), capability = _map(_map(value)['capability']);
    id = providerIdentifier(raw['id']);
    label = _text(raw['label'], 128);
    generation = _integer(raw['generation'], 1, 64);
    configurationSha256 = _digest(raw['configuration_sha256']);
    removed = _flag(raw['removed']);
    credentialAvailable = _flag(raw['credential_available']);
    config = providerConfig(raw['config']);
    if (configurationSha256 != providerConfigurationDigest(config)) _bad();
    if (capability['qualification'] != 'not_checked' ||
        capability['readiness'] != 'unknown') {
      _bad();
    }
    final roles = capability['declared_roles'],
        inputs = capability['supported_inputs'];
    final stt = config['kind'] == 'meta_stt';
    final tts = config['kind'] == 'openrouter_tts';
    final nativeD1 = config['kind'] == 'native_d1';
    if (roles is! List ||
        roles.isEmpty ||
        roles.length > 4 ||
        roles.toSet().length != roles.length ||
        (stt || tts || nativeD1
            ? roles.length != 1 ||
                  roles.single !=
                      (stt
                          ? 'stt'
                          : tts
                          ? 'tts'
                          : 'decisions')
            : roles.any(
                (r) => !{
                  'agent',
                  'converse',
                  'decisions',
                  'auxiliary',
                }.contains(r),
              )) ||
        inputs is! List ||
        inputs.length != 1 ||
        inputs.single != (stt ? 'audio' : 'text')) {
      _bad();
    }
    declaredRoles = List.unmodifiable(roles.cast<String>());
    measuredComplete = _flag(capability['measured_complete_interface']);
    measuredStream = _flag(capability['measured_stream_interface']);
    if ((stt || tts || nativeD1) && (measuredComplete || measuredStream)) {
      _bad();
    }
  }
  late final String id, label, configurationSha256;
  late final int generation;
  late final bool removed,
      credentialAvailable,
      measuredComplete,
      measuredStream;
  late final Map<String, Object?> config;
  late final List<String> declaredRoles;
  String get kind => config['kind'] as String;
  String get model => config['model'] as String;
  ProviderCredentialBinding? get credential => config['credential'] == null
      ? null
      : ProviderCredentialBinding.fromJson(config['credential']);
}

class ProviderCredential {
  ProviderCredential.fromJson(Object? value) {
    final raw = _map(value);
    id = providerIdentifier(raw['id']);
    generation = _generation(raw['generation']);
    removed = _flag(raw['removed']);
    available = _flag(raw['available']);
    source = _text(raw['source'], 20);
    if (!{'imported', 'muse', 'openrouter', 'host_env'}.contains(source) ||
        raw['availability_scope'] != 'stored_key_only' ||
        raw['readiness'] != 'unknown' ||
        available == removed) {
      _bad();
    }
    reference = raw['reference'] == null
        ? null
        : providerIdentifier(raw['reference']);
    if ((source == 'host_env') != (reference != null)) _bad();
  }
  late final String id, generation, source;
  late final String? reference;
  late final bool removed, available;
  ProviderCredentialBinding get binding =>
      ProviderCredentialBinding(id, generation);
}

class ProviderKnownReference {
  ProviderKnownReference.fromJson(Object? value) {
    final raw = _map(value);
    source = _text(raw['source'], 20);
    reference = raw['reference'] == null
        ? null
        : providerIdentifier(raw['reference']);
    if (!{'muse', 'openrouter', 'host_env'}.contains(source) ||
        (source == 'host_env') != (reference != null)) {
      _bad();
    }
  }
  late final String source;
  late final String? reference;
  String get id => '$source:${reference ?? ''}';
  String get label => source == 'muse'
      ? 'Existing Muse / Meta credential'
      : source == 'openrouter'
      ? 'Existing OpenRouter credential'
      : 'Host alias · $reference';
}

class ProviderCatalogSnapshot {
  ProviderCatalogSnapshot.fromJson(Object? value) {
    final raw = _map(value);
    if (raw['schema_version'] != '1.0' ||
        raw['change_boundary'] != 'role_owner' ||
        raw['credential_reference_boundary'] !=
            'explicit_registration_snapshot') {
      _bad();
    }
    revision = _integer(raw['revision'], 0, providerMaxRevision);
    List<Object?> rows(String key, int max) {
      final list = raw[key];
      if (list is! List || list.length > max) _bad();
      return list.cast<Object?>();
    }

    profiles = List.unmodifiable(
      rows('profiles', 32).map(ProviderProfile.fromJson),
    );
    credentials = List.unmodifiable(
      rows('credentials', 32).map(ProviderCredential.fromJson),
    );
    knownReferences = List.unmodifiable(
      rows('known_references', 34).map(ProviderKnownReference.fromJson),
    );
    if (profiles.map((p) => p.id).toSet().length != profiles.length ||
        credentials.map((c) => c.id).toSet().length != credentials.length ||
        knownReferences.map((r) => r.id).toSet().length !=
            knownReferences.length) {
      _bad();
    }
    final limits = _map(raw['limits']);
    if (limits['profiles'] != 32 ||
        limits['profile_generations'] != 64 ||
        limits['credentials'] != 32 ||
        limits['credential_generations'] != 32 ||
        limits['receipts'] != 10000) {
      _bad();
    }
  }
  late final int revision;
  late final List<ProviderProfile> profiles;
  late final List<ProviderCredential> credentials;
  late final List<ProviderKnownReference> knownReferences;
}

class ProviderChangeReceipt {
  ProviderChangeReceipt.fromJson(Object? value) {
    final raw = _map(value);
    if (raw['schema_version'] != '1.0' ||
        raw['status'] != 'catalog_saved' ||
        raw['change_boundary'] != 'role_owner') {
      _bad();
    }
    revision = _integer(raw['revision'], 1, providerMaxRevision);
    idempotencyKey = _text(raw['idempotency_key'], 128);
    if (!RegExp(r'^[A-Za-z0-9:._-]+$').hasMatch(idempotencyKey)) _bad();
    operation = _text(raw['operation'], 24);
    if (!providerOperations.contains(operation)) _bad();
    final result = _map(raw['result']);
    id = providerIdentifier(result['id']);
    removed = _flag(result['removed']);
    if (operation.startsWith('profile_')) {
      generation = _integer(result['generation'], 1, 64);
      configurationSha256 = result['configuration_sha256'] == null
          ? null
          : _digest(result['configuration_sha256']);
      if (operation == 'profile_upsert' && configurationSha256 == null) _bad();
    } else {
      generation = _generation(result['generation']);
    }
    if (removed != operation.endsWith('_remove')) _bad();
  }
  late final String idempotencyKey, operation, id;
  late final Object generation;
  String? configurationSha256;
  late final int revision;
  late final bool removed;
  bool matches(Map<String, Object?> request) =>
      idempotencyKey == request['idempotency_key'] &&
      operation == request['operation'] &&
      revision == (request['expected_revision'] as int) + 1 &&
      id ==
          request[operation.startsWith('profile_')
              ? 'profile_id'
              : 'credential_id'] &&
      (operation != 'profile_upsert' ||
          configurationSha256 ==
              providerConfigurationDigest(
                request['config'] as Map<String, Object?>,
              ));
}

/// Reject duplicate fields, unsafe integer spellings and deep/oversized JSON.
Object? providerJsonDecode(List<int> bytes) => _JsonReader(bytes).read();

class _ConfidenceNumber {
  const _ConfidenceNumber(this.value);
  final double value;
}

class _JsonReader {
  _JsonReader(List<int> bytes)
    : text = utf8.decode(bytes, allowMalformed: false) {
    if (bytes.length > 256 * 1024) _bad();
  }
  final String text;
  int at = 0, values = 0;
  void _space() {
    while (at < text.length && ' \n\r\t'.contains(text[at])) {
      at++;
    }
  }

  Object? read() {
    final result = _value(0);
    _space();
    if (at != text.length) _bad();
    _confidenceNumbers(result);
    return result;
  }

  // Only the new native confidence field admits fractional wire numbers.
  // Delay this check until the whole object is known, independent of key order.
  void _confidenceNumbers(Object? value) {
    if (value is Map<String, Object?>) {
      for (final key in value.keys) {
        final child = value[key];
        if (child is _ConfidenceNumber) {
          if (value['kind'] != 'native_d1' || key != 'min_confidence') _bad();
          value[key] = child.value;
        } else {
          _confidenceNumbers(child);
        }
      }
    } else if (value is List<Object?>) {
      for (final child in value) {
        _confidenceNumbers(child);
      }
    } else if (value is _ConfidenceNumber) {
      _bad();
    }
  }

  Object? _value(int depth) {
    if (depth > 16 || ++values > 8192) _bad();
    _space();
    if (at >= text.length) _bad();
    final c = text[at];
    if (c == '"') return _string();
    if (c == '{') {
      at++;
      final map = <String, Object?>{};
      _space();
      if (at < text.length && text[at] == '}') {
        at++;
        return map;
      }
      while (true) {
        _space();
        if (at >= text.length || text[at] != '"') _bad();
        final key = _string();
        if (key.length > 128 || map.containsKey(key) || map.length >= 32) {
          _bad();
        }
        _space();
        if (at >= text.length || text[at++] != ':') _bad();
        map[key] = _value(depth + 1);
        _space();
        if (at >= text.length) _bad();
        final separator = text[at++];
        if (separator == '}') return map;
        if (separator != ',') _bad();
      }
    }
    if (c == '[') {
      at++;
      final list = <Object?>[];
      _space();
      if (at < text.length && text[at] == ']') {
        at++;
        return list;
      }
      while (true) {
        if (list.length >= 64) _bad();
        list.add(_value(depth + 1));
        _space();
        if (at >= text.length) _bad();
        final separator = text[at++];
        if (separator == ']') return list;
        if (separator != ',') _bad();
      }
    }
    for (final pair in [('true', true), ('false', false), ('null', null)]) {
      if (text.startsWith(pair.$1, at)) {
        at += pair.$1.length;
        return pair.$2;
      }
    }
    final start = at;
    if (text[at] == '-') at++;
    final digits = at;
    while (at < text.length &&
        text.codeUnitAt(at) >= 48 &&
        text.codeUnitAt(at) <= 57) {
      at++;
    }
    if (at == digits) _bad();
    final magnitude = text.substring(digits, at);
    if (magnitude.length > 1 && magnitude.startsWith('0')) _bad();
    var fractional = false;
    if (at < text.length && text[at] == '.') {
      fractional = true;
      at++;
      final startFraction = at;
      while (at < text.length &&
          text.codeUnitAt(at) >= 48 &&
          text.codeUnitAt(at) <= 57) {
        at++;
      }
      if (at == startFraction) _bad();
    }
    if (at < text.length && 'eE'.contains(text[at])) {
      fractional = true;
      at++;
      if (at < text.length && '+-'.contains(text[at])) at++;
      final startExponent = at;
      while (at < text.length &&
          text.codeUnitAt(at) >= 48 &&
          text.codeUnitAt(at) <= 57) {
        at++;
      }
      if (at == startExponent) _bad();
    }
    if (fractional) {
      final value = double.tryParse(text.substring(start, at));
      if (value == null || !value.isFinite) _bad();
      return _ConfidenceNumber(value);
    }
    if (magnitude.length > 16 ||
        (magnitude.length == 16 &&
            magnitude.compareTo('9007199254740991') > 0)) {
      _bad();
    }
    return int.parse(text.substring(start, at));
  }

  String _string() {
    final start = at++;
    while (at < text.length) {
      final c = text.codeUnitAt(at++);
      if (c == 34) {
        final value = jsonDecode(text.substring(start, at)) as String;
        for (var i = 0; i < value.length; i++) {
          final u = value.codeUnitAt(i);
          if (u >= 0xd800 && u <= 0xdbff) {
            if (++i >= value.length ||
                value.codeUnitAt(i) < 0xdc00 ||
                value.codeUnitAt(i) > 0xdfff) {
              _bad();
            }
          } else if (u >= 0xdc00 && u <= 0xdfff) {
            _bad();
          }
        }
        return value;
      }
      if (c < 32) _bad();
      if (c == 92) {
        if (at >= text.length) _bad();
        at++;
      }
    }
    _bad();
  }
}

const kMaskedCustomApiKey = '••••••••••••';
const customProviderActions = [
  'add_custom_provider',
  'update_custom_provider',
  'remove_custom_provider',
  'set_custom_provider_enabled',
  'bind_custom_role_model',
  'set_global_reasoning_effort',
];

class CustomProviderProfile {
  const CustomProviderProfile({
    required this.profileId,
    required this.name,
    required this.baseUrl,
    this.chatCompletionsPath = '/chat/completions',
    this.modelsPath = '/models',
    this.customEndpoints = '',
    this.maxOutputTokens,
    required this.enabled,
    required this.hasApiKey,
    required this.maskedApiKey,
    required this.selectedModel,
    required this.qualifiedModelId,
    required this.discoveredModels,
    required this.allowRemoteHttp,
    required this.referencedBy,
    required this.capabilities,
  });

  final String profileId;
  final String name;
  final String baseUrl;
  final String chatCompletionsPath;
  final String modelsPath;
  final String customEndpoints;
  final int? maxOutputTokens;
  final bool enabled;
  final bool hasApiKey;
  final String maskedApiKey;
  final String selectedModel;
  final String qualifiedModelId;
  final List<String> discoveredModels;
  final bool allowRemoteHttp;
  final List<String> referencedBy;
  final Map<String, Object?> capabilities;

  factory CustomProviderProfile.fromJson(Object? raw) {
    final m = _map(raw);
    final rawDiscovered = m['discovered_models'];
    final discovered = <String>[];
    if (rawDiscovered is List<Object?>) {
      for (final item in rawDiscovered) {
        if (item is String && item.trim().isNotEmpty) {
          discovered.add(item.trim());
        }
      }
    }
    final rawRefs = m['referenced_by'];
    final refs = <String>[];
    if (rawRefs is List<Object?>) {
      for (final item in rawRefs) {
        if (item is String && item.trim().isNotEmpty) {
          refs.add(item.trim());
        }
      }
    }
    final rawCaps = m['capabilities'];
    final caps = rawCaps is Map<String, Object?>
        ? Map<String, Object?>.unmodifiable(rawCaps)
        : const <String, Object?>{
            'chat': 'unverified',
            'streaming': 'unverified',
            'reasoning': 'unverified',
            'multimodal': 'unverified',
            'tool_use': 'unverified',
            'agent_qualified': false,
          };
    final pid = (m['profile_id'] as String?)?.trim() ?? '';
    final selected = (m['selected_model'] as String?)?.trim() ?? '';
    final rawChatPath = (m['chat_completions_path'] as String?)?.trim() ?? '';
    final rawModelsPath = (m['models_path'] as String?)?.trim() ?? '';
    final rawMaxTokens = m['max_output_tokens'];
    int? parsedMaxTokens;
    if (rawMaxTokens is int && rawMaxTokens > 0) {
      parsedMaxTokens = rawMaxTokens;
    } else if (rawMaxTokens is String && rawMaxTokens.trim().isNotEmpty) {
      final v = int.tryParse(rawMaxTokens.trim());
      if (v != null && v > 0) parsedMaxTokens = v;
    }
    return CustomProviderProfile(
      profileId: pid,
      name: (m['name'] as String?)?.trim() ?? pid.toUpperCase(),
      baseUrl: (m['base_url'] as String?)?.trim() ?? '',
      chatCompletionsPath: rawChatPath.isEmpty
          ? '/chat/completions'
          : rawChatPath,
      modelsPath: rawModelsPath.isEmpty ? '/models' : rawModelsPath,
      customEndpoints: (m['custom_endpoints'] as String?)?.trim() ?? '',
      maxOutputTokens: parsedMaxTokens,
      enabled: m['enabled'] is! bool || (m['enabled'] as bool),
      hasApiKey: m['has_api_key'] == true,
      maskedApiKey: (m['masked_api_key'] as String?) ?? '',
      selectedModel: selected,
      qualifiedModelId:
          (m['qualified_model_id'] as String?) ??
          (selected.isNotEmpty ? '$pid:$selected' : ''),
      discoveredModels: List.unmodifiable(discovered),
      allowRemoteHttp: m['allow_remote_http'] == true,
      referencedBy: List.unmodifiable(refs),
      capabilities: caps,
    );
  }
}

class CustomProviderRoleBinding {
  const CustomProviderRoleBinding({
    required this.profileId,
    required this.providerName,
    required this.baseUrl,
    required this.enabled,
    required this.modelId,
    required this.qualifiedModelId,
    this.reasoningEffort,
    this.effectiveReasoningEffort = 'medium',
    this.status = 'proposed_unavailable',
    this.active = false,
    this.unavailableReason,
    this.diagnostic,
  });

  final String profileId;
  final String providerName;
  final String baseUrl;
  final bool enabled;
  final String modelId;
  final String qualifiedModelId;
  final String? reasoningEffort;
  final String effectiveReasoningEffort;
  final String status;
  final bool active;
  final String? unavailableReason;
  final String? diagnostic;

  factory CustomProviderRoleBinding.fromJson(Object? raw) {
    final m = _map(raw);
    final pid = (m['profile_id'] as String?)?.trim() ?? '';
    final mid = (m['model_id'] as String?)?.trim() ?? '';
    final rawEffort = (m['reasoning_effort'] as String?)?.trim();
    final rawEffective = (m['effective_reasoning_effort'] as String?)?.trim();
    final rawStatus = (m['status'] as String?)?.trim();
    final rawReason = (m['unavailable_reason'] as String?)?.trim();
    final rawDiagnostic = (m['diagnostic'] as String?)?.trim();
    return CustomProviderRoleBinding(
      profileId: pid,
      providerName: (m['provider_name'] as String?)?.trim() ?? pid,
      baseUrl: (m['base_url'] as String?)?.trim() ?? '',
      enabled: m['enabled'] is! bool || (m['enabled'] as bool),
      modelId: mid,
      qualifiedModelId: (m['qualified_model_id'] as String?) ?? '$pid:$mid',
      reasoningEffort: (rawEffort != null && rawEffort.isNotEmpty)
          ? rawEffort
          : null,
      effectiveReasoningEffort:
          (rawEffective != null && rawEffective.isNotEmpty)
          ? rawEffective
          : (rawEffort != null && rawEffort.isNotEmpty ? rawEffort : 'medium'),
      status: (rawStatus != null && rawStatus.isNotEmpty)
          ? rawStatus
          : 'proposed_unavailable',
      active: m['active'] == true,
      unavailableReason: (rawReason != null && rawReason.isNotEmpty)
          ? rawReason
          : null,
      diagnostic: (rawDiagnostic != null && rawDiagnostic.isNotEmpty)
          ? rawDiagnostic
          : null,
    );
  }
}

class CustomProvidersSnapshot {
  const CustomProvidersSnapshot({
    required this.schemaVersion,
    required this.envFileName,
    required this.unlimited,
    this.revision = 0,
    this.stateSha256 = '',
    this.reconciled = false,
    this.clientOperationId,
    this.committedRevision,
    this.globalReasoningEffort = 'medium',
    required this.profiles,
    required this.roleBindings,
  });

  const CustomProvidersSnapshot.empty()
    : schemaVersion = 1,
      envFileName = 'custom_providers.env',
      unlimited = true,
      revision = 0,
      stateSha256 = '',
      reconciled = false,
      clientOperationId = null,
      committedRevision = null,
      globalReasoningEffort = 'medium',
      profiles = const [],
      roleBindings = const {};

  final int schemaVersion;
  final String envFileName;
  final bool unlimited;
  final int revision;
  final String stateSha256;
  final bool reconciled;
  final String? clientOperationId;
  final int? committedRevision;
  final String globalReasoningEffort;
  final List<CustomProviderProfile> profiles;
  final Map<String, CustomProviderRoleBinding?> roleBindings;

  bool matchesOperation(Map<String, Object?> request) {
    final opId = request['client_operation_id'];
    final expectedRev = request['expected_revision'];
    if (opId is! String || expectedRev is! int) return false;
    if (clientOperationId != null && clientOperationId != opId) return false;
    final targetRev = committedRevision ?? revision;
    return targetRev > expectedRev;
  }

  factory CustomProvidersSnapshot.fromJson(Object? raw) {
    final m = _map(raw);
    final rawProfiles = m['profiles'];
    final parsedProfiles = <CustomProviderProfile>[];
    if (rawProfiles is List<Object?>) {
      for (final item in rawProfiles) {
        parsedProfiles.add(CustomProviderProfile.fromJson(item));
      }
    }
    final rawRoles = m['role_bindings'];
    final parsedRoles = <String, CustomProviderRoleBinding?>{};
    if (rawRoles is Map<String, Object?>) {
      for (final entry in rawRoles.entries) {
        parsedRoles[entry.key] = entry.value == null
            ? null
            : CustomProviderRoleBinding.fromJson(entry.value);
      }
    }
    final rawGlobalEffort =
        (m['global_reasoning_effort'] as String?)?.trim() ?? '';
    final rawOpId = (m['client_operation_id'] as String?)?.trim();
    return CustomProvidersSnapshot(
      schemaVersion: m['schema_version'] is int
          ? m['schema_version'] as int
          : 1,
      envFileName:
          (m['env_file_name'] as String?)?.trim() ?? 'custom_providers.env',
      unlimited: m['unlimited'] is! bool || (m['unlimited'] as bool),
      revision: m['revision'] is int ? m['revision'] as int : 0,
      stateSha256: (m['state_sha256'] as String?)?.trim() ?? '',
      reconciled: m['reconciled'] == true,
      clientOperationId: (rawOpId != null && rawOpId.isNotEmpty)
          ? rawOpId
          : null,
      committedRevision: m['committed_revision'] is int
          ? m['committed_revision'] as int
          : null,
      globalReasoningEffort: rawGlobalEffort.isEmpty
          ? 'medium'
          : rawGlobalEffort,
      profiles: List.unmodifiable(parsedProfiles),
      roleBindings: Map.unmodifiable(parsedRoles),
    );
  }
}

class CustomProviderReceiptResult {
  const CustomProviderReceiptResult({
    required this.status,
    required this.found,
    required this.reconciled,
    required this.clientOperationId,
    this.action,
    this.targetProfileId,
    this.committedRevision,
    this.committedStateSha256,
    required this.currentRevision,
    required this.currentStateSha256,
    required this.snapshot,
  });

  final String status;
  final bool found;
  final bool reconciled;
  final String clientOperationId;
  final String? action;
  final String? targetProfileId;
  final int? committedRevision;
  final String? committedStateSha256;
  final int currentRevision;
  final String currentStateSha256;
  final CustomProvidersSnapshot snapshot;

  bool matches(Map<String, Object?> request) {
    final opId = request['client_operation_id'];
    final expectedRev = request['expected_revision'];
    final reqAction = request['action'];
    if (opId is! String || expectedRev is! int || reqAction is! String) {
      return false;
    }
    if (!found || status != 'committed' || clientOperationId != opId) {
      return false;
    }
    if (action != null && action != reqAction) return false;
    final targetRev = committedRevision ?? snapshot.revision;
    return targetRev > expectedRev;
  }

  factory CustomProviderReceiptResult.fromJson(Object? raw) {
    final m = _map(raw);
    final rawStatus = (m['status'] as String?)?.trim() ?? '';
    final rawOpId =
        (m['client_operation_id'] as String?)?.trim() ??
        (m['operation_id'] as String?)?.trim() ??
        (m['idempotency_key'] as String?)?.trim() ??
        '';
    final rawAction = (m['action'] as String?)?.trim();
    final rawTarget = (m['target_profile_id'] as String?)?.trim();
    final rawCommittedSha = (m['committed_state_sha256'] as String?)?.trim();
    final snapObj = m['snapshot'] is Map<String, Object?> ? m['snapshot'] : raw;
    final parsedSnap = CustomProvidersSnapshot.fromJson(snapObj);
    return CustomProviderReceiptResult(
      status: rawStatus.isEmpty
          ? (m['found'] == true ? 'committed' : 'not_found')
          : rawStatus,
      found: m['found'] == true,
      reconciled: m['reconciled'] == true,
      clientOperationId: rawOpId,
      action: (rawAction != null && rawAction.isNotEmpty) ? rawAction : null,
      targetProfileId: (rawTarget != null && rawTarget.isNotEmpty)
          ? rawTarget
          : null,
      committedRevision: m['committed_revision'] is int
          ? m['committed_revision'] as int
          : null,
      committedStateSha256:
          (rawCommittedSha != null && rawCommittedSha.isNotEmpty)
          ? rawCommittedSha
          : null,
      currentRevision: m['current_revision'] is int
          ? m['current_revision'] as int
          : parsedSnap.revision,
      currentStateSha256:
          (m['current_state_sha256'] as String?)?.trim() ??
          parsedSnap.stateSha256,
      snapshot: parsedSnap,
    );
  }
}

class CustomModelsDiscoveryResult {
  const CustomModelsDiscoveryResult({
    required this.profileId,
    required this.providerName,
    required this.modelsUrl,
    required this.status,
    required this.httpStatus,
    required this.modelIds,
    required this.manualFallbackAllowed,
    required this.message,
    required this.snapshot,
  });

  final String profileId;
  final String providerName;
  final String modelsUrl;
  final String status;
  final int? httpStatus;
  final List<String> modelIds;
  final bool manualFallbackAllowed;
  final String message;
  final CustomProvidersSnapshot snapshot;

  factory CustomModelsDiscoveryResult.fromJson(Object? raw) {
    final m = _map(raw);
    final rawModels = m['models'];
    final ids = <String>[];
    if (rawModels is List<Object?>) {
      for (final item in rawModels) {
        if (item is Map<String, Object?> && item['model_id'] is String) {
          ids.add((item['model_id'] as String).trim());
        } else if (item is String) {
          ids.add(item.trim());
        }
      }
    }
    return CustomModelsDiscoveryResult(
      profileId: (m['profile_id'] as String?)?.trim() ?? '',
      providerName: (m['provider_name'] as String?)?.trim() ?? '',
      modelsUrl: (m['models_url'] as String?)?.trim() ?? '',
      status: (m['status'] as String?)?.trim() ?? 'unknown',
      httpStatus: m['http_status'] is int ? m['http_status'] as int : null,
      modelIds: List.unmodifiable(ids),
      manualFallbackAllowed:
          m['manual_fallback_allowed'] is! bool ||
          (m['manual_fallback_allowed'] as bool),
      message: (m['message'] as String?)?.trim() ?? '',
      snapshot: m['snapshot'] != null
          ? CustomProvidersSnapshot.fromJson(m['snapshot'])
          : const CustomProvidersSnapshot.empty(),
    );
  }
}
