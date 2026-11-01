/// Read-only native measurements. These are neither live allocation nor billing.
library;

import 'dart:convert';

const _max = 9007199254740991;
Never _bad() => throw const FormatException('Invalid context observations');
Map<String, Object?> _map(Object? value, Set<String> keys) {
  if (value is! Map<String, Object?> ||
      value.length != keys.length ||
      !keys.containsAll(value.keys)) {
    _bad();
  }
  return value;
}

String _text(Object? value, [int chars = 128]) {
  if (value is! String ||
      value.isEmpty ||
      value.runes.length > chars ||
      utf8.encode(value).length > 1024 ||
      value.codeUnits.any((c) => c < 32 || c == 127)) {
    _bad();
  }
  return value;
}

String _match(Object? value, String expression, int length) {
  final text = _text(value, length),
      match = RegExp(expression).matchAsPrefix(value as String);
  if (match == null || match.end != text.length) _bad();
  return text;
}

int _int(Object? value, [int min = 0, int max = _max]) {
  if (value is! int || value < min || value > max) _bad();
  return value;
}

bool _bool(Object? value) {
  if (value is! bool) _bad();
  return value;
}

String _choice(Object? value, Set<String> choices) {
  if (value is! String || !choices.contains(value)) _bad();
  return value;
}

class ContextCatalogPin {
  ContextCatalogPin.fromJson(Object? value) {
    final raw = _map(value, {'id', 'generation', 'configuration_sha256'});
    id = _match(raw['id'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', 64);
    generation = _int(raw['generation'], 1, 64);
    digest = _match(raw['configuration_sha256'], r'[a-f0-9]{64}', 64);
  }
  late final String id, digest;
  late final int generation;
}

class ContextModelPin {
  ContextModelPin.fromJson(Object? value) {
    final raw = _map(value, {
      'source',
      'provider',
      'model',
      'local',
      'reasoning',
      'settings_revision',
      'catalog_revision',
      'catalog_binding',
      'profile_id',
    });
    source = _choice(raw['source'], {'host', 'catalog', 'unmanaged'});
    provider = _text(raw['provider'], 256);
    model = _text(raw['model'], 256);
    local = _bool(raw['local']);
    reasoning = raw['reasoning'] == null ? null : _text(raw['reasoning'], 16);
    settingsRevision = raw['settings_revision'] == null
        ? null
        : _int(raw['settings_revision']);
    catalogRevision = raw['catalog_revision'] == null
        ? null
        : _int(raw['catalog_revision']);
    catalog = raw['catalog_binding'] == null
        ? null
        : ContextCatalogPin.fromJson(raw['catalog_binding']);
    profileId = raw['profile_id'] == null ? null : _text(raw['profile_id'], 80);
    if ((source == 'catalog'
            ? catalog == null || catalogRevision == null
            : catalog != null || catalogRevision != null) ||
        ((source == 'unmanaged') != (settingsRevision == null))) {
      _bad();
    }
  }
  late final String source, provider, model;
  late final bool local;
  late final String? reasoning, profileId;
  late final int? settingsRevision, catalogRevision;
  late final ContextCatalogPin? catalog;
}

class ContextObservation {
  ContextObservation.fromJson(Object? value) {
    final raw = _map(value, {
      'schema_version',
      'id',
      'operation_id',
      'sequence',
      'role',
      'phase',
      'observed_at',
      'scope',
      'model',
      'measurement',
      'selection',
    });
    if (raw['schema_version'] != '1.0' ||
        utf8.encode(jsonEncode(raw)).length > 8000) {
      _bad();
    }
    id = _match(raw['id'], r'[a-f0-9]{32}', 32);
    operationId = _match(raw['operation_id'], r'[a-f0-9]{32}', 32);
    sequence = _int(raw['sequence'], 1);
    role = _choice(raw['role'], {'agent', 'converse'});
    phase = _choice(raw['phase'], {'prepared', 'generation_export_admitted'});
    observedAt = _text(raw['observed_at'], 40);
    if (!observedAt.endsWith('Z') ||
        DateTime.tryParse(observedAt)?.isUtc != true) {
      _bad();
    }
    final scope = _map(raw['scope'], {
      'session_id',
      'thread_id',
      'epoch',
      'source_event_ids',
      'task_id',
      'objective_revision',
      'harness_version',
      'through_sequence',
    });
    sessionId = _text(scope['session_id']);
    threadId = _text(scope['thread_id']);
    epoch = _int(scope['epoch'], 1);
    final sources = scope['source_event_ids'];
    if (sources is! List || sources.length > 128) _bad();
    sourceEventIds = List.unmodifiable(sources.map((v) => _text(v)));
    if (sourceEventIds.toSet().length != sourceEventIds.length) _bad();
    taskId = scope['task_id'] == null ? null : _text(scope['task_id']);
    objectiveRevision = scope['objective_revision'] == null
        ? null
        : _int(scope['objective_revision'], 1);
    harnessVersion = scope['harness_version'] == null
        ? null
        : _text(scope['harness_version']);
    throughSequence = scope['through_sequence'] == null
        ? null
        : _int(scope['through_sequence'], 1);
    model = ContextModelPin.fromJson(raw['model']);
    final count = _map(raw['measurement'], {
      'input_tokens',
      'context_tokens',
      'reserved_output_tokens',
      'headroom_tokens',
      'request_sha256',
      'binding_sha256',
    });
    input = _int(count['input_tokens'], 1);
    capacity = _int(count['context_tokens'], 64);
    reserve = _int(count['reserved_output_tokens'], 16);
    headroom = _int(count['headroom_tokens']);
    requestDigest = _match(count['request_sha256'], r'[a-f0-9]{64}', 64);
    bindingDigest = _match(count['binding_sha256'], r'[a-f0-9]{64}', 64);
    if (capacity - input - reserve != headroom) _bad();
    final selection = _map(raw['selection'], {
      'unit',
      'kept',
      'dropped',
      'examined',
      'history_complete',
      'measurement_attempts',
    });
    unit = _choice(selection['unit'], {'message', 'exchange'});
    kept = _int(selection['kept']);
    dropped = _int(selection['dropped']);
    examined = _int(selection['examined']);
    historyComplete = _bool(selection['history_complete']);
    attempts = _int(selection['measurement_attempts'], 1, 513);
    if (kept + dropped != examined ||
        ((taskId == null) != (objectiveRevision == null)) ||
        (role == 'agent'
            ? taskId == null || unit != 'exchange'
            : taskId != null || unit != 'message' || phase != 'prepared')) {
      _bad();
    }
  }
  late final String id,
      operationId,
      role,
      phase,
      observedAt,
      sessionId,
      threadId,
      unit,
      requestDigest,
      bindingDigest;
  late final String? taskId, harnessVersion;
  late final int sequence,
      epoch,
      input,
      capacity,
      reserve,
      headroom,
      kept,
      dropped,
      examined,
      attempts;
  late final int? objectiveRevision, throughSequence;
  late final List<String> sourceEventIds;
  late final bool historyComplete;
  late final ContextModelPin model;
}

class ContextUsageRow {
  ContextUsageRow.fromJson(Object? value) {
    final raw = _map(value, {'observation', 'configuration_status'});
    observation = ContextObservation.fromJson(raw['observation']);
    configurationStatus = _choice(raw['configuration_status'], {
      'current',
      'obsolete',
      'unknown',
    });
  }
  late final ContextObservation observation;
  late final String configurationStatus;
}

class ContextUsageSnapshot {
  ContextUsageSnapshot.fromJson(Object? value) {
    final raw = _map(value, {
      'schema_version',
      'role',
      'instance_id',
      'latest_sequence',
      'retained_count',
      'storage',
      'measurement_mode',
      'observations',
    });
    if (raw['schema_version'] != '1.0' || raw['storage'] != 'process_memory') {
      _bad();
    }
    role = _choice(raw['role'], {'agent', 'converse'});
    instanceId = _match(raw['instance_id'], r'[a-f0-9]{32}', 32);
    latest = _int(raw['latest_sequence']);
    retained = _int(raw['retained_count'], 0, 128);
    mode = _choice(raw['measurement_mode'], {
      'measured',
      'legacy_unmeasured',
      'unknown',
    });
    final values = raw['observations'];
    if (values is! List || values.length > 32) _bad();
    rows = List.unmodifiable(values.map(ContextUsageRow.fromJson));
    if (retained != (latest < 128 ? latest : 128) ||
        rows.length != (retained < 32 ? retained : 32)) {
      _bad();
    }
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].observation.sequence != latest - i ||
          rows[i].observation.role != role) {
        _bad();
      }
    }
  }
  late final String role, instanceId, mode;
  late final int latest, retained;
  late final List<ContextUsageRow> rows;
}
