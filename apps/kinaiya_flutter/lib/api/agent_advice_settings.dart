part of 'decisions_model_settings.dart';

const _agentAdviceChangeLimit = 1000;
const _agentAdviceBoundary = 'restart_required';
const _agentAdviceProtocol = 'measured_agent_advice_v1';
const agentAdviceInstructionLimit = 8192;
const agentAdviceReasoning = [
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
];

void _agentAdviceVersion(Map<String, Object?> value) {
  _integer(value['contract_version'], 1, 1);
}

String _agentAdviceDigest(Map<String, Object?> canonical) =>
    sha256.convert(utf8.encode(jsonEncode(canonical))).toString();

enum AgentAdviceSource {
  host,
  disabled,
  saved;

  static AgentAdviceSource fromWire(Object? value) =>
      values.firstWhere((source) => value == source.name, orElse: _bad);
}

class AgentAdvicePolicy {
  AgentAdvicePolicy({
    required this.binding,
    required this.instructions,
    required this.contentScope,
    this.reasoning,
  }) {
    _requireWellFormedUnicode(instructions);
    if (instructions.contains('\u0000') ||
        utf8.encode(instructions).length > agentAdviceInstructionLimit ||
        (reasoning != null && !agentAdviceReasoning.contains(reasoning))) {
      _bad();
    }
  }

  factory AgentAdvicePolicy.fromJson(Object? value) {
    final raw = _object(value, {
      'catalog_binding',
      'consumer',
      'content_scope',
      'contract_version',
      'instructions',
      'protocol',
      'reasoning',
    });
    _agentAdviceVersion(raw);
    final instructions = raw['instructions'], reasoning = raw['reasoning'];
    if (raw['consumer'] != 'agent' ||
        raw['protocol'] != _agentAdviceProtocol ||
        instructions is! String ||
        (reasoning != null && reasoning is! String)) {
      _bad();
    }
    return AgentAdvicePolicy(
      binding: DecisionsCatalogBinding.fromJson(raw['catalog_binding']),
      instructions: instructions,
      contentScope: ContentScope.values.firstWhere(
        (scope) => scope.wireValue == raw['content_scope'],
        orElse: _bad,
      ),
      reasoning: reasoning as String?,
    );
  }

  final DecisionsCatalogBinding binding;
  final String instructions;
  final ContentScope contentScope;
  final String? reasoning;

  // Lexical key order matches the canonical UTF-8 native contract at every level.
  Map<String, Object?> get body => Map.unmodifiable({
    'catalog_binding': Map<String, Object?>.unmodifiable({
      'configuration_sha256': binding.configurationSha256,
      'generation': binding.generation,
      'id': binding.id,
    }),
    'consumer': 'agent',
    'content_scope': contentScope.wireValue,
    'contract_version': 1,
    'instructions': instructions,
    'protocol': _agentAdviceProtocol,
    'reasoning': reasoning,
  });
  late final String digest = _agentAdviceDigest(body);
}

class AgentAdviceObservation {
  AgentAdviceObservation.fromJson(Object? value, {bool host = false}) {
    final raw = _object(value, {
      'contract_version',
      'source',
      'policy',
      'policy_sha256',
      'catalog_status',
      if (host) ...{
        'owner',
        'consumer',
        'observation',
        'change_boundary',
        'request_admission',
      },
    });
    _agentAdviceVersion(raw);
    if (host &&
        (raw['owner'] != 'decisions' ||
            raw['consumer'] != 'agent' ||
            raw['observation'] != 'catalog_metadata_only' ||
            raw['change_boundary'] != _agentAdviceBoundary ||
            raw['request_admission'] != 'unavailable')) {
      _bad();
    }
    source = _match(
      raw['source'],
      host
          ? r'host|unconfigured|invalid_host'
          : r'host|unconfigured|invalid_host|disabled|saved|unavailable',
    );
    catalogStatus = _match(
      raw['catalog_status'],
      r'eligible|unavailable|not_selected',
    );
    policy = raw['policy'] == null
        ? null
        : AgentAdvicePolicy.fromJson(raw['policy']);
    policyDigest = raw['policy_sha256'] == null
        ? null
        : _hash(raw['policy_sha256']);
    final isSelected = source == 'host' || source == 'saved';
    if (isSelected != (policy != null) ||
        isSelected == (catalogStatus == 'not_selected') ||
        policy?.digest != policyDigest) {
      _bad();
    }
  }

  late final String source, catalogStatus;
  late final AgentAdvicePolicy? policy;
  late final String? policyDigest;

  bool sameDeclaration(AgentAdviceObservation other) =>
      source == other.source && policyDigest == other.policyDigest;
}

class AgentAdviceSettingsSnapshot {
  AgentAdviceSettingsSnapshot.fromJson(Object? value) {
    final raw = _object(value, {
      'contract_version',
      'owner',
      'consumer',
      'backend_id',
      'instance_id',
      'revision',
      'selection_source',
      'host',
      'active',
      'next_launch',
      'change_boundary',
      'writable',
    });
    _agentAdviceVersion(raw);
    if (raw['owner'] != 'decisions' ||
        raw['consumer'] != 'agent' ||
        raw['change_boundary'] != _agentAdviceBoundary) {
      _bad();
    }
    backendId = _authority(raw['backend_id']);
    instanceId = _authority(raw['instance_id']);
    revision = _integer(raw['revision'], 0, _agentAdviceChangeLimit);
    writable = _boolean(raw['writable']);
    selection = AgentAdviceSource.fromWire(raw['selection_source']);
    host = AgentAdviceObservation.fromJson(raw['host'], host: true);
    active = AgentAdviceObservation.fromJson(raw['active']);
    nextLaunch = AgentAdviceObservation.fromJson(raw['next_launch']);
    if (writable != (revision < _agentAdviceChangeLimit) ||
        (selection == AgentAdviceSource.host
            ? !nextLaunch.sameDeclaration(host) ||
                  nextLaunch.catalogStatus != host.catalogStatus
            : nextLaunch.source != selection.name)) {
      _bad();
    }
  }

  late final String backendId, instanceId;
  late final int revision;
  late final bool writable;
  late final AgentAdviceSource selection;
  late final AgentAdviceObservation host, active, nextLaunch;

  bool sameRevision(AgentAdviceSettingsSnapshot other) =>
      backendId == other.backendId &&
      instanceId == other.instanceId &&
      revision == other.revision;
}

class AgentAdvicePolicyChange {
  AgentAdvicePolicyChange({
    required this.base,
    required String key,
    required this.source,
    this.policy,
  }) : key = decisionsModelKey(key) {
    if (!base.writable ||
        (source == AgentAdviceSource.saved) != (policy != null)) {
      _bad();
    }
  }

  final AgentAdviceSettingsSnapshot base;
  final String key;
  final AgentAdviceSource source;
  final AgentAdvicePolicy? policy;

  late final Map<String, Object?> body = Map.unmodifiable({
    'contract_version': 1,
    'expected_backend': base.backendId,
    'expected_instance': base.instanceId,
    'expected_revision': base.revision,
    'idempotency_key': key,
    'selection': Map<String, Object?>.unmodifiable({
      'contract_version': 1,
      'policy': policy?.body,
      'source': source.name,
    }),
  });
  late final String digest = _agentAdviceDigest(body);
}

class AgentAdviceSaveReceipt {
  AgentAdviceSaveReceipt.fromJson(
    Object? value,
    AgentAdvicePolicyChange original,
  ) {
    final raw = _object(value, {
      'contract_version',
      'status',
      'backend_id',
      'instance_id',
      'revision',
      'idempotency_key',
      'request_sha256',
      'selection_source',
      'policy_sha256',
      'change_boundary',
    });
    _agentAdviceVersion(raw);
    revision = _integer(raw['revision'], 1, _agentAdviceChangeLimit);
    if (raw['status'] != 'saved' ||
        raw['change_boundary'] != _agentAdviceBoundary ||
        raw['backend_id'] != original.base.backendId ||
        raw['instance_id'] != original.base.instanceId ||
        revision != original.base.revision + 1 ||
        raw['idempotency_key'] != original.key ||
        raw['request_sha256'] != original.digest ||
        raw['selection_source'] != original.source.name ||
        raw['policy_sha256'] != original.policy?.digest) {
      _bad();
    }
  }

  late final int revision;
}

void validateAgentAdviceSaveRefusal(
  Object? value,
  AgentAdvicePolicyChange original,
) {
  final raw = _object(value, {
    'contract_version',
    'status',
    'request_sha256',
    'reason',
  });
  _agentAdviceVersion(raw);
  if (raw['status'] != 'refused' ||
      raw['request_sha256'] != original.digest ||
      raw['reason'] != 'selection_unavailable') {
    _bad();
  }
}

class AgentAdviceSaveLookup {
  AgentAdviceSaveLookup.fromJson(
    Object? value,
    AgentAdvicePolicyChange original,
  ) {
    final raw = _object(value, {
      'contract_version',
      'backend_id',
      'instance_id',
      'current_revision',
      'request_sha256',
      'receipt',
      'mutation_fenced',
    });
    _agentAdviceVersion(raw);
    if (raw['backend_id'] != original.base.backendId ||
        raw['request_sha256'] != original.digest) {
      _bad();
    }
    final instance = _authority(raw['instance_id']);
    final revision = _integer(
      raw['current_revision'],
      0,
      _agentAdviceChangeLimit,
    );
    receipt = raw['receipt'] == null
        ? null
        : AgentAdviceSaveReceipt.fromJson(raw['receipt'], original);
    fenced = _boolean(raw['mutation_fenced']);
    if (fenced !=
            (receipt == null &&
                (instance != original.base.instanceId ||
                    revision > original.base.revision ||
                    revision == _agentAdviceChangeLimit)) ||
        (receipt != null && receipt!.revision > revision)) {
      _bad();
    }
  }

  late final AgentAdviceSaveReceipt? receipt;
  late final bool fenced;
}
