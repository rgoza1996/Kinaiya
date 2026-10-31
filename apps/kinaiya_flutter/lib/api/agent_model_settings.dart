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
  }
  late final int revision;
  late final AgentModelBinding defaultBinding;
  late final List<AgentModelProfile> profiles;
  late final Map<String, AgentModelBinding> harnesses;
  AgentModelBinding effective(String? harness) =>
      harnesses[harness] ?? defaultBinding;
  AgentModelProfile? profile(String id) {
    for (final row in profiles) {
      if (row.id == id) return row;
    }
    return null;
  }
}
