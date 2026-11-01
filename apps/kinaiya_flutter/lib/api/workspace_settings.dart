/// Owner workspace selection. Saved history does not prove current application.
library;

import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'tool_permissions.dart' show permissionJsonDecode;

const workspaceRevisionLimit = 9007199254740990;
Never _bad() => throw const FormatException('Invalid workspace metadata');

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

String _authority(Object? value) => _match(value, r'[a-f0-9]{32}');
String _hash(Object? value) => _match(value, r'[a-f0-9]{64}');
String _workspaceId(String root) =>
    sha256.convert(utf8.encode(jsonEncode({'workspace': root}))).toString();
String workspaceChangeKey(Object? value) =>
    _match(value, r'[A-Za-z0-9:._-]{1,128}');
int _int(Object? value, int min, [int max = workspaceRevisionLimit]) {
  if (value is! int || value < min || value > max) _bad();
  return value;
}

bool _bool(Object? value) {
  if (value is! bool) _bad();
  return value;
}

String? _error(Object? value) {
  if (value == null) return null;
  if (value is! String || value.isEmpty || value.runes.length > 256) _bad();
  return value;
}

void _unicode(String value) {
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
}

int _compareUnicode(String left, String right) {
  final a = left.runes.iterator, b = right.runes.iterator;
  while (true) {
    final moreA = a.moveNext(), moreB = b.moveNext();
    if (!moreA || !moreB) {
      return moreA == moreB
          ? 0
          : moreA
          ? 1
          : -1;
    }
    if (a.current != b.current) return a.current.compareTo(b.current);
  }
}

String _path(Object? value, {required bool relative}) {
  if (value is! String ||
      value.isEmpty ||
      utf8.encode(value).length > (relative ? 512 : 4096) ||
      value.codeUnits.any((c) => c < 32 || c == 127)) {
    _bad();
  }
  if (relative && value == '.') return value;
  _unicode(value);
  if (value.startsWith('/') == relative ||
      (value != '/' &&
          value
              .split('/')
              .skip(relative ? 0 : 1)
              .any((part) => part.isEmpty || part == '.' || part == '..'))) {
    _bad();
  }
  return value;
}

class WorkspaceSelection {
  factory WorkspaceSelection.fromInput(String root, String readonlyLines) {
    final paths = readonlyLines.split('\n').where((p) => p.isNotEmpty).toList()
      ..sort(_compareUnicode);
    return WorkspaceSelection.fromJson({'root': root, 'readonly_paths': paths});
  }
  WorkspaceSelection.fromJson(Object? value) {
    final raw = _object(value, {'root', 'readonly_paths'});
    root = _path(raw['root'], relative: false);
    final rows = raw['readonly_paths'];
    if (rows is! List || rows.length > 32) _bad();
    readonly = List.unmodifiable(rows.map((p) => _path(p, relative: true)));
    for (var i = 1; i < readonly.length; i++) {
      if (_compareUnicode(readonly[i - 1], readonly[i]) >= 0) _bad();
    }
  }
  late final String root;
  late final List<String> readonly;
  Map<String, Object?> get body =>
      Map.unmodifiable({'root': root, 'readonly_paths': readonly});
  bool matches(WorkspaceSelection other) =>
      jsonEncode(body) == jsonEncode(other.body);
}

class WorkspacePin {
  WorkspacePin.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'workspace_id',
      'root_identity',
      'settings_revision',
      'source',
      'shell_backend',
      'mode',
    });
    if (raw['schema_version'] != '1.0' ||
        !{'host', 'owner_saved'}.contains(raw['source']) ||
        raw['shell_backend'] != 'bubblewrap' ||
        raw['mode'] != 'direct') {
      _bad();
    }
    workspaceId = _hash(raw['workspace_id']);
    rootIdentity = _hash(raw['root_identity']);
    revision = _int(raw['settings_revision'], 0);
    source = raw['source'] as String;
  }
  late final String workspaceId, rootIdentity, source;
  late final int revision;
}

class WorkspaceActive {
  WorkspaceActive.fromJson(Object? value) {
    final raw = _object(value, {'settings', 'pin'});
    settings = WorkspaceSelection.fromJson(raw['settings']);
    pin = WorkspacePin.fromJson(raw['pin']);
    if (pin.workspaceId != _workspaceId(settings.root)) {
      _bad();
    }
  }
  late final WorkspaceSelection settings;
  late final WorkspacePin pin;
}

class WorkspaceSettingsSnapshot {
  WorkspaceSettingsSnapshot.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'revision',
      'state_token',
      'writable',
      'saved_settings',
      'saved_root_identity',
      'desired_available',
      'desired_error',
      'active',
      'active_matches_saved',
      'active_error',
      'busy',
      'operation_state',
      'host_guards',
      'execution',
      'change_boundary',
      'limits',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['change_boundary'] != 'settled_service') {
      _bad();
    }
    backend = _authority(raw['backend_id']);
    instance = _authority(raw['instance_id']);
    revision = _int(raw['revision'], 0);
    stateToken = _hash(raw['state_token']);
    writable = _bool(raw['writable']);
    saved = WorkspaceSelection.fromJson(raw['saved_settings']);
    savedRootIdentity = _hash(raw['saved_root_identity']);
    desiredAvailable = _bool(raw['desired_available']);
    desiredError = _error(raw['desired_error']);
    active = raw['active'] == null
        ? null
        : WorkspaceActive.fromJson(raw['active']);
    activeMatchesSaved = _bool(raw['active_matches_saved']);
    activeError = _error(raw['active_error']);
    busy = _bool(raw['busy']);
    if (active != null && active!.pin.revision > revision) _bad();
    if (activeMatchesSaved &&
        (active == null ||
            !active!.settings.matches(saved) ||
            active!.pin.rootIdentity != savedRootIdentity ||
            active!.pin.revision != revision)) {
      _bad();
    }
    final ops = raw['operation_state'];
    if (ops == null) {
      operationState = null;
    } else {
      final fields = _object(ops, {
        'closed',
        'tasks',
        'workers',
        'processes',
        'retirements',
        'submit_preflights',
        'submit_operations',
        'pending_submits',
        'ecosystem_operations',
        'cleanup_pending',
        'cleanup_failed',
      });
      operationState = Map.unmodifiable({
        'closed': _bool(fields['closed']),
        for (final entry in fields.entries)
          if (entry.key != 'closed') entry.key: _int(entry.value, 0, 100000),
      });
    }
    final guards = _object(raw['host_guards'], {
      'readonly',
      'protected',
      'control',
      'evaluation',
      'credentials',
    });
    var pathCount = 0;
    hostGuards = Map.unmodifiable({
      for (final entry in guards.entries)
        entry.key: (() {
          final rows = entry.value;
          if (rows is! List || (pathCount += rows.length) > 256) _bad();
          return List<String>.unmodifiable(
            rows.map((p) => _path(p, relative: false)),
          );
        })(),
    });
    final execution = _object(raw['execution'], {
      'shell_backend',
      'mode',
      'binary_detected',
      'isolation_verified',
      'supported_backends',
      'worktree_supported',
    });
    if (execution['shell_backend'] != 'bubblewrap' ||
        execution['mode'] != 'direct' ||
        execution['isolation_verified'] != false ||
        execution['worktree_supported'] != false ||
        execution['supported_backends'] is! List ||
        jsonEncode(execution['supported_backends']) != '["bubblewrap"]') {
      _bad();
    }
    binaryDetected = _bool(execution['binary_detected']);
    final limits = _object(raw['limits'], {
      'readonly_paths',
      'receipts',
      'path_bytes',
      'relative_path_bytes',
    });
    if (limits['readonly_paths'] != 32 ||
        limits['receipts'] != 10000 ||
        limits['path_bytes'] != 4096 ||
        limits['relative_path_bytes'] != 512) {
      _bad();
    }
  }
  late final String backend, instance, stateToken, savedRootIdentity;
  late final int revision;
  late final bool writable,
      desiredAvailable,
      activeMatchesSaved,
      busy,
      binaryDetected;
  late final String? desiredError, activeError;
  late final WorkspaceSelection saved;
  late final WorkspaceActive? active;
  late final Map<String, Object?>? operationState;
  late final Map<String, List<String>> hostGuards;
  bool sameBase(WorkspaceSettingsSnapshot other) =>
      backend == other.backend &&
      instance == other.instance &&
      revision == other.revision &&
      stateToken == other.stateToken;
  Map<String, Object?> change(WorkspaceSelection settings, String key) =>
      workspaceSettingsChange({
        'schema_version': '1.0',
        'expected_backend': backend,
        'expected_instance': instance,
        'expected_revision': revision,
        'expected_state': stateToken,
        'idempotency_key': key,
        'settings': settings.body,
      });
}

Map<String, Object?> workspaceSettingsChange(Object? value) {
  final raw = _object(value, {
    'schema_version',
    'expected_backend',
    'expected_instance',
    'expected_revision',
    'expected_state',
    'idempotency_key',
    'settings',
  });
  if (raw['schema_version'] != '1.0') _bad();
  return Map.unmodifiable({
    'schema_version': '1.0',
    'expected_backend': _authority(raw['expected_backend']),
    'expected_instance': _authority(raw['expected_instance']),
    'expected_revision': _int(
      raw['expected_revision'],
      0,
      workspaceRevisionLimit - 1,
    ),
    'expected_state': _hash(raw['expected_state']),
    'idempotency_key': workspaceChangeKey(raw['idempotency_key']),
    'settings': WorkspaceSelection.fromJson(raw['settings']).body,
  });
}

class WorkspaceSettingsReceipt {
  WorkspaceSettingsReceipt.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'status',
      'backend_id',
      'instance_id',
      'idempotency_key',
      'expected_revision',
      'expected_state',
      'revision',
      'settings',
      'root_identity',
      'workspace_id',
      'change_boundary',
    });
    if (raw['schema_version'] != '1.0' ||
        raw['status'] != 'workspace_saved' ||
        raw['change_boundary'] != 'settled_service') {
      _bad();
    }
    request = workspaceSettingsChange({
      'schema_version': '1.0',
      'expected_backend': raw['backend_id'],
      'expected_instance': raw['instance_id'],
      'expected_revision': raw['expected_revision'],
      'expected_state': raw['expected_state'],
      'idempotency_key': raw['idempotency_key'],
      'settings': raw['settings'],
    });
    revision = _int(raw['revision'], 1);
    rootIdentity = _hash(raw['root_identity']);
    workspaceId = _hash(raw['workspace_id']);
    if (revision != (request['expected_revision'] as int) + 1 ||
        workspaceId !=
            _workspaceId(
              WorkspaceSelection.fromJson(request['settings']).root,
            )) {
      _bad();
    }
  }
  late final Map<String, Object?> request;
  late final int revision;
  late final String rootIdentity, workspaceId;
  String get key => request['idempotency_key'] as String;
  bool matches(Map<String, Object?> other) =>
      jsonEncode(request) == jsonEncode(workspaceSettingsChange(other));
}

class WorkspaceSettingsLookup {
  WorkspaceSettingsLookup.fromJson(Object? value) {
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
    request = workspaceSettingsChange(raw['request']);
    if (_authority(raw['backend_id']) != request['expected_backend']) _bad();
    receipt = raw['receipt'] == null
        ? null
        : WorkspaceSettingsReceipt.fromJson(raw['receipt']);
    fenced = _bool(raw['mutation_fenced']);
    if ((raw['status'] == 'recorded') != (receipt != null) ||
        (receipt != null && (!receipt!.matches(request) || !fenced))) {
      _bad();
    }
  }
  late final Map<String, Object?> request;
  late final WorkspaceSettingsReceipt? receipt;
  late final bool fenced;
  bool matches(Map<String, Object?> other) =>
      jsonEncode(request) == jsonEncode(workspaceSettingsChange(other));
}

Object? workspaceSettingsJsonDecode(List<int> bytes) {
  final value = permissionJsonDecode(bytes);
  void check(Object? child) {
    if (child is String) {
      for (var i = 0; i < child.length; i++) {
        final c = child.codeUnitAt(i);
        if (c >= 0xd800 && c <= 0xdbff) {
          if (++i >= child.length ||
              child.codeUnitAt(i) < 0xdc00 ||
              child.codeUnitAt(i) > 0xdfff) {
            _bad();
          }
        } else if (c >= 0xdc00 && c <= 0xdfff) {
          _bad();
        }
      }
    } else if (child is Map) {
      for (final entry in child.entries) {
        check(entry.key);
        check(entry.value);
      }
    } else if (child is List) {
      for (final item in child) {
        check(item);
      }
    }
  }

  check(value);
  return value;
}
