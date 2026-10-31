import 'agent_workspace.dart';

DateTime _created(Object? value) {
  final time = DateTime.tryParse(workspaceText(value, max: 80));
  if (time == null || !time.isUtc) throw const FormatException();
  return time;
}

int _integer(Object? value, {int minimum = 0}) {
  if (value is! int || value < minimum || value > 9007199254740990) {
    throw const FormatException();
  }
  return value;
}

class GeneratedOutput {
  GeneratedOutput.fromJson(Object? value) {
    if (value is! Map<String, Object?>) throw const FormatException();
    eventId = workspaceText(value['event_id'], max: 128);
    sequence = _integer(value['sequence'], minimum: 1);
    name = workspaceText(value['name'], max: 1024);
    path = workspaceText(value['relative_path'], max: 1024);
    type = workspaceText(value['file_type'], max: 20);
    taskId = workspaceText(value['task_id'], max: 128);
    sessionId = workspaceText(value['session_id'], max: 128);
    createdAt = _created(value['created_at']);
    if (!RegExp(
      r'^[a-f0-9]{64}$',
    ).hasMatch(workspaceText(value['sha256'], max: 64))) {
      throw const FormatException();
    }
  }
  late final String eventId, name, path, type, taskId, sessionId;
  late final int sequence;
  late final DateTime createdAt;
}

class GeneratedOutputPage {
  GeneratedOutputPage.fromJson(
    Object? value, {
    required String kind,
    required int after,
  }) {
    final raw = workspaceObject(value);
    final list = raw['items'];
    if (raw['kind'] != kind || list is! List || list.length > 100) {
      throw const FormatException();
    }
    items = List.unmodifiable(list.map(GeneratedOutput.fromJson));
    var previous = after;
    for (final item in items) {
      if (item.sequence <= previous) throw const FormatException();
      previous = item.sequence;
    }
    nextCursor = raw['next_cursor'] == null
        ? null
        : _integer(raw['next_cursor'], minimum: after + 1);
    if (nextCursor != null && nextCursor! < previous) {
      throw const FormatException();
    }
    if (nextCursor == null && items.isNotEmpty) throw const FormatException();
  }
  late final List<GeneratedOutput> items;
  late final int? nextCursor;
}

class AgentProject {
  AgentProject.fromJson(Object? value) {
    final raw = workspaceObject(value);
    id = workspaceText(raw['project_id'], max: 36);
    if (!RegExp(r'^[a-f0-9-]{36}$').hasMatch(id)) throw const FormatException();
    name = workspaceText(raw['name'], max: 120);
    createdAt = _created(raw['created_at']);
    revision = _integer(raw['revision'], minimum: 1);
    final sessions = raw['session_ids'];
    if (sessions is! List || sessions.length > 200) {
      throw const FormatException();
    }
    sessionIds = List.unmodifiable(
      sessions.map((id) => workspaceText(id, max: 128)),
    );
    if (sessionIds.toSet().length != sessionIds.length) {
      throw const FormatException();
    }
  }
  late final String id, name;
  late final int revision;
  late final DateTime createdAt;
  late final List<String> sessionIds;
}

class AgentProjectPage {
  AgentProjectPage.fromJson(Object? value) {
    final raw = workspaceObject(value), rows = raw['items'];
    if (rows is! List || rows.length > 20) throw const FormatException();
    items = List.unmodifiable(rows.map(AgentProject.fromJson));
    if (items.map((row) => row.id).toSet().length != items.length) {
      throw const FormatException();
    }
    nextCursor = raw['next_cursor'] == null
        ? null
        : workspaceText(raw['next_cursor'], max: 36);
    if (nextCursor != null && (items.isEmpty || items.last.id != nextCursor)) {
      throw const FormatException();
    }
  }
  late final List<AgentProject> items;
  late final String? nextCursor;
}
