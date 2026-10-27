/// Dart bindings for the checked-in JSON schemas in
/// packages/contracts/schemas/*.json. Field mapping is snake_case-exact and
/// every wire model carries a schema_version == '1.0' check.
library;

const String schemaVersion = '1.0';

void checkSchemaVersion(Map<String, Object?> json, String model) {
  final version = (json['schema_version'] as String?) ?? '1.0';
  if (version != schemaVersion) {
    throw FormatException('Unsupported $model schema_version: $version');
  }
}

Map<String, Object?> _map(Object? value) {
  if (value is Map<String, Object?>) return value;
  if (value is Map) return value.cast<String, Object?>();
  throw FormatException('Expected a JSON object, got $value');
}

List<Map<String, Object?>> _mapList(Object? value) {
  if (value == null) return const [];
  if (value is List) return value.map(_map).toList();
  throw FormatException('Expected a JSON list, got $value');
}

/// Shared journal session (Session.json).
class SessionInfo {
  SessionInfo({
    required this.sessionId,
    required this.threadId,
    required this.channel,
    required this.epoch,
    this.name = '',
  });

  factory SessionInfo.fromJson(Map<String, Object?> json) {
    checkSchemaVersion(json, 'Session');
    final channel = json['channel'] as String? ?? 'converse';
    if (channel != 'converse' && channel != 'agent') {
      throw FormatException('Unknown session channel: $channel');
    }
    return SessionInfo(
      sessionId: json['session_id'] as String? ?? '',
      threadId: json['thread_id'] as String? ?? '',
      channel: channel,
      epoch: (json['epoch'] as num?)?.toInt() ?? 1,
      name: json['name'] as String? ?? '',
    );
  }

  final String sessionId;
  final String threadId;
  final String channel;
  final int epoch;
  final String name;

  Map<String, Object?> toCreateJson() => <String, Object?>{
    'schema_version': schemaVersion,
    'channel': channel,
    'name': name,
  };
}

/// Journal entry (JournalEntry.json). Payload stays untyped by contract.
class JournalEntry {
  JournalEntry({
    required this.eventId,
    required this.sequence,
    required this.sessionId,
    required this.threadId,
    required this.epoch,
    required this.kind,
    required this.payload,
    required this.origin,
    this.causes = const [],
    this.taskRefs = const [],
  });

  factory JournalEntry.fromJson(Map<String, Object?> json) {
    checkSchemaVersion(json, 'JournalEntry');
    return JournalEntry(
      eventId: json['event_id'] as String? ?? '',
      sequence: (json['sequence'] as num?)?.toInt() ?? 0,
      sessionId: json['session_id'] as String? ?? '',
      threadId: json['thread_id'] as String? ?? '',
      epoch: (json['epoch'] as num?)?.toInt() ?? 1,
      kind: json['kind'] as String? ?? '',
      payload: _map(json['payload']),
      origin: json['origin'] as String? ?? '',
      causes: ((json['causes'] as List?) ?? []).map((e) => '$e').toList(),
      taskRefs: ((json['task_refs'] as List?) ?? []).map((e) => '$e').toList(),
    );
  }

  final String eventId;
  final int sequence;
  final String sessionId;
  final String threadId;
  final int epoch;
  final String kind;
  final Map<String, Object?> payload;
  final String origin;
  final List<String> causes;
  final List<String> taskRefs;

  String get text => payload['text'] as String? ?? '';
}

/// Agent task (Task.json). State is authoritative; success requires
/// state == succeeded plus supporting evidence.
class AgentTask {
  AgentTask({
    required this.taskId,
    required this.sessionId,
    required this.threadId,
    required this.epoch,
    required this.objective,
    required this.objectiveRevision,
    required this.state,
    required this.idempotencyKey,
    this.summary = '',
    this.evidence = const [],
    this.remainingRequiredWork = const [],
    this.error,
  });

  factory AgentTask.fromJson(Map<String, Object?> json) {
    checkSchemaVersion(json, 'Task');
    return AgentTask(
      taskId: json['task_id'] as String? ?? '',
      sessionId: json['session_id'] as String? ?? '',
      threadId: json['thread_id'] as String? ?? '',
      epoch: (json['epoch'] as num?)?.toInt() ?? 1,
      objective: json['objective'] as String? ?? '',
      objectiveRevision: (json['objective_revision'] as num?)?.toInt() ?? 1,
      state: json['state'] as String? ?? 'accepted',
      idempotencyKey: json['idempotency_key'] as String? ?? '',
      summary: json['summary'] as String? ?? '',
      evidence: _mapList(json['evidence']).map(Evidence.fromJson).toList(),
      remainingRequiredWork: ((json['remaining_required_work'] as List?) ?? [])
          .map((e) => '$e')
          .toList(),
      error: json['error'] as String?,
    );
  }

  final String taskId;
  final String sessionId;
  final String threadId;
  final int epoch;
  final String objective;
  final int objectiveRevision;
  final String state;
  final String idempotencyKey;
  final String summary;
  final List<Evidence> evidence;
  final List<String> remainingRequiredWork;
  final String? error;

  static const Set<String> terminalStates = {
    'succeeded',
    'failed',
    'cancelled',
  };

  bool get isTerminal => terminalStates.contains(state);

  /// Grounded success: authoritative succeeded state with independent
  /// native evidence at the current objective revision. MCP and compute
  /// receipts alone never prove a task effect.
  bool get isGroundedSuccess {
    final current = evidence.where(
      (e) => e.objectiveRevision == objectiveRevision,
    );
    final native = current.where(
      (e) => !{'external', 'extension'}.contains(e.kind),
    );
    final independent = native.any(
      (e) =>
          {'file', 'command', 'test'}.contains(e.kind) ||
          (e.kind == 'read' && e.path != null && e.sha256 != null),
    );
    return state == 'succeeded' &&
        remainingRequiredWork.isEmpty &&
        native.isNotEmpty &&
        (!current.any((e) => {'external', 'extension'}.contains(e.kind)) ||
            independent);
  }

  bool get hasUnverifiedExternalOnly =>
      state == 'succeeded' &&
      evidence.any(
        (e) =>
            {'external', 'extension'}.contains(e.kind) &&
            e.objectiveRevision == objectiveRevision,
      ) &&
      !isGroundedSuccess;
}

class Evidence {
  Evidence({
    required this.kind,
    required this.summary,
    this.path,
    this.sha256,
    this.exitCode,
    this.output = '',
    this.objectiveRevision = 1,
  });

  factory Evidence.fromJson(Map<String, Object?> json) {
    checkSchemaVersion(json, 'Evidence');
    const kinds = {
      'file',
      'command',
      'read',
      'search',
      'test',
      'extension',
      'external',
    };
    final kind = json['kind'] as String? ?? '';
    if (!kinds.contains(kind)) {
      throw FormatException('Unknown evidence kind: $kind');
    }
    return Evidence(
      kind: kind,
      summary: json['summary'] as String? ?? '',
      path: json['path'] as String?,
      sha256: json['sha256'] as String?,
      exitCode: (json['exit_code'] as num?)?.toInt(),
      output: json['output'] as String? ?? '',
      objectiveRevision: (json['objective_revision'] as num?)?.toInt() ?? 1,
    );
  }

  final String kind;
  final String summary;
  final String? path;
  final String? sha256;
  final int? exitCode;
  final String output;
  final int objectiveRevision;
}

/// Approval request surfaced over the journal (approval.requested payload).
class ApprovalRequest {
  ApprovalRequest({
    required this.approvalId,
    required this.taskId,
    required this.tool,
    required this.actionDigest,
    required this.objectiveRevision,
    this.arguments = const {},
    this.expiresAt,
    this.reason = '',
    this.sessionId = '',
    this.threadId = '',
    this.epoch = 0,
  });

  factory ApprovalRequest.fromEntry(JournalEntry entry) {
    return ApprovalRequest.fromJson({
      ...entry.payload,
      'approval_id': entry.payload['approval_id'] ?? entry.eventId,
      'session_id': entry.sessionId,
      'thread_id': entry.threadId,
      'epoch': entry.epoch,
    });
  }

  factory ApprovalRequest.fromJson(Map<String, Object?> p) {
    checkSchemaVersion(p, 'PendingApproval');
    return ApprovalRequest(
      approvalId: p['approval_id'] as String? ?? '',
      taskId: p['task_id'] as String? ?? '',
      tool: p['tool'] as String? ?? '',
      actionDigest: p['action_digest'] as String? ?? '',
      objectiveRevision: (p['objective_revision'] as num?)?.toInt() ?? 1,
      arguments: _map(p['arguments']),
      reason: p['reason'] as String? ?? '',
      sessionId: p['session_id'] as String? ?? '',
      threadId: p['thread_id'] as String? ?? '',
      epoch: (p['epoch'] as num?)?.toInt() ?? 0,
      expiresAt: p['expires_at'] is num
          ? DateTime.fromMillisecondsSinceEpoch(
              ((p['expires_at'] as num) * 1000).round(),
              isUtc: true,
            ).toIso8601String()
          : p['expires_at'] as String?,
    );
  }

  final String approvalId;
  final String taskId;
  final String tool;
  final String actionDigest;
  final int objectiveRevision;
  final Map<String, Object?> arguments;
  final String? expiresAt;
  final String reason;
  final String sessionId;
  final String threadId;
  final int epoch;

  bool get isExpired {
    if (expiresAt == null || expiresAt!.isEmpty) return false;
    final t = DateTime.tryParse(expiresAt!);
    return t != null && DateTime.now().toUtc().isAfter(t);
  }
}

class PendingApprovalPage {
  PendingApprovalPage(this.approvals, this.nextAfter);
  factory PendingApprovalPage.fromJson(Map<String, Object?> json) {
    checkSchemaVersion(json, 'PendingApprovalPage');
    final entries = _mapList(json['approvals']);
    if (entries.any((e) => e['origin'] != 'agent-supervisor')) {
      throw const FormatException('Unverified approval origin');
    }
    return PendingApprovalPage(
      entries.map(ApprovalRequest.fromJson).toList(),
      json['next_after'] as String?,
    );
  }
  final List<ApprovalRequest> approvals;
  final String? nextAfter;
}

/// Agent-only command registry entry (GET /v1/commands).
class CommandDef {
  CommandDef({
    required this.name,
    required this.description,
    this.available = true,
    this.origin = '',
    this.aliases = const [],
    this.syntax = '',
    this.executionType = '',
  });

  factory CommandDef.fromJson(Map<String, Object?> json) {
    return CommandDef(
      name: (json['name'] as String? ?? '').replaceFirst(RegExp(r'^/'), ''),
      description: json['description'] as String? ?? '',
      available: json['available'] as bool? ?? true,
      origin: json['origin'] as String? ?? '',
      aliases: (json['aliases'] as List? ?? []).cast<String>(),
      syntax: json['syntax'] as String? ?? '',
      executionType: json['execution_type'] as String? ?? '',
    );
  }

  final String name;
  final String description;
  final bool available;
  final String origin;
  final List<String> aliases;
  final String syntax;

  /// Backend-declared execution class. `runtime_control` runs a
  /// deterministic owner-authenticated handler without model inference;
  /// `skill_invocation` starts normal Agent work. Unknown or missing
  /// classes fail closed to inference so they cannot bypass consent.
  /// The `skill` entry is always inference even when a stale backend still
  /// labels it `runtime_control` (Lead corrects it to `skill_invocation`).
  final String executionType;

  bool get isDeterministic =>
      executionType == 'runtime_control' && name != 'skill';

  bool get requiresInference => !isDeterministic;

  bool matches(String input) => name == input || aliases.contains(input);
}
