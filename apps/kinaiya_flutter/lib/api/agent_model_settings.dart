/// Validated non-secret owner settings. Profiles describe configuration only.
library;

import 'agent_workspace.dart';

class AgentModelBinding {
  const AgentModelBinding(this.profileId, this.reasoning);
  factory AgentModelBinding.fromJson(Object? value) {
    if (value is! Map<String, Object?>) throw const FormatException();
    final effort = value['reasoning'];
    if (effort != null && (effort is! String || effort.length > 16)) {
      throw const FormatException();
    }
    return AgentModelBinding(
      workspaceText(value['profile_id'], max: 80),
      effort as String?,
    );
  }
  final String profileId;
  final String? reasoning;
}

class AgentModelProfile {
  AgentModelProfile.fromJson(Object? value) {
    if (value is! Map<String, Object?> || value['selectable'] is! bool) {
      throw const FormatException();
    }
    id = workspaceText(value['id'], max: 80);
    selectable = value['selectable'] as bool;
    final auxiliary = value['auxiliary_selectable'];
    if (auxiliary != null && auxiliary is! bool) throw const FormatException();
    auxiliarySelectable = auxiliary == true;
    final info = value['provider'];
    if (selectable) {
      if (info is! Map<String, Object?> || info['local'] is! bool) {
        throw const FormatException();
      }
      provider = workspaceText(info['provider'], max: 128);
      model = workspaceText(info['model'], max: 256);
      local = info['local'] as bool;
      providerInfo = Map.unmodifiable(info);
    } else if (info != null) {
      throw const FormatException();
    }
    final levels = value['reasoning_options'];
    if (levels is! List ||
        levels.length > 6 ||
        levels.any(
          (x) =>
              x is! String ||
              !const [
                'none',
                'minimal',
                'low',
                'medium',
                'high',
                'xhigh',
              ].contains(x),
        )) {
      throw const FormatException();
    }
    reasoningOptions = List.unmodifiable(levels.cast<String>());
    final selected = value['reasoning'];
    if (selected != null && (selected is! String || selected.length > 16)) {
      throw const FormatException();
    }
    reasoning = selected as String?;
  }
  late final String id;
  late final bool selectable;
  late final bool auxiliarySelectable;
  String? provider, model, reasoning;
  bool? local;
  Map<String, Object?>? providerInfo;
  late final List<String> reasoningOptions;
  String get label => id == 'host-default' ? 'Host default' : id;
}

class AgentModelsSnapshot {
  AgentModelsSnapshot.fromJson(Object? value) {
    final raw = workspaceObject(value);
    final rev = raw['revision'];
    final list = raw['profiles'];
    final overrides = raw['harnesses'];
    if (rev is! int ||
        rev < 0 ||
        rev > 9007199254740990 ||
        raw['change_boundary'] != 'new_tasks' ||
        list is! List ||
        list.isEmpty ||
        list.length > 32 ||
        overrides is! Map<String, Object?> ||
        overrides.length > 1000) {
      throw const FormatException();
    }
    revision = rev;
    defaultBinding = AgentModelBinding.fromJson(raw['default']);
    profiles = List.unmodifiable(list.map(AgentModelProfile.fromJson));
    if (profiles.map((x) => x.id).toSet().length != profiles.length) {
      throw const FormatException();
    }
    harnesses = Map.unmodifiable(
      overrides.map(
        (key, value) => MapEntry(
          workspaceText(key, max: 128),
          AgentModelBinding.fromJson(value),
        ),
      ),
    );
    final tasks = raw['auxiliary_tasks'] ?? const [];
    if (tasks is! List || tasks.length > 5) throw const FormatException();
    final labels = <String, String>{};
    for (final task in tasks) {
      if (task is! Map<String, Object?>) throw const FormatException();
      final id = workspaceText(task['id'], max: 40);
      if (!const {
            'analysis',
            'compression',
            'title_generation',
            'delegation',
            'harness_design',
          }.contains(id) ||
          labels.containsKey(id)) {
        throw const FormatException();
      }
      labels[id] = workspaceText(task['label'], max: 100);
    }
    auxiliaryTasks = Map.unmodifiable(labels);
    Map<String, AgentModelBinding> routes(Object? value) {
      if (value is! Map<String, Object?> ||
          value.keys.any((key) => !labels.containsKey(key))) {
        throw const FormatException();
      }
      return Map.unmodifiable(
        value.map(
          (key, binding) => MapEntry(key, AgentModelBinding.fromJson(binding)),
        ),
      );
    }

    auxiliary = routes(raw['auxiliary'] ?? const <String, Object?>{});
    final custom = raw['harness_auxiliary'] ?? const <String, Object?>{};
    if (custom is! Map<String, Object?> || custom.length > 1000) {
      throw const FormatException();
    }
    harnessAuxiliary = Map.unmodifiable(
      custom.map(
        (key, value) => MapEntry(workspaceText(key, max: 128), routes(value)),
      ),
    );
  }
  late final int revision;
  late final AgentModelBinding defaultBinding;
  late final List<AgentModelProfile> profiles;
  late final Map<String, AgentModelBinding> harnesses;
  late final Map<String, String> auxiliaryTasks;
  late final Map<String, AgentModelBinding> auxiliary;
  late final Map<String, Map<String, AgentModelBinding>> harnessAuxiliary;
  AgentModelBinding inherited(String? harness, [String? purpose]) =>
      purpose == null
      ? defaultBinding
      : (harness == null ? null : auxiliary[purpose]) ??
            harnesses[harness] ??
            defaultBinding;
  AgentModelBinding effective(String? harness, [String? purpose]) =>
      purpose == null
      ? harnesses[harness] ?? defaultBinding
      : harnessAuxiliary[harness]?[purpose] ??
            auxiliary[purpose] ??
            harnesses[harness] ??
            defaultBinding;
  bool overrides(String? harness, String? purpose) => purpose == null
      ? harnesses.containsKey(harness)
      : harness == null
      ? auxiliary.containsKey(purpose)
      : harnessAuxiliary[harness]?.containsKey(purpose) == true;
  AgentModelProfile? profile(String id) {
    for (final row in profiles) {
      if (row.id == id) return row;
    }
    return null;
  }
}
