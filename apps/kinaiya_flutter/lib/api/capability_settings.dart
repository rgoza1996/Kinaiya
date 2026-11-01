/// Installed owner capabilities. None of these declarations grants execution.
library;

import 'dart:convert';

const capabilityRevisionLimit = 9007199254740990;
const capabilityTools = [
  'read_file',
  'write_file',
  'search',
  'shell',
  'read_transcript',
  'select_context',
  'mcp_list',
  'mcp_call',
  'skills_list',
  'skill_load',
  'plugins_list',
  'plugin_call',
  'publish_ui',
  'consult_model',
  'tool_catalog',
  'finish',
];
const capabilitySections = ['tools', 'skills', 'plugins'];
Never _bad() =>
    throw const FormatException('Invalid native capability metadata');
Map<String, Object?> _object(Object? value, Set<String> keys) {
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !keys.containsAll(value.keys)) {
    _bad();
  }
  return value;
}

String _text(Object? value, int max, {bool empty = false}) {
  if (value is! String ||
      (!empty && value.isEmpty) ||
      value.contains('\u0000')) {
    _bad();
  }
  for (var i = 0; i < value.length; i++) {
    final n = value.codeUnitAt(i);
    if (n >= 0xd800 && n <= 0xdbff) {
      if (++i >= value.length ||
          value.codeUnitAt(i) < 0xdc00 ||
          value.codeUnitAt(i) > 0xdfff) {
        _bad();
      }
    } else if (n >= 0xdc00 && n <= 0xdfff) {
      _bad();
    }
  }
  if (utf8.encode(value).length > max) _bad();
  return value;
}

String _match(Object? value, String pattern) {
  final text = _text(value, 4096);
  if (RegExp(pattern).matchAsPrefix(text)?.end != text.length) _bad();
  return text;
}

String _authority(Object? v) => _match(v, r'[0-9a-f]{32}');
String _hash(Object? v) => _match(v, r'[0-9a-f]{64}');
String capabilityChangeKey(Object? v) => _match(v, r'[A-Za-z0-9:._-]{1,128}');
int _int(Object? v, int min, [int max = capabilityRevisionLimit]) {
  if (v is! int || v < min || v > max) _bad();
  return v;
}

bool _bool(Object? v) {
  if (v is! bool) _bad();
  return v;
}

List<Object?> _list(Object? v, int max) {
  if (v is! List<Object?> || v.length > max) _bad();
  return v;
}

Object? _freeze(Object? v) {
  if (v is Map<String, Object?>) {
    return Map<String, Object?>.unmodifiable(
      v.map((k, v) => MapEntry(k, _freeze(v))),
    );
  }
  if (v is List) return List<Object?>.unmodifiable(v.map(_freeze));
  return v;
}

List<String> capabilityDisabled(Object? v) {
  final names = _list(v, 15).map((v) => _text(v, 64)).toList();
  if (names.toSet().length != names.length ||
      names.contains('finish') ||
      names.any((v) => !capabilityTools.contains(v)) ||
      jsonEncode(names) !=
          jsonEncode(capabilityTools.where(names.contains).toList())) {
    _bad();
  }
  return List.unmodifiable(names);
}

String capabilityActionSection(Map<String, Object?> action) =>
    action['operation'] == 'set_tools'
    ? 'tools'
    : (action['operation'] as String).endsWith('skill')
    ? 'skills'
    : 'plugins';

Map<String, Object?> capabilityAction(Object? value) {
  if (value is! Map<String, Object?>) _bad();
  final op = value['operation'];
  if (op == 'set_tools') {
    final raw = _object(value, {'operation', 'disabled'});
    return Map.unmodifiable({
      'operation': op,
      'disabled': capabilityDisabled(raw['disabled']),
    });
  }
  if (op == 'enable_skill' || op == 'disable_skill') {
    final raw = _object(value, {'operation', 'skill_id', 'digest'});
    final id = _text(raw['skill_id'], 265);
    if (id.codeUnits.any((n) => n < 32 || n == 127)) _bad();
    return Map.unmodifiable({
      'operation': op,
      'skill_id': id,
      'digest': _match(raw['digest'], r'sha256:[0-9a-f]{64}'),
    });
  }
  if (!{
    'evaluate_plugin',
    'promote_plugin',
    'disable_plugin',
    'rollback_plugin',
  }.contains(op)) {
    _bad();
  }
  final raw = _object(value, {
    'operation',
    'plugin_id',
    'version_id',
    'source_sha256',
    'manifest_sha256',
    'report_id',
    'reviewed',
  });
  final report = raw['report_id'] == null
      ? null
      : _match(raw['report_id'], r'rep-[A-Za-z0-9-]{1,60}');
  final reviewed = _bool(raw['reviewed']);
  if ((op == 'promote_plugin') != (report != null) ||
      ({'promote_plugin', 'rollback_plugin'}.contains(op) && !reviewed)) {
    _bad();
  }
  return Map.unmodifiable({
    'operation': op,
    'plugin_id': _match(raw['plugin_id'], r'[a-z][a-z0-9_-]{0,63}'),
    'version_id': _match(raw['version_id'], r'v-[0-9a-f]{24}'),
    'source_sha256': _hash(raw['source_sha256']),
    'manifest_sha256': _hash(raw['manifest_sha256']),
    'report_id': report,
    'reviewed': reviewed,
  });
}

Map<String, Object?> capabilityChange(Object? value) {
  final raw = _object(value, {
    'schema_version',
    'expected_backend',
    'expected_instance',
    'expected_revision',
    'expected_state',
    'idempotency_key',
    'action',
  });
  if (raw['schema_version'] != '1.0') _bad();
  return Map.unmodifiable({
    'schema_version': '1.0',
    'expected_backend': _authority(raw['expected_backend']),
    'expected_instance': _authority(raw['expected_instance']),
    'expected_revision': _int(
      raw['expected_revision'],
      0,
      capabilityRevisionLimit - 1,
    ),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': capabilityChangeKey(raw['idempotency_key']),
    'action': capabilityAction(raw['action']),
  });
}

class CapabilityItem {
  CapabilityItem.fromJson(Object? value) {
    if (value is! Map<String, Object?>) _bad();
    kind = value['kind'] as String? ?? '';
    final Set<String> keys;
    if (kind == 'native') {
      keys = {
        'kind',
        'name',
        'qualified_id',
        'group',
        'description',
        'protocol_version',
        'protected',
        'requested_enabled',
        'host_available',
        'effective_available',
        'authorization',
      };
    } else if (kind == 'skill') {
      keys = {
        'kind',
        'skill_id',
        'name',
        'version',
        'digest',
        'description',
        'source',
        'installed',
        'enabled',
        'status',
        'compatibility_notes',
        'model_invocable',
        'user_invocable',
        'scope',
        'integrity',
        'dependent_task_ids',
        'authorization',
        'target_sha256',
      };
    } else if (kind == 'plugin') {
      keys = {
        'kind',
        'plugin_id',
        'version_id',
        'source_sha256',
        'manifest_sha256',
        'description',
        'runtime',
        'author',
        'license',
        'installed',
        'enabled',
        'active_version',
        'approved',
        'suite_configured',
        'rollback_eligible',
        'reports',
        'reports_total',
        'scope',
        'integrity',
        'dependent_task_ids',
        'authorization',
        'target_sha256',
      };
    } else {
      _bad();
    }
    final raw = _object(value, keys);
    if (!{
      'blocked',
      'unknown',
      'requires_action_checks',
    }.contains(raw['authorization'])) {
      _bad();
    }
    _text(raw['description'], kind == 'skill' ? 65536 : 4096, empty: true);
    if (kind == 'native') {
      final name = _text(raw['name'], 64);
      if (!capabilityTools.contains(name) ||
          raw['qualified_id'] != 'native:$name' ||
          raw['protocol_version'] != '1.0' ||
          _bool(raw['protected']) != (name == 'finish')) {
        _bad();
      }
      _text(raw['group'], 64);
      final requested = _bool(raw['requested_enabled']),
          host = _bool(raw['host_available']),
          effective = _bool(raw['effective_available']);
      if ((effective && (!requested || !host)) ||
          (name == 'finish' && (!requested || !host || !effective))) {
        _bad();
      }
    } else {
      _bool(raw['installed']);
      _bool(raw['enabled']);
      _hash(raw['target_sha256']);
      if (!{
        'local_only',
        'public_synthetic',
        'private_cloud_authorized',
      }.contains(raw['scope'])) {
        _bad();
      }
      for (final id in _list(raw['dependent_task_ids'], 256)) {
        _text(id, 128);
      }
      if (kind == 'skill') {
        _text(raw['skill_id'], 265);
        _text(raw['name'], 128);
        _text(raw['version'], 64);
        _match(raw['digest'], r'sha256:[0-9a-f]{64}');
        _text(raw['status'], 64);
        if (raw['integrity'] != 'registered_only') _bad();
        _bool(raw['model_invocable']);
        _bool(raw['user_invocable']);
        for (final note in _list(raw['compatibility_notes'], 65536)) {
          _text(note, 65536, empty: true);
        }
        final source = _object(raw['source'], {
          'kind',
          'namespace',
          'repository',
          'ref',
          'subdir',
        });
        if (!{'local', 'github', 'clawhub'}.contains(source['kind'])) _bad();
        for (final k in ['namespace', 'repository', 'ref', 'subdir']) {
          if (source[k] != null) _text(source[k], 65536);
        }
      } else {
        _match(raw['plugin_id'], r'[a-z][a-z0-9_-]{0,63}');
        _match(raw['version_id'], r'v-[0-9a-f]{24}');
        _hash(raw['source_sha256']);
        _hash(raw['manifest_sha256']);
        _text(raw['author'], 800);
        _text(raw['license'], 800);
        if (raw['runtime'] != 'python_json.v1' ||
            raw['integrity'] != 'verified_current' ||
            raw['installed'] != true) {
          _bad();
        }
        if (raw['active_version'] != null) {
          _match(raw['active_version'], r'v-[0-9a-f]{24}');
        }
        _bool(raw['approved']);
        _bool(raw['suite_configured']);
        _bool(raw['rollback_eligible']);
        final reports = _list(raw['reports'], 8);
        _int(raw['reports_total'], reports.length, 256);
        final ids = <String>{};
        for (final v in reports) {
          final report = _object(v, {'report_id', 'passed', 'current_binding'});
          final id = _match(report['report_id'], r'rep-[A-Za-z0-9-]{1,60}');
          if (!ids.add(id)) _bad();
          _bool(report['passed']);
          if (_bool(report['current_binding']) && report['passed'] != true) {
            _bad();
          }
        }
        if (raw['enabled'] == true &&
            (raw['approved'] != true ||
                raw['active_version'] != raw['version_id'])) {
          _bad();
        }
      }
    }
    body = _freeze(raw) as Map<String, Object?>;
  }
  late final String kind;
  late final Map<String, Object?> body;
  String get identity =>
      (body[kind == 'native'
              ? 'name'
              : kind == 'skill'
              ? 'skill_id'
              : 'version_id']
          as String);
  String get label => (body[kind == 'plugin' ? 'plugin_id' : 'name'] as String);
  String get description => body['description'] as String;
  bool get enabled =>
      body[kind == 'native' ? 'effective_available' : 'enabled'] == true;
  Map<String, Object?> action(
    String operation, {
    String? report,
    bool reviewed = false,
  }) => capabilityAction(
    kind == 'skill'
        ? {
            'operation': operation,
            'skill_id': body['skill_id'],
            'digest': body['digest'],
          }
        : {
            'operation': operation,
            'plugin_id': body['plugin_id'],
            'version_id': body['version_id'],
            'source_sha256': body['source_sha256'],
            'manifest_sha256': body['manifest_sha256'],
            'report_id': report,
            'reviewed': reviewed,
          },
  );
}

class CapabilitySnapshot {
  CapabilitySnapshot.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'revision',
      'state_token',
      'section',
      'host_state',
      'disabled',
      'writable',
      'busy',
      'items',
      'next_after',
      'pending_count',
      'pending_operations',
      'change_boundary',
      'limits',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['change_boundary'] != 'native_owner_admission' ||
        !capabilitySections.contains(raw['section']) ||
        !{
          'configured',
          'not_configured',
          'unavailable',
        }.contains(raw['host_state'])) {
      _bad();
    }
    backend = _authority(raw['backend_id']);
    instance = _authority(raw['instance_id']);
    revision = _int(raw['revision'], 0);
    stateToken = _hash(raw['state_token']);
    section = raw['section'] as String;
    hostState = raw['host_state'] as String;
    disabled = capabilityDisabled(raw['disabled']);
    writable = _bool(raw['writable']);
    busy = _bool(raw['busy']);
    pendingCount = _int(raw['pending_count'], 0, 10000);
    final operations = <Map<String, String>>[];
    String? previousKey;
    const bySection = {
      'tools': {'set_tools'},
      'skills': {'enable_skill', 'disable_skill'},
      'plugins': {
        'evaluate_plugin',
        'promote_plugin',
        'disable_plugin',
        'rollback_plugin',
      },
    };
    for (final value in _list(raw['pending_operations'], 10000)) {
      final row = _object(value, {'idempotency_key', 'operation'});
      final key = capabilityChangeKey(row['idempotency_key']);
      final operation = _text(row['operation'], 32);
      if (!bySection[section]!.contains(operation) ||
          (previousKey != null && previousKey.compareTo(key) >= 0)) {
        _bad();
      }
      operations.add(
        Map.unmodifiable({'idempotency_key': key, 'operation': operation}),
      );
      previousKey = key;
    }
    if (operations.length != pendingCount) _bad();
    pendingOperations = List.unmodifiable(operations);
    items = List.unmodifiable(
      _list(raw['items'], 8).map(CapabilityItem.fromJson),
    );
    final ids = <String>{};
    for (final item in items) {
      if (item.kind !=
              (section == 'tools'
                  ? 'native'
                  : section == 'skills'
                  ? 'skill'
                  : 'plugin') ||
          !ids.add(item.identity)) {
        _bad();
      }
      if (item.kind == 'native' &&
          item.body['requested_enabled'] == disabled.contains(item.identity)) {
        _bad();
      }
    }
    if (hostState != 'configured' && items.isNotEmpty) _bad();
    nextAfter = raw['next_after'] == null
        ? null
        : _int(raw['next_after'], 1, 1000000);
    if (nextAfter != null && items.isEmpty) _bad();
    final limits = _object(raw['limits'], {'page', 'receipts'});
    if (limits['page'] != 8 || limits['receipts'] != 10000) _bad();
  }
  late final String backend, instance, stateToken, section, hostState;
  late final int revision, pendingCount;
  late final bool writable, busy;
  late final List<String> disabled;
  late final List<CapabilityItem> items;
  late final List<Map<String, String>> pendingOperations;
  late final int? nextAfter;
  bool sameBase(CapabilitySnapshot other) =>
      backend == other.backend &&
      instance == other.instance &&
      revision == other.revision &&
      stateToken == other.stateToken &&
      section == other.section;
  Map<String, Object?> change(Map<String, Object?> action, String key) =>
      capabilityChange({
        'schema_version': '1.0',
        'expected_backend': backend,
        'expected_instance': instance,
        'expected_revision': revision,
        'expected_state': stateToken,
        'idempotency_key': key,
        'action': action,
      });
}

class CapabilityReceipt {
  CapabilityReceipt.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'expected_backend',
      'expected_instance',
      'expected_revision',
      'expected_state',
      'idempotency_key',
      'action',
      'status',
      'revision',
      'result',
      'change_boundary',
    });
    if (raw['status'] != 'capability_change_recorded' ||
        raw['change_boundary'] != 'native_owner_admission') {
      _bad();
    }
    request = capabilityChange({
      for (final key in [
        'schema_version',
        'expected_backend',
        'expected_instance',
        'expected_revision',
        'expected_state',
        'idempotency_key',
        'action',
      ])
        key: raw[key],
    });
    revision = _int(raw['revision'], 1);
    if (revision != (request['expected_revision'] as int) + 1) _bad();
    final action = request['action'] as Map<String, Object?>,
        op = action['operation'];
    final Map<String, Object?> result;
    if (op == 'set_tools') {
      result = _object(raw['result'], {'operation', 'disabled'});
      if (jsonEncode(capabilityAction(result)) != jsonEncode(action)) _bad();
    } else if ((op as String).endsWith('skill')) {
      result = _object(raw['result'], {
        'operation',
        'skill_id',
        'digest',
        'enabled',
      });
      if (result['skill_id'] != action['skill_id'] ||
          result['digest'] != action['digest'] ||
          _bool(result['enabled']) != (op == 'enable_skill')) {
        _bad();
      }
    } else if (op == 'evaluate_plugin') {
      result = _object(raw['result'], {
        'operation',
        'version_id',
        'report_id',
        'passed',
        'cases',
      });
      if (result['version_id'] != action['version_id']) _bad();
      _match(result['report_id'], r'rep-[A-Za-z0-9-]{1,60}');
      _bool(result['passed']);
      _int(result['cases'], 1, 64);
    } else {
      result = _object(raw['result'], {
        'operation',
        'plugin_id',
        'version_id',
        'enabled',
      });
      if (result['plugin_id'] != action['plugin_id'] ||
          result['version_id'] != action['version_id'] ||
          _bool(result['enabled']) != (op != 'disable_plugin')) {
        _bad();
      }
    }
    if (result['operation'] != op) _bad();
  }
  late final Map<String, Object?> request;
  late final int revision;
  String get key => request['idempotency_key'] as String;
  bool matches(Map<String, Object?> other) =>
      jsonEncode(request) == jsonEncode(capabilityChange(other));
}

class CapabilityLookup {
  CapabilityLookup.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'request',
      'backend_id',
      'instance_id',
      'current_revision',
      'status',
      'mutation_fenced',
      'receipt',
    });
    if (raw['schema_version'] != '1.0' ||
        !{'recorded', 'pending', 'unobserved'}.contains(raw['status'])) {
      _bad();
    }
    request = capabilityChange(raw['request']);
    if (_authority(raw['backend_id']) != request['expected_backend']) _bad();
    final instance = _authority(raw['instance_id']),
        revision = _int(raw['current_revision'], 0);
    status = raw['status'] as String;
    fenced = _bool(raw['mutation_fenced']);
    receipt = raw['receipt'] == null
        ? null
        : CapabilityReceipt.fromJson(raw['receipt']);
    if ((status == 'recorded') != (receipt != null) ||
        (receipt != null &&
            (!receipt!.matches(request) || receipt!.revision > revision)) ||
        fenced !=
            (status == 'unobserved' &&
                (instance != request['expected_instance'] ||
                    revision > (request['expected_revision'] as int))) ||
        (status == 'pending' &&
            revision <= (request['expected_revision'] as int))) {
      _bad();
    }
  }
  late final Map<String, Object?> request;
  late final String status;
  late final bool fenced;
  late final CapabilityReceipt? receipt;
  bool matches(Map<String, Object?> other) =>
      jsonEncode(request) == jsonEncode(capabilityChange(other));
}

Object? capabilityJsonDecode(List<int> bytes) {
  if (bytes.length > 65536) _bad();
  final value = _CapabilityJsonReader(bytes).read();
  void check(Object? v) {
    if (v is String) {
      _text(v, 65536, empty: true);
    } else if (v is Map<String, Object?>) {
      for (final e in v.entries) {
        check(e.key);
        check(e.value);
      }
    } else if (v is List) {
      for (final child in v) {
        check(child);
      }
    }
  }

  check(value);
  return value;
}

class _CapabilityJsonReader {
  _CapabilityJsonReader(List<int> bytes)
    : text = utf8.decode(bytes, allowMalformed: false) {
    if (bytes.length > 64 * 1024) _bad();
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
    if (depth > 16 || ++values > 65536) _bad();
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
        if (list.length >= 65536) _bad();
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
