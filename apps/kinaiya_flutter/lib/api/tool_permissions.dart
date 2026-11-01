/// Native owner permission metadata. Scope objects returned by the host are the
/// only scope objects that may be used in a mutation.
library;

import 'dart:convert';

const permissionRevisionLimit = 9007199254740990;
const permissionModes = ['allow', 'ask', 'deny'];
const permissionTools = ['read_file', 'write_file', 'shell'];
const permissionScopeKinds = ['default', 'workspace', 'session', 'task'];
const _boundary = 'next_action_admission';

Never _bad() => throw const FormatException('Invalid tool permission response');

Map<String, Object?> _object(Object? value) {
  if (value is! Map<String, Object?>) _bad();
  return value;
}

void _keys(Map<String, Object?> value, Set<String> fields) {
  if (value.length != fields.length || !fields.containsAll(value.keys)) _bad();
}

void _version(Map<String, Object?> value) {
  if (value['schema_version'] != '1.0') _bad();
}

String _id(Object? value) {
  if (value is! String ||
      value.isEmpty ||
      value.runes.length > 128 ||
      value.codeUnits.contains(0)) {
    _bad();
  }
  return value;
}

String _hash(Object? value) {
  if (value is! String || !RegExp(r'^[a-f0-9]{64}$').hasMatch(value)) _bad();
  return value;
}

int _integer(Object? value, int min, int max) {
  if (value is! int || value < min || value > max) _bad();
  return value;
}

String permissionKey(Object? value) {
  if (value is! String ||
      !RegExp(r'^[A-Za-z0-9:._-]{1,128}$').hasMatch(value)) {
    _bad();
  }
  return value;
}

String _mode(Object? value) {
  if (!permissionModes.contains(value)) _bad();
  return value as String;
}

String _tool(Object? value) {
  if (!permissionTools.contains(value)) _bad();
  return value as String;
}

class ToolPermissionScope {
  ToolPermissionScope.fromJson(Object? value) {
    final raw = _object(value), type = raw['kind'];
    if (!permissionScopeKinds.contains(type)) _bad();
    kind = type as String;
    final fields = {'kind'};
    if (kind != 'default') fields.add('workspace_id');
    if (kind == 'session' || kind == 'task') {
      fields.addAll(['session_id', 'thread_id', 'epoch']);
    }
    if (kind == 'task') fields.addAll(['task_id', 'objective_revision']);
    _keys(raw, fields);
    workspaceId = kind == 'default' ? null : _hash(raw['workspace_id']);
    final sessionScope = kind == 'session' || kind == 'task';
    sessionId = sessionScope ? _id(raw['session_id']) : null;
    threadId = sessionScope ? _id(raw['thread_id']) : null;
    epoch = sessionScope ? _integer(raw['epoch'], 1, 9007199254740991) : null;
    taskId = kind == 'task' ? _id(raw['task_id']) : null;
    objectiveRevision = kind == 'task'
        ? _integer(raw['objective_revision'], 1, 9007199254740991)
        : null;
    body = Map.unmodifiable({
      'kind': kind,
      if (workspaceId != null) 'workspace_id': workspaceId,
      if (sessionId != null) ...{
        'session_id': sessionId,
        'thread_id': threadId,
        'epoch': epoch,
      },
      if (taskId != null) ...{
        'task_id': taskId,
        'objective_revision': objectiveRevision,
      },
    });
    if (utf8.encode(jsonEncode(body)).length > 2048) _bad();
  }
  late final String kind;
  late final String? workspaceId;
  late final String? sessionId, threadId, taskId;
  late final int? epoch, objectiveRevision;
  late final Map<String, Object?> body;
  bool matches(ToolPermissionScope other) =>
      jsonEncode(body) == jsonEncode(other.body);
  String get label => switch (kind) {
    'default' => 'Default',
    'workspace' => 'Workspace',
    'session' => 'Session $sessionId · epoch $epoch',
    _ => 'Task $taskId · revision $objectiveRevision',
  };
}

class ToolPermissionRow {
  ToolPermissionRow.fromJson(Object? value) {
    final raw = _object(value);
    _keys(raw, {
      'tool',
      'override',
      'effective',
      'source',
      'source_scope_id',
      'rule_id',
      'rule_revision',
      'hard_denied',
      'host_mode',
    });
    tool = _tool(raw['tool']);
    override = raw['override'] == null ? null : _mode(raw['override']);
    effective = _mode(raw['effective']);
    if (!{...permissionScopeKinds, 'host'}.contains(raw['source'])) _bad();
    source = raw['source'] as String;
    sourceScopeId = raw['source_scope_id'] == null
        ? null
        : _hash(raw['source_scope_id']);
    ruleId = raw['rule_id'] == null ? null : _hash(raw['rule_id']);
    ruleRevision = raw['rule_revision'] == null
        ? null
        : _integer(raw['rule_revision'], 1, permissionRevisionLimit);
    if (raw['hard_denied'] is! bool) _bad();
    hardDenied = raw['hard_denied'] as bool;
    hostMode = _mode(raw['host_mode']);
    if (hardDenied && effective != 'deny') _bad();
  }
  late final String tool, effective, source, hostMode;
  late final String? override, sourceScopeId, ruleId;
  late final int? ruleRevision;
  late final bool hardDenied;
}

class ToolPermissionsSnapshot {
  ToolPermissionsSnapshot.fromJson(Object? value) {
    final raw = _object(value);
    _version(raw);
    _keys(raw, {
      'schema_version',
      'revision',
      'state_token',
      'instance_id',
      'scope',
      'scope_id',
      'scope_live',
      'change_boundary',
      'tools',
      'limits',
    });
    revision = _integer(raw['revision'], 0, permissionRevisionLimit);
    stateToken = _hash(raw['state_token']);
    final instance = raw['instance_id'];
    if (instance is! String ||
        !RegExp(
          r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
        ).hasMatch(instance)) {
      _bad();
    }
    instanceId = instance;
    scope = ToolPermissionScope.fromJson(raw['scope']);
    scopeId = _hash(raw['scope_id']);
    if (raw['scope_live'] is! bool || raw['change_boundary'] != _boundary) {
      _bad();
    }
    scopeLive = raw['scope_live'] as bool;
    final rows = raw['tools'];
    if (rows is! List || rows.length != 3) _bad();
    tools = List.unmodifiable(rows.map(ToolPermissionRow.fromJson));
    if (tools.map((row) => row.tool).toSet().length != 3) _bad();
    final limits = _object(raw['limits']);
    _keys(limits, {'rules', 'receipts'});
    if (limits['rules'] != 1024 || limits['receipts'] != 10000) _bad();
  }
  late final int revision;
  late final String stateToken, scopeId, instanceId;
  late final ToolPermissionScope scope;
  late final bool scopeLive;
  late final List<ToolPermissionRow> tools;

  Map<String, Object?> change(String tool, String? mode, String key) {
    _tool(tool);
    if (mode != null) _mode(mode);
    if ((!scopeLive && mode != null) || revision >= permissionRevisionLimit) {
      _bad();
    }
    return permissionChange({
      'schema_version': '1.0',
      'expected_revision': revision,
      'expected_state': stateToken,
      'idempotency_key': permissionKey(key),
      'scope': scope.body,
      'tool': tool,
      'mode': mode,
    });
  }
}

/// Validate and detach one exact request before a transport may see it.
Map<String, Object?> permissionChange(Object? value) {
  final raw = _object(value);
  _keys(raw, {
    'schema_version',
    'expected_revision',
    'expected_state',
    'idempotency_key',
    'scope',
    'tool',
    'mode',
  });
  _version(raw);
  return Map.unmodifiable({
    'schema_version': '1.0',
    'expected_revision': _integer(
      raw['expected_revision'],
      0,
      permissionRevisionLimit - 1,
    ),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': permissionKey(raw['idempotency_key']),
    'scope': ToolPermissionScope.fromJson(raw['scope']).body,
    'tool': _tool(raw['tool']),
    'mode': raw['mode'] == null ? null : _mode(raw['mode']),
  });
}

class ToolPermissionReceipt {
  ToolPermissionReceipt.fromJson(Object? value) {
    final raw = _object(value);
    _version(raw);
    _keys(raw, {
      'schema_version',
      'status',
      'idempotency_key',
      'scope',
      'scope_id',
      'tool',
      'mode',
      'expected_revision',
      'expected_state',
      'revision',
      'change_boundary',
    });
    if (raw['status'] != 'permission_saved' ||
        raw['change_boundary'] != _boundary) {
      _bad();
    }
    key = permissionKey(raw['idempotency_key']);
    scope = ToolPermissionScope.fromJson(raw['scope']);
    scopeId = _hash(raw['scope_id']);
    tool = _tool(raw['tool']);
    mode = raw['mode'] == null ? null : _mode(raw['mode']);
    expectedState = _hash(raw['expected_state']);
    expectedRevision = _integer(
      raw['expected_revision'],
      0,
      permissionRevisionLimit - 1,
    );
    revision = _integer(raw['revision'], 1, permissionRevisionLimit);
    if (revision != expectedRevision + 1) _bad();
  }
  late final String key, scopeId, tool, expectedState;
  late final String? mode;
  late final ToolPermissionScope scope;
  late final int expectedRevision, revision;
  bool matches(Map<String, Object?> request, String nativeScopeId) =>
      key == request['idempotency_key'] &&
      scopeId == nativeScopeId &&
      scope.matches(ToolPermissionScope.fromJson(request['scope'])) &&
      tool == request['tool'] &&
      mode == request['mode'] &&
      expectedRevision == request['expected_revision'] &&
      expectedState == request['expected_state'];
}

class SavedToolPermissionScope {
  SavedToolPermissionScope.fromJson(Object? value) {
    final raw = _object(value);
    _keys(raw, {'scope_id', 'scope'});
    scopeId = _hash(raw['scope_id']);
    scope = ToolPermissionScope.fromJson(raw['scope']);
  }
  late final String scopeId;
  late final ToolPermissionScope scope;
}

class ToolPermissionScopesPage {
  ToolPermissionScopesPage.fromJson(Object? value) {
    final raw = _object(value);
    _version(raw);
    _keys(raw, {'schema_version', 'scopes', 'next_after'});
    final rows = raw['scopes'];
    if (rows is! List || rows.length > 20) _bad();
    scopes = List.unmodifiable(rows.map(SavedToolPermissionScope.fromJson));
    var previous = '';
    for (final scope in scopes) {
      if (scope.scopeId.compareTo(previous) <= 0) _bad();
      previous = scope.scopeId;
    }
    nextAfter = raw['next_after'] == null ? null : _hash(raw['next_after']);
    if (nextAfter != null &&
        (scopes.isEmpty || nextAfter != scopes.last.scopeId)) {
      _bad();
    }
  }
  late final List<SavedToolPermissionScope> scopes;
  late final String? nextAfter;
}

/// Reject duplicate fields, unsafe integer spellings and deep/oversized JSON.
Object? permissionJsonDecode(List<int> bytes) =>
    _PermissionJsonReader(bytes).read();

class _PermissionJsonReader {
  _PermissionJsonReader(List<int> bytes)
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
