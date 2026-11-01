/// Dart bindings for the checked-in JSON schemas in
/// packages/contracts/schemas/*.json. Field mapping is snake_case-exact and
/// every wire model carries a schema_version == '1.0' check.
library;

import 'dart:convert';

const String schemaVersion = '1.0';

/// Owner-declared disclosure for this input, independent of provider choice.
enum ContentScope {
  localOnly('local_only', 2),
  publicSynthetic('public_synthetic', 0),
  privateCloudAuthorized('private_cloud_authorized', 1);

  const ContentScope(this.wireValue, this._restriction);
  final String wireValue;
  final int _restriction;

  ContentScope combine(ContentScope other) =>
      _restriction >= other._restriction ? this : other;

  static ContentScope fromWire(Object? value) => values.firstWhere(
    (scope) => scope.wireValue == value,
    orElse: () => localOnly,
  );
}

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
    this.harnessVersion,
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
      harnessVersion: json['harness_version'] as String?,
    );
  }

  final String sessionId;
  final String threadId;
  final String channel;
  final int epoch;
  final String name;
  final String? harnessVersion;

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
    this.contentScope = ContentScope.localOnly,
    this.error,
    this.harnessVersion,
    this.modelPin,
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
      contentScope: ContentScope.fromWire(json['content_scope']),
      idempotencyKey: json['idempotency_key'] as String? ?? '',
      summary: json['summary'] as String? ?? '',
      evidence: _mapList(json['evidence']).map(Evidence.fromJson).toList(),
      remainingRequiredWork: ((json['remaining_required_work'] as List?) ?? [])
          .map((e) => '$e')
          .toList(),
      error: json['error'] as String?,
      harnessVersion: json['harness_version'] as String?,
      modelPin: json['model_pin'] is Map
          ? (json['model_pin'] as Map).cast<String, Object?>()
          : null,
    );
  }

  final String taskId;
  final String sessionId;
  final String threadId;
  final int epoch;
  final String objective;
  final int objectiveRevision;
  final String state;
  final ContentScope contentScope;
  final String idempotencyKey;
  final String summary;
  final List<Evidence> evidence;
  final List<String> remainingRequiredWork;
  final String? error;
  final String? harnessVersion;
  final Map<String, Object?>? modelPin;

  static const Set<String> terminalStates = {
    'succeeded',
    'failed',
    'cancelled',
  };

  bool get isTerminal => terminalStates.contains(state);

  /// Grounded success needs native work or a native publication receipt at
  /// the current revision. Publication proves UI publication only; inspection,
  /// MCP and compute receipts alone never prove work or external effects.
  bool get isGroundedSuccess {
    final current = evidence.where(
      (e) => e.objectiveRevision == objectiveRevision,
    );
    final native = current.where(
      (e) => {'file', 'command', 'read', 'search', 'test'}.contains(e.kind),
    );
    final published = current.any((e) => e._hasArtifactReceiptShape);
    final independent = native.any(
      (e) =>
          {'file', 'command', 'test'}.contains(e.kind) ||
          (e.kind == 'read' && e.path != null && e.sha256 != null),
    );
    return state == 'succeeded' &&
        remainingRequiredWork.isEmpty &&
        (native.isNotEmpty || published) &&
        (!current.any((e) => {'external', 'extension'}.contains(e.kind)) ||
            independent ||
            published);
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

/// Frozen owner input. Retrying uses this body, never the current draft/model.
class AgentSubmission {
  AgentSubmission({
    required this.session,
    required this.text,
    required this.scope,
    required this.key,
    this.command,
  }) {
    if (session.channel != 'agent' ||
        session.sessionId.isEmpty ||
        session.threadId.isEmpty ||
        session.epoch < 1 ||
        text.isEmpty ||
        text.length > 16000 ||
        key.isEmpty ||
        key.length > 256) {
      throw const FormatException('Invalid Agent submission');
    }
    body = Map.unmodifiable({
      'schema_version': schemaVersion,
      'session_id': session.sessionId,
      'thread_id': session.threadId,
      'epoch': session.epoch,
      if (command == null) 'objective': text else 'input': text,
      'content_scope': scope.wireValue,
      'idempotency_key': key,
      if (command == null) ...{
        'source_event_ids': const <String>[],
        'source_revision': 1,
      },
    });
    if (utf8.encode(jsonEncode(body)).length > 65536) {
      throw const FormatException('Agent submission exceeds its request bound');
    }
  }

  final SessionInfo session;
  final String text, key;
  final ContentScope scope;
  final String? command;
  late final Map<String, Object?> body;

  bool sameScope(SessionInfo? other) =>
      other != null &&
      other.sessionId == session.sessionId &&
      other.threadId == session.threadId &&
      other.epoch == session.epoch;

  static const taskStates = {
    'accepted',
    'queued',
    'running',
    'verifying',
    'awaiting_approval',
    'blocked',
    'succeeded',
    'failed',
    'cancel_requested',
    'cancelled',
    'paused',
  };

  static bool validCommandTaskResult(Object? value) {
    if (value is! Map<String, Object?>) return false;
    final id = value['accepted_task_id'];
    return id is String &&
        id.isNotEmpty &&
        id.length <= 128 &&
        !id.contains('\u0000') &&
        taskStates.contains(value['state']);
  }

  AgentTask taskReceipt(Map<String, Object?> raw) {
    // Mutable objective, state and model metadata may legitimately have moved
    // after steering. The native exact POST validates original logical input;
    // the returned Task must retain its original immutable submission identity.
    final revision = raw['objective_revision'], state = raw['state'];
    final taskId = raw['task_id'];
    if (raw['schema_version'] != schemaVersion ||
        raw['session_id'] != session.sessionId ||
        raw['thread_id'] != session.threadId ||
        raw['epoch'] is! int ||
        raw['epoch'] != session.epoch ||
        raw['idempotency_key'] != key ||
        taskId is! String ||
        taskId.isEmpty ||
        taskId.length > 128 ||
        revision is! int ||
        revision < 1 ||
        revision > 9007199254740991 ||
        !taskStates.contains(state) ||
        raw['objective'] is! String ||
        !ContentScope.values.any(
          (value) => value.wireValue == raw['content_scope'],
        ) ||
        scope.combine(ContentScope.fromWire(raw['content_scope'])) !=
            ContentScope.fromWire(raw['content_scope'])) {
      throw const FormatException('Agent submission receipt does not match');
    }
    final pin = raw['model_pin'];
    if (pin != null) _historicalPin(pin);
    final auxiliary = raw['auxiliary_models'];
    if (auxiliary != null) {
      if (auxiliary is! Map<String, Object?> ||
          auxiliary.length > 5 ||
          !const {
            'analysis',
            'compression',
            'title_generation',
            'delegation',
            'harness_design',
          }.containsAll(auxiliary.keys)) {
        throw const FormatException('Invalid recorded auxiliary model pins');
      }
      for (final value in auxiliary.values) {
        _historicalPin(value);
      }
    }
    return AgentTask.fromJson(raw);
  }

  static void _historicalPin(Object? value) {
    if (value is! Map<String, Object?> ||
        value['local'] is! bool ||
        value['settings_revision'] is! int ||
        (value['settings_revision'] as int) < 0 ||
        (value['schema_version'] != null &&
            value['schema_version'] != schemaVersion) ||
        (value['reasoning'] != null &&
            (value['reasoning'] is! String ||
                (value['reasoning'] as String).length > 16))) {
      throw const FormatException('Invalid recorded model pin');
    }
    for (final field in {
      'profile_id': 80,
      'provider': 128,
      'model': 256,
    }.entries) {
      final label = value[field.key];
      if (label is! String || label.isEmpty || label.length > field.value) {
        throw const FormatException('Invalid recorded model pin');
      }
    }
    final binding = value['catalog_binding'],
        revision = value['catalog_revision'];
    if (binding == null && revision == null) return;
    if (binding is! Map<String, Object?> ||
        binding.length != 3 ||
        binding['id'] != value['profile_id'] ||
        binding['id'] is! String ||
        !RegExp(
          r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$',
        ).hasMatch(binding['id'] as String) ||
        binding['generation'] is! int ||
        (binding['generation'] as int) < 1 ||
        (binding['generation'] as int) > 64 ||
        binding['configuration_sha256'] is! String ||
        !RegExp(
          r'^[a-f0-9]{64}$',
        ).hasMatch(binding['configuration_sha256'] as String) ||
        revision is! int ||
        revision < 0 ||
        revision > 9007199254740990) {
      throw const FormatException('Invalid recorded catalog model pin');
    }
  }
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
      'artifact',
      'inspection',
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

  // The authoritative succeeded state comes from Agent. This shape check
  // prevents a malformed artifact receipt from becoming supporting evidence;
  // the client cannot independently verify the stored publication's bytes.
  bool get _hasArtifactReceiptShape {
    if (kind != 'artifact' || path == null || sha256 == null) return false;
    final id = RegExp(r'artifact:[A-Za-z0-9_-]{1,128}').matchAsPrefix(path!);
    final hash = RegExp(r'[a-f0-9]{64}').matchAsPrefix(sha256!);
    return id?.end == path!.length && hash?.end == sha256!.length;
  }
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
  /// `create-harness` always starts ordinary Agent work, even if stale metadata
  /// calls it a deterministic control.
  /// The `skill` entry is always inference even when a stale backend still
  /// labels it `runtime_control` (Lead corrects it to `skill_invocation`).
  final String executionType;

  bool get isDeterministic =>
      executionType == 'runtime_control' &&
      !const {'skill', 'create-harness'}.contains(name);

  bool get requiresInference => !isDeterministic;

  bool matches(String input) => name == input || aliases.contains(input);
}
