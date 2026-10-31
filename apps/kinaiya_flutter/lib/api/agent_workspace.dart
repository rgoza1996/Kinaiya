/// Owner workspace read models. Metadata never supplies execution authority.
library;

import 'models.dart';

Map<String, Object?> workspaceObject(Object? value) {
  if (value is! Map<String, Object?> || value['schema_version'] != '1.0') {
    throw const FormatException('Unsupported Agent workspace response');
  }
  return value;
}

String workspaceText(Object? value, {int max = 4096, bool empty = false}) {
  if (value is! String || (!empty && value.isEmpty) || value.length > max) {
    throw const FormatException('Invalid Agent workspace text');
  }
  return value;
}

class AgentHarness {
  const AgentHarness({
    required this.versionId,
    required this.name,
    required this.kind,
    required this.description,
    required this.active,
    required this.selectable,
    required this.eligibility,
    this.registeredAt,
  });

  factory AgentHarness.fromJson(Object? value) {
    if (value is! Map<String, Object?> ||
        value['active'] is! bool ||
        value['selectable'] is! bool) {
      throw const FormatException('Invalid harness catalog entry');
    }
    final time = value['registered_at'];
    if (time != null && (time is! num || !time.isFinite || time < 0)) {
      throw const FormatException('Invalid harness registration time');
    }
    return AgentHarness(
      versionId: workspaceText(value['version_id'], max: 128),
      name: workspaceText(value['name'], max: 200),
      kind: workspaceText(value['kind'], max: 80),
      description: workspaceText(value['description'], empty: true),
      active: value['active'] as bool,
      selectable: value['selectable'] as bool,
      eligibility: workspaceText(value['eligibility'], max: 80),
      registeredAt: (time as num?)?.toDouble(),
    );
  }

  final String versionId, name, kind, description, eligibility;
  final bool active, selectable;
  final double? registeredAt;
}

class AgentHarnessPage {
  AgentHarnessPage.fromJson(Object? value) {
    final raw = workspaceObject(value);
    final rows = raw['items'];
    if (raw['configured'] is! bool || rows is! List || rows.length > 50) {
      throw const FormatException('Invalid harness catalog page');
    }
    configured = raw['configured'] as bool;
    items = List.unmodifiable(rows.map(AgentHarness.fromJson));
    if (items.map((x) => x.versionId).toSet().length != items.length) {
      throw const FormatException('Repeated harness identity');
    }
    nextCursor = raw['next_cursor'] == null
        ? null
        : workspaceText(raw['next_cursor'], max: 128);
    if (nextCursor != null &&
        (items.isEmpty || nextCursor != items.last.versionId)) {
      throw const FormatException('Invalid harness catalog cursor');
    }
  }
  late final bool configured;
  late final List<AgentHarness> items;
  late final String? nextCursor;
}

SessionInfo workspaceSession(Object? value) {
  final raw = workspaceObject(value);
  final epoch = raw['epoch'];
  if (raw['channel'] != 'agent' ||
      epoch is! int ||
      epoch < 1 ||
      epoch > 9007199254740991) {
    throw const FormatException('Invalid Agent session scope');
  }
  workspaceText(raw['session_id'], max: 128);
  workspaceText(raw['thread_id'], max: 128);
  workspaceText(raw['name'], max: 200, empty: true);
  if (raw['harness_version'] != null) {
    workspaceText(raw['harness_version'], max: 128);
  }
  return SessionInfo.fromJson(raw);
}

class AgentSessionPage {
  AgentSessionPage.fromJson(Object? value) {
    final raw = workspaceObject(value);
    final rows = raw['items'];
    if (rows is! List || rows.length > 20) {
      throw const FormatException('Invalid Agent session page');
    }
    items = List.unmodifiable(rows.map(workspaceSession));
    if (items.map((x) => x.sessionId).toSet().length != items.length) {
      throw const FormatException('Repeated Agent session identity');
    }
    nextCursor = raw['next_cursor'] == null
        ? null
        : workspaceText(raw['next_cursor'], max: 128);
    if (nextCursor != null &&
        (!RegExp(r'^[1-9][0-9]*$').hasMatch(nextCursor!) || items.isEmpty)) {
      throw const FormatException('Invalid Agent session cursor');
    }
  }
  late final List<SessionInfo> items;
  late final String? nextCursor;
}
