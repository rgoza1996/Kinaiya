/// Bounded, non-secret owner execution settings and inspection metadata.
library;

import 'agent_workspace.dart';

Map<String, Object?> _object(Object? value) {
  if (value is! Map<String, Object?>) throw const FormatException();
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) throw const FormatException();
  return value;
}

int _integer(Object? value, int min, int max) {
  if (value is! int || value < min || value > max) {
    throw const FormatException();
  }
  return value;
}

String _directoryText(Object? value) {
  if (value is! String || value.isEmpty || value.runes.length > 512) {
    throw const FormatException('Invalid execution directory');
  }
  // Native limits Unicode codepoints, rather than UTF16 code units. Reject
  // unpaired surrogates before JSON encoding can replace them.
  for (var i = 0; i < value.length; i++) {
    final c = value.codeUnitAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      if (++i >= value.length ||
          value.codeUnitAt(i) < 0xdc00 ||
          value.codeUnitAt(i) > 0xdfff) {
        throw const FormatException('Invalid execution directory');
      }
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      throw const FormatException('Invalid execution directory');
    }
  }
  return value;
}

/// Lexical Linux Path.as_posix normalization; this does not inspect a folder.
String canonicalRuntimeDirectory(Object? value) {
  final text = _directoryText(value);
  final parts = text.split('/');
  if (text.startsWith('/') ||
      parts.contains('..') ||
      text.runes.any((c) => c < 32 || c == 127)) {
    throw const FormatException('Invalid execution directory');
  }
  final retained = parts.where((p) => p.isNotEmpty && p != '.');
  return retained.isEmpty ? '.' : retained.join('/');
}

Map<String, Object?> _closed(Object? value, Set<String> keys) {
  final raw = _object(value);
  if (raw.length != keys.length || !raw.keys.every(keys.contains)) {
    throw const FormatException('Invalid execution settings proof');
  }
  return raw;
}

String _backend(Object? value) {
  if (value is! String || !RegExp(r'^[a-f0-9]{32}$').hasMatch(value)) {
    throw const FormatException('Invalid execution authority');
  }
  return value;
}

String _instance(Object? value) {
  if (value is! String ||
      !RegExp(
        r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$',
      ).hasMatch(value)) {
    throw const FormatException('Invalid execution authority');
  }
  return value;
}

const _settingKeys = {
  'file_writes',
  'shell_commands',
  'tool_network',
  'max_steps',
  'shell_timeout_s',
  'working_directory',
};

Map<String, Object?> _canonicalSettings(Object? value) {
  final raw = _closed(value, _settingKeys);
  final settings = AgentExecutionSettings.fromJson(raw).toJson();
  settings['working_directory'] = canonicalRuntimeDirectory(
    raw['working_directory'],
  );
  return Map.unmodifiable(settings);
}

bool _sameJson(Object? a, Object? b) {
  if (a is Map<String, Object?> && b is Map<String, Object?>) {
    return a.length == b.length &&
        a.keys.every((key) => b.containsKey(key) && _sameJson(a[key], b[key]));
  }
  return a.runtimeType == b.runtimeType && a == b;
}

class AgentRuntimeAuthority {
  AgentRuntimeAuthority.fromJson(Object? value) {
    final raw = _closed(value, {
      'mutation_version',
      'backend_id',
      'instance_id',
      'writable',
    });
    if (raw['mutation_version'] != '1.1') throw const FormatException();
    backendId = _backend(raw['backend_id']);
    instanceId = _instance(raw['instance_id']);
    writable = _flag(raw['writable']);
  }
  late final String backendId, instanceId;
  late final bool writable;

  static AgentRuntimeAuthority? supported(Object? value) {
    if (value is! Map || value['mutation_version'] != '1.1') return null;
    return AgentRuntimeAuthority.fromJson(value);
  }
}

/// Complete bound request, detached and normalized once before dispatch.
class AgentRuntimeChange {
  AgentRuntimeChange.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'expected_backend',
      'expected_instance',
      'expected_revision',
      'expected_state',
      'idempotency_key',
      'settings',
      'yolo',
    });
    if (raw['schema_version'] != '1.1' ||
        raw['expected_state'] is! String ||
        !RegExp(r'^[a-f0-9]{64}$').hasMatch(raw['expected_state'] as String) ||
        raw['idempotency_key'] is! String ||
        !RegExp(
          r'^[A-Za-z0-9:._-]{1,128}$',
        ).hasMatch(raw['idempotency_key'] as String)) {
      throw const FormatException('Invalid execution change');
    }
    _backend(raw['expected_backend']);
    _instance(raw['expected_instance']);
    _integer(raw['expected_revision'], 0, 9007199254740990);
    _flag(raw['yolo']);
    body = Map.unmodifiable({
      ...raw,
      'settings': _canonicalSettings(raw['settings']),
    });
  }
  late final Map<String, Object?> body;
  String get backendId => body['expected_backend'] as String;
  String get instanceId => body['expected_instance'] as String;
  String get key => body['idempotency_key'] as String;
  int get revision => body['expected_revision'] as int;
  bool matches(AgentRuntimeChange other) => _sameJson(body, other.body);
}

class AgentRuntimeReceipt {
  AgentRuntimeReceipt.fromJson(Object? value, AgentRuntimeChange original) {
    final raw = _closed(value, {
      'schema_version',
      'backend_id',
      'service_instance',
      'idempotency_key',
      'revision',
      'settings',
      'requested_yolo',
      'change_boundary',
      'status',
    });
    if (raw['schema_version'] != '1.1' ||
        raw['backend_id'] != original.backendId ||
        raw['service_instance'] != original.instanceId ||
        raw['idempotency_key'] != original.key ||
        raw['revision'] is! int ||
        raw['revision'] != original.revision + 1 ||
        raw['requested_yolo'] is! bool ||
        raw['requested_yolo'] != original.body['yolo'] ||
        raw['change_boundary'] != 'settled_service' ||
        raw['status'] != 'settings_saved') {
      throw const FormatException('Execution receipt does not match');
    }
    final settings = _canonicalSettings(raw['settings']);
    // A proof must already be canonical. Do not repair a mismatched echo.
    if (!_sameJson(raw['settings'], settings) ||
        !_sameJson(settings, original.body['settings'])) {
      throw const FormatException('Execution receipt does not match');
    }
    json = Map.unmodifiable({...raw, 'settings': settings});
  }
  late final Map<String, Object?> json;
}

class AgentRuntimeLookup {
  AgentRuntimeLookup.fromJson(Object? value, AgentRuntimeChange original) {
    final raw = _closed(value, {
      'schema_version',
      'backend_id',
      'request',
      'status',
      'receipt',
      'mutation_fenced',
    });
    if (raw['schema_version'] != '1.1' ||
        raw['backend_id'] != original.backendId ||
        raw['mutation_fenced'] is! bool ||
        !_sameJson(raw['request'], original.body)) {
      throw const FormatException('Execution lookup does not match');
    }
    mutationFenced = raw['mutation_fenced'] as bool;
    if (raw['status'] == 'recorded') {
      if (!mutationFenced) throw const FormatException();
      receipt = AgentRuntimeReceipt.fromJson(raw['receipt'], original);
    } else {
      if (raw['status'] != 'unobserved' || raw['receipt'] != null) {
        throw const FormatException('Invalid execution lookup');
      }
      receipt = null;
    }
    json = Map.unmodifiable({
      ...raw,
      'request': original.body,
      'receipt': receipt?.json,
    });
  }
  late final AgentRuntimeReceipt? receipt;
  late final bool mutationFenced;
  late final Map<String, Object?> json;
  bool get refused => receipt == null && mutationFenced;
}

class AgentExecutionSettings {
  AgentExecutionSettings.fromJson(Object? value) {
    final raw = _object(value);
    String mode(String name) {
      final result = raw[name];
      if (!const ['ask', 'allow', 'deny'].contains(result)) {
        throw const FormatException();
      }
      return result as String;
    }

    fileWrites = mode('file_writes');
    shellCommands = mode('shell_commands');
    toolNetwork = _flag(raw['tool_network']);
    maxSteps = _integer(raw['max_steps'], 1, 2000);
    shellTimeout = _integer(raw['shell_timeout_s'], 1, 120);
    workingDirectory = _directoryText(raw['working_directory']);
  }

  late final String fileWrites, shellCommands, workingDirectory;
  late final bool toolNetwork;
  late final int maxSteps, shellTimeout;

  Map<String, Object?> toJson() => {
    'file_writes': fileWrites,
    'shell_commands': shellCommands,
    'tool_network': toolNetwork,
    'max_steps': maxSteps,
    'shell_timeout_s': shellTimeout,
    'working_directory': workingDirectory,
  };

  bool matches(AgentExecutionSettings other) =>
      fileWrites == other.fileWrites &&
      shellCommands == other.shellCommands &&
      toolNetwork == other.toolNetwork &&
      maxSteps == other.maxSteps &&
      shellTimeout == other.shellTimeout &&
      workingDirectory == other.workingDirectory;
}

class ExecutionPathList {
  ExecutionPathList.fromJson(Object? value) {
    final raw = _object(value), list = raw['paths'];
    if (list is! List || list.length > 64) throw const FormatException();
    paths = List.unmodifiable(list.map((x) => workspaceText(x)));
    total = _integer(raw['total'], paths.length, 9007199254740990);
    truncated = _flag(raw['truncated']);
    if (truncated != (total > paths.length)) throw const FormatException();
  }
  late final List<String> paths;
  late final int total;
  late final bool truncated;
}

class AgentExecutionMetadata {
  AgentExecutionMetadata.fromJson(Object? value) {
    final raw = _object(value);
    workspace = workspaceText(raw['workspace']);
    shellWorkspaceAccess = workspaceText(
      raw['shell_workspace_access'],
      max: 16,
    );
    if (!const [
      'read_only',
      'read_write',
      'unavailable',
    ].contains(shellWorkspaceAccess)) {
      throw const FormatException();
    }
    directoryValid = _flag(raw['shell_directory_valid']);
    directory = raw['shell_directory'] == null
        ? null
        : workspaceText(raw['shell_directory']);
    if (directoryValid != (directory != null)) throw const FormatException();
    readonly = ExecutionPathList.fromJson(raw['readonly']);
    protected = ExecutionPathList.fromJson(raw['protected']);
    binaryDetected = _flag(raw['shell_binary_detected']);
    isolationVerified = _flag(raw['isolation_verified']);
    namespace = workspaceText(raw['network_namespace'], max: 32);
    final environment = _object(raw['environment']);
    if (raw['shell_backend'] != 'bubblewrap' ||
        raw['sandbox_optional'] != false ||
        raw['workspace_mutable'] != false ||
        !const ['shared_host', 'isolated'].contains(namespace) ||
        environment['clear_host_environment'] != true ||
        environment['PATH'] != '/usr/bin:/bin' ||
        environment['HOME'] != '/tmp') {
      throw const FormatException();
    }
  }
  late final String workspace, namespace, shellWorkspaceAccess;
  late final String? directory;
  late final bool directoryValid, binaryDetected, isolationVerified;
  late final ExecutionPathList readonly, protected;
}

class AgentRuntimeSnapshot {
  AgentRuntimeSnapshot.fromJson(Object? value) {
    final raw = workspaceObject(value);
    revision = _integer(raw['revision'], 0, 9007199254740990);
    stateToken = workspaceText(raw['state_token'], max: 64);
    if (!RegExp(r'^[a-f0-9]{64}$').hasMatch(stateToken) ||
        raw['change_boundary'] != 'settled_service' ||
        raw['yolo_scope'] != 'service_instance') {
      throw const FormatException();
    }
    instanceId = workspaceText(raw['instance_id'], max: 128);
    writeAuthority = AgentRuntimeAuthority.supported(raw['write_authority']);
    if (writeAuthority != null && writeAuthority!.instanceId != instanceId) {
      throw const FormatException('Execution authority does not match');
    }
    source = workspaceText(raw['source'], max: 32);
    if (!const ['owner_saved', 'host_configuration'].contains(source)) {
      throw const FormatException();
    }
    settings = AgentExecutionSettings.fromJson(raw['settings']);
    saved = AgentExecutionSettings.fromJson(raw['saved_settings']);
    activeMatchesSaved = _flag(raw['active_matches_saved']);
    yolo = _flag(raw['yolo']);
    effectiveWrite = _flag(raw['effective_write']);
    effectiveShell = _flag(raw['effective_shell']);
    busy = _flag(raw['busy']);
    execution = AgentExecutionMetadata.fromJson(raw['execution']);
    if (activeMatchesSaved != settings.matches(saved) ||
        effectiveWrite !=
            (settings.fileWrites != 'deny' &&
                (settings.fileWrites == 'allow' || yolo)) ||
        effectiveShell !=
            (settings.shellCommands != 'deny' &&
                (settings.shellCommands == 'allow' || yolo))) {
      throw const FormatException();
    }
  }
  late final int revision;
  late final String stateToken, instanceId, source;
  late final AgentExecutionSettings settings, saved;
  late final bool activeMatchesSaved,
      yolo,
      effectiveWrite,
      effectiveShell,
      busy;
  late final AgentExecutionMetadata execution;
  late final AgentRuntimeAuthority? writeAuthority;
}
