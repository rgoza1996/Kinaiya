/// Strict, immutable Session operating-mode metadata and original-request proof.
library;

import 'dart:convert';

enum AgentMode {
  off('off', 'Off'),
  onDemand('on_demand', 'On-demand'),
  autonomous('autonomous', 'Autonomous');

  const AgentMode(this.wire, this.label);
  final String wire, label;
}

const agentActiveWorkPolicy = 'finish_already_accepted';
const _maxInteger = 9007199254740991;
const _invalid = FormatException('Invalid Agent operating-mode proof');

void _tree(Object? value, [int depth = 1, List<int>? count]) {
  count ??= [0];
  if (depth > 12 || ++count[0] > 2048) throw _invalid;
  if (value is Map<String, Object?>) {
    for (final entry in value.entries) {
      _tree(entry.key, depth + 1, count);
      _tree(entry.value, depth + 1, count);
    }
  } else if (value is List) {
    for (final item in value) {
      _tree(item, depth + 1, count);
    }
  } else if (value is int) {
    if (value.abs() > _maxInteger) throw _invalid;
  } else if (value != null && value is! String && value is! bool) {
    throw _invalid;
  }
}

Map<String, Object?> _closed(Object? value, Set<String> keys) {
  _tree(value);
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !value.keys.every(keys.contains)) {
    throw _invalid;
  }
  return value;
}

String _id(Object? value, String pattern) {
  if (value is! String || RegExp(pattern).stringMatch(value) != value) {
    throw _invalid;
  }
  return value;
}

String _backend(Object? value) => _id(value, r'^[a-f0-9]{32}$');
String _instance(Object? value) => _id(
  value,
  r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$',
);

int _revision(Object? value, int min, int max) {
  if (value is! int || value < min || value > max) throw _invalid;
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) throw _invalid;
  return value;
}

AgentMode _mode(Object? value) {
  for (final mode in AgentMode.values) {
    if (value == mode.wire) return mode;
  }
  throw _invalid;
}

void _versionAndPolicy(Map<String, Object?> raw) {
  if (raw['schema_version'] != '1.0' ||
      raw['active_work_policy'] != agentActiveWorkPolicy) {
    throw _invalid;
  }
}

Map<String, Object?> _bounded(Map<String, Object?> raw, int bytes) {
  if (utf8.encode(jsonEncode(raw)).length > bytes) throw _invalid;
  return Map.unmodifiable(raw);
}

/// The complete seven fields are detached once and reused for every attempt.
class AgentModeChange {
  AgentModeChange.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'idempotency_key',
      'expected_backend',
      'expected_instance',
      'expected_revision',
      'mode',
      'active_work_policy',
    });
    _versionAndPolicy(raw);
    key = _id(raw['idempotency_key'], r'^[A-Za-z0-9:._-]{1,128}$');
    backendId = _backend(raw['expected_backend']);
    instanceId = _instance(raw['expected_instance']);
    revision = _revision(raw['expected_revision'], 0, _maxInteger - 1);
    mode = _mode(raw['mode']);
    // Sorted keys also make the retained wire body canonical compact JSON.
    body = _bounded({
      'active_work_policy': agentActiveWorkPolicy,
      'expected_backend': backendId,
      'expected_instance': instanceId,
      'expected_revision': revision,
      'idempotency_key': key,
      'mode': mode.wire,
      'schema_version': '1.0',
    }, 4096);
  }

  late final String key, backendId, instanceId;
  late final int revision;
  late final AgentMode mode;
  late final Map<String, Object?> body;

  bool matches(AgentModeChange other) =>
      body.keys.every((key) => body[key] == other.body[key]);
}

class AgentControlSnapshot {
  AgentControlSnapshot.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'revision',
      'mode',
      'active_work_policy',
      'control_writable',
      'available_modes',
      'standing_work_available',
    });
    _versionAndPolicy(raw);
    backendId = _backend(raw['backend_id']);
    instanceId = _instance(raw['instance_id']);
    revision = _revision(raw['revision'], 0, _maxInteger);
    mode = _mode(raw['mode']);
    controlWritable = _flag(raw['control_writable']);
    standingWorkAvailable = _flag(raw['standing_work_available']);
    final modes = raw['available_modes'];
    final expected = [
      AgentMode.off,
      AgentMode.onDemand,
      if (standingWorkAvailable) AgentMode.autonomous,
    ];
    if (modes is! List ||
        modes.length != expected.length ||
        !List.generate(
          modes.length,
          (i) => modes[i] == expected[i].wire,
        ).every((value) => value)) {
      throw _invalid;
    }
    availableModes = List.unmodifiable(expected);
    json = _bounded({
      ...raw,
      'available_modes': List.unmodifiable(expected.map((value) => value.wire)),
    }, 16384);
  }

  late final String backendId, instanceId;
  late final int revision;
  late final AgentMode mode;
  // Serving-controller eligibility is separate from credential permission.
  late final bool controlWritable, standingWorkAvailable;
  late final List<AgentMode> availableModes;
  late final Map<String, Object?> json;

  bool matches(AgentControlSnapshot other) =>
      backendId == other.backendId &&
      instanceId == other.instanceId &&
      revision == other.revision &&
      mode == other.mode &&
      controlWritable == other.controlWritable &&
      standingWorkAvailable == other.standingWorkAvailable;
}

/// Historical acknowledgment; it never supplies the currently active mode.
class AgentModeReceipt {
  AgentModeReceipt.fromJson(Object? value, AgentModeChange original) {
    final raw = _closed(value, {
      'schema_version',
      'status',
      'backend_id',
      'service_instance',
      'revision',
      'request',
    });
    final echo = AgentModeChange.fromJson(raw['request']);
    if (raw['schema_version'] != '1.0' ||
        raw['status'] != 'mode_saved' ||
        _backend(raw['backend_id']) != original.backendId ||
        _instance(raw['service_instance']) != original.instanceId ||
        _revision(raw['revision'], 1, _maxInteger) != original.revision + 1 ||
        !echo.matches(original)) {
      throw _invalid;
    }
    request = original;
    revision = original.revision + 1;
    json = _bounded({...raw, 'request': original.body}, 16384);
  }

  late final AgentModeChange request;
  late final int revision;
  late final Map<String, Object?> json;
}

/// Only this serialized native observation can permanently fence an unknown save.
class AgentModeLookupResult {
  AgentModeLookupResult.fromJson(Object? value, AgentModeChange original) {
    final raw = _closed(value, {
      'schema_version',
      'status',
      'request',
      'state',
      'receipt',
      'mutation_fenced',
    });
    final echo = AgentModeChange.fromJson(raw['request']);
    state = AgentControlSnapshot.fromJson(raw['state']);
    mutationFenced = _flag(raw['mutation_fenced']);
    if (raw['schema_version'] != '1.0' ||
        !echo.matches(original) ||
        state.backendId != original.backendId) {
      throw _invalid;
    }
    if (raw['status'] == 'recorded') {
      receipt = AgentModeReceipt.fromJson(raw['receipt'], original);
      if (state.revision < receipt!.revision ||
          (state.revision == receipt!.revision &&
              state.mode != original.mode)) {
        throw _invalid;
      }
    } else {
      if (raw['status'] != 'unobserved' || raw['receipt'] != null) {
        throw _invalid;
      }
      receipt = null;
    }
    // Native must prove issuance/retirement before emitting this envelope.
    // A different UUID from a standalone state GET is never such a proof.
    final fenced =
        receipt != null ||
        state.instanceId != original.instanceId ||
        state.revision > original.revision;
    if (mutationFenced != fenced) throw _invalid;
    json = _bounded({
      ...raw,
      'request': original.body,
      'state': state.json,
      'receipt': receipt?.json,
    }, 16384);
  }

  late final AgentControlSnapshot state;
  late final AgentModeReceipt? receipt;
  late final bool mutationFenced;
  late final Map<String, Object?> json;
  bool get refused => receipt == null && mutationFenced;
}
