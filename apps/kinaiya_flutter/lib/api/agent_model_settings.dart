/// Validated non-secret owner settings. Profiles describe configuration only.
library;

import 'agent_workspace.dart';
import 'provider_settings.dart' show providerIdentifier;

const _efforts = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'};

String? _effort(Object? value) {
  if (value != null && (value is! String || !_efforts.contains(value))) {
    throw const FormatException('Invalid model reasoning');
  }
  return value as String?;
}

/// An explicit immutable provider generation, never inferred from a label or
/// resolved by an ID alone. The digest is native catalog configuration identity.
class AgentCatalogBinding {
  const AgentCatalogBinding(this.id, this.generation, this.configurationSha256);
  factory AgentCatalogBinding.fromJson(Object? value) {
    if (value is! Map<String, Object?> ||
        value.length != 3 ||
        !value.keys.toSet().containsAll({
          'id',
          'generation',
          'configuration_sha256',
        })) {
      throw const FormatException('Invalid catalog model binding');
    }
    final generation = value['generation'],
        digest = value['configuration_sha256'];
    if (generation is! int ||
        generation < 1 ||
        generation > 64 ||
        digest is! String ||
        digest.length != 64 ||
        !RegExp(r'^[0-9a-f]{64}$').hasMatch(digest)) {
      throw const FormatException('Invalid catalog model binding');
    }
    return AgentCatalogBinding(
      providerIdentifier(value['id']),
      generation,
      digest,
    );
  }
  final String id, configurationSha256;
  final int generation;
  Map<String, Object?> toJson() => {
    'id': id,
    'generation': generation,
    'configuration_sha256': configurationSha256,
  };
  @override
  bool operator ==(Object other) =>
      other is AgentCatalogBinding &&
      id == other.id &&
      generation == other.generation &&
      configurationSha256 == other.configurationSha256;
  @override
  int get hashCode => Object.hash(id, generation, configurationSha256);
}

/// Picker identity distinguishes the host namespace from the provider catalog.
/// Requests always carry explicit typed fields rather than an encoded alias.
class AgentModelReference {
  const AgentModelReference.host(String this.profileId) : catalogBinding = null;
  const AgentModelReference.catalog(AgentCatalogBinding this.catalogBinding)
    : profileId = null;
  final String? profileId;
  final AgentCatalogBinding? catalogBinding;
  @override
  bool operator ==(Object other) =>
      other is AgentModelReference &&
      profileId == other.profileId &&
      catalogBinding == other.catalogBinding;
  @override
  int get hashCode => Object.hash(profileId, catalogBinding);
}

const agentModelMaxRevision = 9007199254740990;
const agentModelPurposes = {
  'analysis',
  'compression',
  'title_generation',
  'delegation',
  'harness_design',
};

bool _scalarText(String value) {
  for (var i = 0; i < value.length; i++) {
    final c = value.codeUnitAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      if (++i >= value.length ||
          value.codeUnitAt(i) < 0xdc00 ||
          value.codeUnitAt(i) > 0xdfff) {
        return false;
      }
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      return false;
    }
  }
  return true;
}

String _authorityId(Object? value) {
  if (value is! String || !RegExp(r'^[a-f0-9]{32}$').hasMatch(value)) {
    throw const FormatException('Invalid model authority');
  }
  return value;
}

Map<String, Object?> _closedObject(Object? value, Set<String> keys) {
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !value.keys.every(keys.contains)) {
    throw const FormatException('Invalid model settings proof');
  }
  return value;
}

/// Native authority is separate from endpoint and private authentication scope.
class AgentModelAuthority {
  AgentModelAuthority.fromJson(Object? value) {
    final raw = _closedObject(value, {
      'mutation_version',
      'backend_id',
      'instance_id',
      'writable',
    });
    if (raw['mutation_version'] != '1.1' || raw['writable'] is! bool) {
      throw const FormatException('Unsupported model write authority');
    }
    backendId = _authorityId(raw['backend_id']);
    instanceId = _authorityId(raw['instance_id']);
    writable = raw['writable'] as bool;
  }
  late final String backendId, instanceId;
  late final bool writable;

  static AgentModelAuthority? supported(Object? value) {
    if (value is! Map || value['mutation_version'] != '1.1') return null;
    return AgentModelAuthority.fromJson(value);
  }
}

/// Complete canonical request, detached once and immutable through recovery.
class AgentModelChange {
  AgentModelChange.fromJson(Object? value) {
    const required = {
      'schema_version',
      'expected_backend',
      'expected_instance',
      'expected_revision',
      'idempotency_key',
      'harness_version',
      'profile_id',
      'reasoning',
    };
    if (value is! Map<String, Object?>) throw const FormatException();
    final raw = _closedObject(value, {
      ...required,
      if (value.containsKey('auxiliary_task')) 'auxiliary_task',
      if (value.containsKey('catalog_binding')) 'catalog_binding',
    });
    if (raw['schema_version'] != '1.1' ||
        raw['expected_revision'] is! int ||
        (raw['expected_revision'] as int) < 0 ||
        (raw['expected_revision'] as int) > agentModelMaxRevision ||
        raw['idempotency_key'] is! String ||
        !RegExp(
          r'^[A-Za-z0-9:._-]{1,128}$',
        ).hasMatch(raw['idempotency_key'] as String)) {
      throw const FormatException('Invalid model change');
    }
    _authorityId(raw['expected_backend']);
    _authorityId(raw['expected_instance']);
    final harness = raw['harness_version'], profile = raw['profile_id'];
    final catalog = raw.containsKey('catalog_binding')
        ? AgentCatalogBinding.fromJson(raw['catalog_binding'])
        : null;
    if (catalog != null && profile != null) throw const FormatException();
    if ((harness != null &&
            (harness is! String ||
                !RegExp(r'^(seed-v1|v-[a-f0-9]{24})$').hasMatch(harness))) ||
        (profile != null &&
            (profile is! String ||
                profile.isEmpty ||
                profile.length > 80 ||
                profile.trim().isEmpty ||
                !_scalarText(profile) ||
                profile.codeUnits.any((c) => c < 32 || c == 127))) ||
        (raw['reasoning'] != null &&
            !const {
              'none',
              'minimal',
              'low',
              'medium',
              'high',
              'xhigh',
            }.contains(raw['reasoning'])) ||
        (raw.containsKey('auxiliary_task') &&
            !agentModelPurposes.contains(raw['auxiliary_task']))) {
      throw const FormatException('Invalid model change');
    }
    body = Map.unmodifiable({
      ...raw,
      if (catalog != null)
        'catalog_binding': Map.unmodifiable(catalog.toJson()),
    });
  }
  late final Map<String, Object?> body;
  String get backendId => body['expected_backend'] as String;
  String get instanceId => body['expected_instance'] as String;
  String get key => body['idempotency_key'] as String;
  int get revision => body['expected_revision'] as int;
  bool matches(AgentModelChange other) =>
      body.length == other.body.length &&
      body.keys.every(
        (key) =>
            other.body.containsKey(key) &&
            (key == 'catalog_binding'
                ? AgentCatalogBinding.fromJson(body[key]) ==
                      AgentCatalogBinding.fromJson(other.body[key])
                : body[key] == other.body[key]),
      );
}

class AgentModelReceipt {
  AgentModelReceipt.fromJson(Object? value, AgentModelChange original) {
    final body = original.body;
    final raw = _closedObject(value, {
      'schema_version',
      'backend_id',
      'service_instance',
      'idempotency_key',
      'revision',
      'harness_version',
      'binding',
      'change_boundary',
      if (body.containsKey('auxiliary_task')) 'auxiliary_task',
    });
    if (raw['schema_version'] != '1.1' ||
        raw['backend_id'] != original.backendId ||
        raw['service_instance'] != original.instanceId ||
        raw['idempotency_key'] != original.key ||
        raw['revision'] is! int ||
        raw['revision'] != original.revision + 1 ||
        raw['harness_version'] != body['harness_version'] ||
        raw['auxiliary_task'] != body['auxiliary_task'] ||
        raw['change_boundary'] != 'new_tasks') {
      throw const FormatException('Model receipt does not match');
    }
    final binding = raw['binding'];
    Map<String, Object?>? copied;
    if (body.containsKey('catalog_binding')) {
      copied = _closedObject(binding, {'catalog_binding', 'reasoning'});
      if (AgentCatalogBinding.fromJson(copied['catalog_binding']) !=
              AgentCatalogBinding.fromJson(body['catalog_binding']) ||
          copied['reasoning'] != body['reasoning']) {
        throw const FormatException('Model receipt does not match');
      }
      copied = {
        ...copied,
        'catalog_binding': Map.unmodifiable(
          AgentCatalogBinding.fromJson(copied['catalog_binding']).toJson(),
        ),
      };
    } else if (body['profile_id'] == null) {
      if (binding != null) throw const FormatException();
    } else {
      copied = _closedObject(binding, {'profile_id', 'reasoning'});
      if (copied['profile_id'] != body['profile_id'] ||
          copied['reasoning'] != body['reasoning']) {
        throw const FormatException('Model receipt does not match');
      }
    }
    json = Map.unmodifiable({
      ...raw,
      'binding': copied == null ? null : Map.unmodifiable(copied),
    });
  }
  late final Map<String, Object?> json;
}

class AgentModelLookup {
  AgentModelLookup.fromJson(Object? value, AgentModelChange original) {
    final raw = _closedObject(value, {
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
        !original.matches(AgentModelChange.fromJson(raw['request']))) {
      throw const FormatException('Model lookup does not match');
    }
    mutationFenced = raw['mutation_fenced'] as bool;
    if (raw['status'] == 'recorded') {
      if (!mutationFenced) throw const FormatException();
      receipt = AgentModelReceipt.fromJson(raw['receipt'], original);
    } else {
      if (raw['status'] != 'unobserved' || raw['receipt'] != null) {
        throw const FormatException('Invalid model lookup');
      }
      receipt = null;
    }
    json = Map.unmodifiable({
      ...raw,
      'request': original.body,
      'receipt': receipt?.json,
    });
  }
  late final AgentModelReceipt? receipt;
  late final bool mutationFenced;
  late final Map<String, Object?> json;
  bool get refused => receipt == null && mutationFenced;
}

class AgentModelBinding {
  const AgentModelBinding(String this.profileId, this.reasoning)
    : catalogBinding = null;
  const AgentModelBinding.catalog(
    AgentCatalogBinding this.catalogBinding,
    this.reasoning,
  ) : profileId = null;
  factory AgentModelBinding.fromJson(Object? value) {
    if (value is! Map<String, Object?> ||
        value.length != 2 ||
        !value.containsKey('reasoning')) {
      throw const FormatException();
    }
    final effort = _effort(value['reasoning']);
    if (value.containsKey('catalog_binding')) {
      return AgentModelBinding.catalog(
        AgentCatalogBinding.fromJson(value['catalog_binding']),
        effort,
      );
    }
    if (!value.containsKey('profile_id')) {
      throw const FormatException();
    }
    return AgentModelBinding(
      workspaceText(value['profile_id'], max: 80),
      effort,
    );
  }
  final String? profileId;
  final AgentCatalogBinding? catalogBinding;
  final String? reasoning;
  AgentModelReference get reference => catalogBinding == null
      ? AgentModelReference.host(profileId!)
      : AgentModelReference.catalog(catalogBinding!);
}

class AgentModelProfile {
  AgentModelProfile.fromJson(Object? value) {
    _read(value, catalog: false);
  }
  AgentModelProfile.fromCatalogJson(Object? value) {
    _read(value, catalog: true);
  }
  void _read(Object? value, {required bool catalog}) {
    if (value is! Map<String, Object?> || value['selectable'] is! bool) {
      throw const FormatException();
    }
    id = workspaceText(value['id'], max: 80);
    catalogBinding = catalog
        ? AgentCatalogBinding.fromJson(value['catalog_binding'])
        : null;
    if (catalog && catalogBinding!.id != id) throw const FormatException();
    reference = catalog
        ? AgentModelReference.catalog(catalogBinding!)
        : AgentModelReference.host(id);
    catalogLabel = catalog ? workspaceText(value['label'], max: 128) : null;
    reason = catalog ? workspaceText(value['reason'], max: 512) : null;
    if (catalog &&
        (value['qualification'] != 'not_checked' ||
            value['readiness'] != 'unknown')) {
      throw const FormatException('Unsupported catalog qualification');
    }
    selectable = value['selectable'] as bool;
    final auxiliary = value['auxiliary_selectable'];
    if (catalog
        ? auxiliary is! bool
        : auxiliary != null && auxiliary is! bool) {
      throw const FormatException();
    }
    auxiliarySelectable = auxiliary == true;
    final info = value['provider'];
    if (selectable || auxiliarySelectable || (catalog && info != null)) {
      if (info is! Map<String, Object?> || info['local'] is! bool) {
        throw const FormatException();
      }
      provider = workspaceText(info['provider'], max: 128);
      model = workspaceText(info['model'], max: 256);
      local = info['local'] as bool;
      providerInfo = Map.unmodifiable(info);
      if (catalog &&
          (info['schema_version'] != '1.0' || info['streaming'] is! bool)) {
        throw const FormatException('Invalid catalog provider metadata');
      }
    } else if (info != null) {
      throw const FormatException();
    }
    final levels = value['reasoning_options'];
    if (levels is! List ||
        levels.length > 6 ||
        levels.any((x) => x is! String || !_efforts.contains(x)) ||
        levels.toSet().length != levels.length) {
      throw const FormatException();
    }
    reasoningOptions = List.unmodifiable(
      catalog ? _efforts.where(levels.contains) : levels.cast<String>(),
    );
    reasoning = _effort(value['reasoning']);
  }

  late final String id;
  late final AgentCatalogBinding? catalogBinding;
  late final AgentModelReference reference;
  late final String? catalogLabel, reason;
  late final bool selectable;
  late final bool auxiliarySelectable;
  String? provider, model, reasoning;
  bool? local;
  Map<String, Object?>? providerInfo;
  late final List<String> reasoningOptions;
  String get label =>
      catalogLabel ?? (id == 'host-default' ? 'Host default' : id);
  String get sourceLabel => catalogBinding == null
      ? 'Host configuration'
      : 'Provider catalog · Generation ${catalogBinding!.generation}';
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
    writeAuthority = AgentModelAuthority.supported(raw['write_authority']);
    defaultBinding = AgentModelBinding.fromJson(raw['default']);
    profiles = List.unmodifiable(list.map(AgentModelProfile.fromJson));
    if (profiles.map((x) => x.id).toSet().length != profiles.length) {
      throw const FormatException();
    }
    final saved = raw.containsKey('catalog_profiles')
        ? raw['catalog_profiles']
        : const [];
    if (saved is! List || saved.length > 32) throw const FormatException();
    catalogProfiles = List.unmodifiable(
      saved.map(AgentModelProfile.fromCatalogJson),
    );
    catalogError = raw['catalog_error'] == null
        ? null
        : workspaceText(raw['catalog_error'], max: 512);
    if (catalogProfiles.map((profile) => profile.id).toSet().length !=
        catalogProfiles.length) {
      throw const FormatException('Repeated catalog model identity');
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
  late final AgentModelAuthority? writeAuthority;
  late final AgentModelBinding defaultBinding;
  late final List<AgentModelProfile> profiles;
  late final List<AgentModelProfile> catalogProfiles;
  late final String? catalogError;
  List<AgentModelProfile> get choices =>
      List.unmodifiable([...profiles, ...catalogProfiles]);
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

  AgentModelProfile? profileFor(AgentModelReference reference) {
    for (final row in choices) {
      if (row.reference == reference) return row;
    }
    return null;
  }

  AgentModelProfile? currentCatalogProfile(String id) {
    for (final row in catalogProfiles) {
      if (row.id == id) return row;
    }
    return null;
  }

  String bindingLabel(AgentModelBinding binding) {
    final selected = profileFor(binding.reference);
    final catalog = binding.catalogBinding;
    if (catalog != null) {
      return '${selected?.label ?? catalog.id} · Provider generation ${catalog.generation}'
          '${selected == null ? ' · Unavailable' : ''}';
    }
    return '${selected?.label ?? binding.profileId} · Host configuration';
  }
}
