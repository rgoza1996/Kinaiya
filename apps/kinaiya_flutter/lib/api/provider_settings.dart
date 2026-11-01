/// Non-secret provider declarations. Saving these does not activate a role.
library;

import 'dart:convert';
import 'package:crypto/crypto.dart';

const providerMaxRevision = 9007199254740990;
const providerEfforts = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'];
const providerKinds = ['meta', 'lmstudio', 'compatible', 'laya', 'meta_stt'];
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
  if (kind == 'meta_stt') return _metaSttConfig(raw);
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

Object? _canonical(Object? value) => value is Map<String, Object?>
    ? {
        for (final key in value.keys.toList()..sort())
          key: _canonical(value[key]),
      }
    : value is List
    ? value.map(_canonical).toList()
    : value;

String providerConfigurationDigest(Map<String, Object?> config) => sha256
    .convert(utf8.encode(jsonEncode(_canonical(providerConfig(config)))))
    .toString();

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
    if (roles is! List ||
        roles.isEmpty ||
        roles.length > 4 ||
        roles.toSet().length != roles.length ||
        (stt
            ? roles.length != 1 || roles.single != 'stt'
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
    if (stt && (measuredComplete || measuredStream)) _bad();
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
    return result;
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
    if ((magnitude.length > 1 && magnitude.startsWith('0')) ||
        magnitude.length > 16 ||
        (magnitude.length == 16 &&
            magnitude.compareTo('9007199254740991') > 0) ||
        (at < text.length && '.eE'.contains(text[at]))) {
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
