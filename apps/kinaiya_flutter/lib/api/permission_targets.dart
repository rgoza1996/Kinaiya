/// Passive native and exact MCP identities. Inventory never grants a tool.
library;

import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'tool_permissions.dart';

const nativePermissionTargets = {
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
};

Never _bad() =>
    throw const FormatException('Invalid permission target response');
Map<String, Object?> _object(Object? value, Set<String> keys) {
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !value.keys.every(keys.contains)) {
    _bad();
  }
  return value;
}

String _hash(Object? value) {
  if (value is! String ||
      value.length != 64 ||
      !_fullMatch(r'[a-f0-9]{64}', value)) {
    _bad();
  }
  return value;
}

bool _fullMatch(String pattern, String value) {
  final match = RegExp(pattern).matchAsPrefix(value);
  return match != null && match.end == value.length;
}

String _key(Object? value) {
  if (value is! String ||
      value.isEmpty ||
      value.length > 128 ||
      !_fullMatch(r'[A-Za-z0-9:._-]{1,128}', value)) {
    _bad();
  }
  return value;
}

ToolPermissionScope _scope(Object? value) {
  final scope = ToolPermissionScope.fromJson(value);
  if (scope.workspaceId != null) _hash(scope.workspaceId);
  return scope;
}

int _integer(Object? value, int min, int max) {
  if (value is! int || value < min || value > max) _bad();
  return value;
}

String? _mode(Object? value) {
  if (value != null && !permissionModes.contains(value)) _bad();
  return value as String?;
}

String _modeRequired(Object? value) => _mode(value) ?? _bad();
bool _boolean(Object? value) {
  if (value is! bool) _bad();
  return value;
}

class PermissionTarget {
  PermissionTarget.fromJson(Object? value) {
    if (value is! Map<String, Object?>) _bad();
    final kind = value['kind'];
    final raw = _object(value, {
      'kind',
      'name',
      if (kind == 'mcp') 'server_id',
    });
    if (kind != 'native' && kind != 'mcp') _bad();
    name = raw['name'] is String ? raw['name'] as String : _bad();
    if (kind == 'native') {
      if (!nativePermissionTargets.contains(name)) _bad();
      serverId = null;
    } else {
      serverId = raw['server_id'] is String
          ? raw['server_id'] as String
          : _bad();
      if (name.isEmpty ||
          name.length > 128 ||
          serverId!.isEmpty ||
          serverId!.length > 128 ||
          !_fullMatch(r'[A-Za-z0-9_.-]{1,128}', name) ||
          !_fullMatch(r'[A-Za-z0-9_.-]{1,128}', serverId!)) {
        _bad();
      }
    }
    // Key order matches native canonical JSON; all target identities are ASCII.
    body = Map.unmodifiable({
      'kind': kind,
      'name': name,
      if (serverId != null) 'server_id': serverId,
    });
    id = sha256.convert(utf8.encode(jsonEncode(body))).toString();
  }
  late final String name, id;
  late final String? serverId;
  late final Map<String, Object?> body;
  bool get isMcp => serverId != null;
  String get label => isMcp ? '$serverId / $name' : name.replaceAll('_', ' ');
  bool matches(PermissionTarget other) =>
      id == other.id && jsonEncode(body) == jsonEncode(other.body);
}

class PermissionTargetBinding {
  PermissionTargetBinding.fromJson(Object? value, PermissionTarget target) {
    final raw = _object(
      value,
      target.isMcp
          ? {'config_sha256', 'tool_sha256'}
          : {'protocol_version', 'schema_sha256'},
    );
    if (target.isMcp) {
      body = Map.unmodifiable({
        'config_sha256': _hash(raw['config_sha256']),
        'tool_sha256': raw['tool_sha256'] == null
            ? null
            : _hash(raw['tool_sha256']),
      });
    } else {
      if (raw['protocol_version'] != '1.0') _bad();
      body = Map.unmodifiable({
        'protocol_version': '1.0',
        'schema_sha256': _hash(raw['schema_sha256']),
      });
    }
  }
  late final Map<String, Object?> body;
  bool get observed =>
      !body.containsKey('tool_sha256') || body['tool_sha256'] != null;
  bool matches(PermissionTargetBinding other) =>
      jsonEncode(body) == jsonEncode(other.body);
}

class PermissionTargetRow {
  PermissionTargetRow.fromJson(Object? value) {
    final raw = _object(value, {
      'target_id',
      'target',
      'binding',
      'binding_state',
      'stored_binding',
      'rule_api',
      'available',
      'editable',
      'reason',
      'override',
      'effective',
      'source',
      'source_scope_id',
      'rule_id',
      'rule_revision',
      'hard_denied',
      'host_mode',
    });
    target = PermissionTarget.fromJson(raw['target']);
    if (_hash(raw['target_id']) != target.id) _bad();
    binding = raw['binding'] == null
        ? null
        : PermissionTargetBinding.fromJson(raw['binding'], target);
    storedBinding = raw['stored_binding'] == null
        ? null
        : PermissionTargetBinding.fromJson(raw['stored_binding'], target);
    if (!{'current', 'changed', 'unavailable'}.contains(raw['binding_state'])) {
      _bad();
    }
    bindingState = raw['binding_state'] as String;
    if (!{null, 'legacy', 'target'}.contains(raw['rule_api'])) _bad();
    ruleApi = raw['rule_api'] as String?;
    available = _boolean(raw['available']);
    editable = _boolean(raw['editable']);
    hardDenied = _boolean(raw['hard_denied']);
    if (raw['reason'] is! String ||
        (raw['reason'] as String).isEmpty ||
        (raw['reason'] as String).runes.length > 512) {
      _bad();
    }
    reason = raw['reason'] as String;
    override = _mode(raw['override']);
    effective = _modeRequired(raw['effective']);
    hostMode = _modeRequired(raw['host_mode']);
    if (!{...permissionScopeKinds, 'host'}.contains(raw['source'])) _bad();
    source = raw['source'] as String;
    sourceScopeId = raw['source_scope_id'] == null
        ? null
        : _hash(raw['source_scope_id']);
    ruleId = raw['rule_id'] == null ? null : _hash(raw['rule_id']);
    ruleRevision = raw['rule_revision'] == null
        ? null
        : _integer(raw['rule_revision'], 1, permissionRevisionLimit);
    if ((ruleId == null) != (ruleRevision == null) ||
        (hardDenied && effective != 'deny') ||
        (available && binding == null) ||
        (ruleApi == null && editable) ||
        (ruleApi == 'legacy' &&
            (target.isMcp || !permissionTools.contains(target.name))) ||
        (ruleApi == 'target' && !target.isMcp && target.name != 'search')) {
      _bad();
    }
  }
  late final PermissionTarget target;
  late final PermissionTargetBinding? binding, storedBinding;
  late final String bindingState, reason, effective, hostMode, source;
  late final String? ruleApi, override, sourceScopeId, ruleId;
  late final int? ruleRevision;
  late final bool available, editable, hardDenied;
}

class PermissionTargetsPage {
  PermissionTargetsPage.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'revision',
      'state_token',
      'instance_id',
      'scope',
      'scope_id',
      'scope_live',
      'change_boundary',
      'limits',
      'targets',
      'next_after',
    });
    if (raw['schema_version'] != '1.1' ||
        raw['change_boundary'] != 'next_action_admission') {
      _bad();
    }
    revision = _integer(raw['revision'], 0, permissionRevisionLimit);
    stateToken = _hash(raw['state_token']);
    final instance = raw['instance_id'];
    if (instance is! String ||
        instance.length != 36 ||
        !_fullMatch(
          r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
          instance,
        )) {
      _bad();
    }
    instanceId = instance;
    scope = _scope(raw['scope']);
    scopeId = _hash(raw['scope_id']);
    scopeLive = _boolean(raw['scope_live']);
    final limits = _object(raw['limits'], {'rules', 'receipts'});
    if (limits['rules'] != 1024 || limits['receipts'] != 10000) _bad();
    final rows = raw['targets'];
    if (rows is! List || rows.length > 20) _bad();
    targets = List.unmodifiable(rows.map(PermissionTargetRow.fromJson));
    final ids = targets.map((row) => row.target.id).toList();
    for (var i = 1; i < ids.length; i++) {
      if (ids[i - 1].compareTo(ids[i]) >= 0) _bad();
    }
    nextAfter = raw['next_after'] == null ? null : _hash(raw['next_after']);
    if (nextAfter != null && (ids.length != 20 || nextAfter != ids.last)) {
      _bad();
    }
  }
  late final int revision;
  late final String stateToken, instanceId, scopeId;
  late final ToolPermissionScope scope;
  late final bool scopeLive;
  late final List<PermissionTargetRow> targets;
  late final String? nextAfter;
  Map<String, Object?> change(
    PermissionTargetRow row,
    String? mode,
    String key,
  ) {
    final binding = mode == null
        ? row.storedBinding ?? row.binding
        : row.binding;
    if (binding == null ||
        row.ruleApi != 'target' ||
        !row.editable ||
        (!scopeLive && mode != null) ||
        (mode != null &&
            mode != 'deny' &&
            (!row.available || !binding.observed))) {
      _bad();
    }
    return permissionTargetChange({
      'schema_version': '1.1',
      'expected_revision': revision,
      'expected_state': stateToken,
      'idempotency_key': key,
      'scope': scope.body,
      'target': row.target.body,
      'binding': binding.body,
      'mode': mode,
    });
  }
}

Map<String, Object?> permissionTargetChange(Object? value) {
  final raw = _object(value, {
    'schema_version',
    'expected_revision',
    'expected_state',
    'idempotency_key',
    'scope',
    'target',
    'binding',
    'mode',
  });
  if (raw['schema_version'] != '1.1') _bad();
  final target = PermissionTarget.fromJson(raw['target']);
  if (!target.isMcp && target.name != 'search') _bad();
  return Map.unmodifiable({
    'schema_version': '1.1',
    'expected_revision': _integer(
      raw['expected_revision'],
      0,
      permissionRevisionLimit - 1,
    ),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': _key(raw['idempotency_key']),
    'scope': _scope(raw['scope']).body,
    'target': target.body,
    'binding': PermissionTargetBinding.fromJson(raw['binding'], target).body,
    'mode': _mode(raw['mode']),
  });
}

class PermissionTargetReceipt {
  PermissionTargetReceipt.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'status',
      'idempotency_key',
      'scope',
      'scope_id',
      'target',
      'binding',
      'mode',
      'expected_revision',
      'expected_state',
      'revision',
      'change_boundary',
    });
    if (raw['status'] != 'permission_target_saved' ||
        raw['change_boundary'] != 'next_action_admission') {
      _bad();
    }
    body = permissionTargetChange({
      for (final field in raw.entries)
        if (!{
          'status',
          'scope_id',
          'revision',
          'change_boundary',
        }.contains(field.key))
          field.key: field.value,
    });
    scopeId = _hash(raw['scope_id']);
    revision = _integer(raw['revision'], 1, permissionRevisionLimit);
    if (revision != (body['expected_revision'] as int) + 1) _bad();
  }
  late final Map<String, Object?> body;
  late final String scopeId;
  late final int revision;
  bool matches(Map<String, Object?> request, String nativeScopeId) =>
      scopeId == nativeScopeId &&
      jsonEncode(body) == jsonEncode(permissionTargetChange(request));
}
