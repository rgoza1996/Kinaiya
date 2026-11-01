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
    workingDirectory = workspaceText(raw['working_directory'], max: 512);
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
    if (!const ['read_only', 'read_write'].contains(shellWorkspaceAccess)) {
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
}
