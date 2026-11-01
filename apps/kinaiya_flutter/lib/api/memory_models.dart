/// Strict, detached owner-facing Converse memory wire values (ADR0052).
library;

import 'dart:convert';

const memoryMaxSafeInteger = 9007199254740991;
const _bindingKeys = {'schema_version', 'session_id', 'thread_id', 'epoch'};
Never _invalid() => throw const FormatException('Invalid memory response');

Map<String, Object?> _object(Object? value, Set<String> keys) {
  if (value is! Map || value.length != keys.length) _invalid();
  final result = <String, Object?>{};
  for (final entry in value.entries) {
    if (entry.key is! String || !keys.contains(entry.key)) _invalid();
    result[entry.key as String] = entry.value;
  }
  if (result['schema_version'] != '1.0') _invalid();
  return result;
}

int _integer(Object? value, {int min = 0}) {
  if (value is! int || value < min || value > memoryMaxSafeInteger) _invalid();
  return value;
}

bool _boolean(Object? value) {
  if (value is! bool) _invalid();
  return value;
}

/// Checks code units too: utf8.encode alone replaces unpaired surrogates.
String _string(Object? value, {int min = 0, int max = 2048}) {
  if (value is! String) _invalid();
  for (var i = 0; i < value.length; i++) {
    final c = value.codeUnitAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      if (++i >= value.length) _invalid();
      final low = value.codeUnitAt(i);
      if (low < 0xdc00 || low > 0xdfff) _invalid();
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      _invalid();
    }
  }
  final size = utf8.encode(value).length;
  if (size < min || size > max) _invalid();
  return value;
}

String memoryIdentifier(Object? value) {
  final s = _string(value, min: 1, max: 128);
  final match = RegExp(r'^[A-Za-z0-9_.:-]+$').firstMatch(s);
  if (match == null || match.end != s.length) _invalid();
  return s;
}

String _enum(Object? value, Set<String> choices) {
  if (value is! String || !choices.contains(value)) _invalid();
  return value;
}

String _text(Object? value, {required int max, bool nonblank = false}) {
  final s = _string(value, min: nonblank ? 1 : 0, max: max);
  if (nonblank && s.trim().isEmpty) _invalid();
  if (s.runes.any(
    (c) => (c < 32 || (c >= 127 && c <= 159)) && c != 9 && c != 10,
  )) {
    _invalid();
  }
  return s;
}

List<String> _identifiers(Object? value) {
  if (value is! List || value.length > 8) _invalid();
  final result = value.map(memoryIdentifier).toList();
  if (result.toSet().length != result.length) _invalid();
  return List.unmodifiable(result);
}

class MemoryBinding {
  MemoryBinding(String sessionId, String threadId, int epoch)
    : sessionId = memoryIdentifier(sessionId),
      threadId = memoryIdentifier(threadId),
      epoch = _integer(epoch, min: 1);

  factory MemoryBinding.fromJson(Object? value) =>
      _from(_object(value, _bindingKeys));
  static MemoryBinding _from(Map<String, Object?> m) => MemoryBinding(
    memoryIdentifier(m['session_id']),
    memoryIdentifier(m['thread_id']),
    _integer(m['epoch'], min: 1),
  );
  final String sessionId;
  final String threadId;
  final int epoch;
  Map<String, Object?> toJson() => {
    'schema_version': '1.0',
    'session_id': sessionId,
    'thread_id': threadId,
    'epoch': epoch,
  };
  @override
  bool operator ==(Object other) =>
      other is MemoryBinding &&
      sessionId == other.sessionId &&
      threadId == other.threadId &&
      epoch == other.epoch;
  @override
  int get hashCode => Object.hash(sessionId, threadId, epoch);
}

class MemoryNamespaceState {
  MemoryNamespaceState._(
    this.binding,
    this.enabled,
    this.revision,
    this.nodeCount,
    this.status,
  );
  factory MemoryNamespaceState.fromJson(Object? value) {
    final m = _object(value, {
      ..._bindingKeys,
      'enabled',
      'revision',
      'node_count',
      'status',
    });
    return MemoryNamespaceState._(
      MemoryBinding._from(m),
      _boolean(m['enabled']),
      _integer(m['revision']),
      _integer(m['node_count']),
      _enum(m['status'], {'ready', 'quarantined'}),
    );
  }
  final MemoryBinding binding;
  final bool enabled;
  final int revision;
  final int nodeCount;
  final String status;
}

class MemorySummary {
  MemorySummary._(
    this.nodeId,
    this.ordinal,
    this.kind,
    this.state,
    this.preview,
    this.previewTruncated,
  );
  factory MemorySummary.fromJson(Object? value) {
    final m = _object(value, {
      'schema_version',
      'node_id',
      'ordinal',
      'kind',
      'state',
      'preview',
      'preview_truncated',
    });
    final state = _enum(m['state'], {
      'active',
      'invalidated',
      'tainted',
      'forgotten',
    });
    final preview = m['preview'] == null
        ? null
        : _text(m['preview'], max: 128, nonblank: false);
    final truncated = _boolean(m['preview_truncated']);
    if (state == 'forgotten'
        ? preview != null || truncated
        : preview == null || preview.isEmpty) {
      _invalid();
    }
    return MemorySummary._(
      memoryIdentifier(m['node_id']),
      _integer(m['ordinal'], min: 1),
      _enum(m['kind'], {'assertion', 'observation', 'inference'}),
      state,
      preview,
      truncated,
    );
  }
  final String nodeId;
  final int ordinal;
  final String kind;
  final String state;
  final String? preview;
  final bool previewTruncated;
}

class MemoryCatalog {
  MemoryCatalog._(this.state, this.entries);
  factory MemoryCatalog.fromJson(Object? value) {
    final m = _object(value, {'schema_version', 'state', 'entries'});
    final state = MemoryNamespaceState.fromJson(m['state']);
    final raw = m['entries'];
    if (raw is! List || raw.length > 256 || raw.length != state.nodeCount) {
      _invalid();
    }
    final entries = raw.map(MemorySummary.fromJson).toList();
    if (entries.map((e) => e.nodeId).toSet().length != entries.length) {
      _invalid();
    }
    for (var i = 0; i < entries.length; i++) {
      if (entries[i].ordinal != i + 1) _invalid();
    }
    return MemoryCatalog._(state, List.unmodifiable(entries));
  }
  final MemoryNamespaceState state;
  MemoryBinding get binding => state.binding;
  final List<MemorySummary> entries;
}

class MemoryUseState {
  MemoryUseState._(
    this.binding,
    this.runtimeId,
    this.useGeneration,
    this.enabled,
    this.implementationId,
    this.selectorIds,
    this.pending,
    this.notice,
  );
  factory MemoryUseState.fromJson(Object? value) {
    final m = _object(value, {
      ..._bindingKeys,
      'runtime_id',
      'use_generation',
      'enabled',
      'implementation_id',
      'selector_ids',
      'pending',
      'notice',
    });
    final enabled = _boolean(m['enabled']);
    final implementation = m['implementation_id'] == null
        ? null
        : memoryIdentifier(m['implementation_id']);
    if (enabled && implementation == null) _invalid();
    return MemoryUseState._(
      MemoryBinding._from(m),
      memoryIdentifier(m['runtime_id']),
      _integer(m['use_generation']),
      enabled,
      implementation,
      _identifiers(m['selector_ids']),
      _boolean(m['pending']),
      _enum(m['notice'], {
        'disabled',
        'ready',
        'unavailable',
        'context_omitted',
      }),
    );
  }
  final MemoryBinding binding;
  final String runtimeId;
  final int useGeneration;
  final bool enabled;
  final String? implementationId;
  final List<String> selectorIds;
  final bool pending;
  final String notice;
}

class MemoryFact {
  MemoryFact._(this.key, this.value);
  factory MemoryFact(String key, Object? value) => MemoryFact.fromJson({
    'schema_version': '1.0',
    'key': key,
    'value': value,
  });
  factory MemoryFact.fromJson(Object? value) {
    final m = _object(value, {'schema_version', 'key', 'value'});
    final key = _string(m['key'], min: 1, max: 128);
    final match = RegExp(r'^[A-Za-z0-9_.:/-]+$').firstMatch(key);
    if (match == null || match.end != key.length) _invalid();
    final v = m['value'];
    if (v is String) {
      _text(v, max: 512);
    } else if (v is int) {
      _integer(v, min: -memoryMaxSafeInteger);
    } else if (v != null && v is! bool) {
      _invalid();
    }
    return MemoryFact._(key, v);
  }
  final String key;
  final Object? value;
  Map<String, Object?> toJson() => {
    'schema_version': '1.0',
    'key': key,
    'value': value,
  };
  bool matches(MemoryFact? other) =>
      other != null && key == other.key && value == other.value;
}

String memoryOperationKey(Object? value) {
  final key = _string(value, min: 1, max: 128);
  final match = RegExp(r'^[!-~]+$').firstMatch(key);
  if (match == null || match.end != key.length) _invalid();
  return key;
}

/// Detached exact native request, reusable for explicit same-key reconciliation.
Map<String, Object?> memoryContentRequest(Object? value, String operation) {
  final capture = operation == 'capture';
  if (!capture && operation != 'forget') _invalid();
  final m = _object(value, {
    ..._bindingKeys,
    'expected_revision',
    'idempotency_key',
    if (capture) ...{
      'kind',
      'source_event_id',
      'source_revision',
      'body',
      'fact',
      'prerequisite_ids',
    } else
      'node_id',
  });
  final result = <String, Object?>{
    ...MemoryBinding._from(m).toJson(),
    'expected_revision': _integer(m['expected_revision']),
    'idempotency_key': memoryOperationKey(m['idempotency_key']),
  };
  if (!capture) {
    result['node_id'] = memoryIdentifier(m['node_id']);
    return Map.unmodifiable(result);
  }
  final kind = _enum(m['kind'], {'assertion', 'observation', 'inference'});
  final fact = m['fact'] == null ? null : MemoryFact.fromJson(m['fact']);
  final parents = _identifiers(m['prerequisite_ids']);
  if (kind == 'inference' ? fact != null || parents.isEmpty : fact == null) {
    _invalid();
  }
  result.addAll({
    'kind': kind,
    'source_event_id': memoryIdentifier(m['source_event_id']),
    'source_revision': _integer(m['source_revision'], min: 1),
    'body': _text(m['body'], max: 2048, nonblank: true),
    'fact': fact == null ? null : Map.unmodifiable(fact.toJson()),
    'prerequisite_ids': parents,
  });
  return Map.unmodifiable(result);
}

class MemoryForgetResult {
  MemoryForgetResult._(this.state, this.forgottenIds);
  factory MemoryForgetResult.fromJson(Object? value) {
    final m = _object(value, {'schema_version', 'state', 'forgotten_ids'});
    final list = m['forgotten_ids'];
    if (list is! List || list.length > 256) _invalid();
    final ids = list.map(memoryIdentifier).toList();
    if (ids.toSet().length != ids.length) _invalid();
    return MemoryForgetResult._(
      MemoryNamespaceState.fromJson(m['state']),
      List.unmodifiable(ids),
    );
  }
  final MemoryNamespaceState state;
  final List<String> forgottenIds;
  MemoryBinding get binding => state.binding;
}

class MemoryOperationReceipt {
  MemoryOperationReceipt._(this.operationId, this.revision, this.nodeId);
  factory MemoryOperationReceipt.fromJson(Object? value) {
    final m = _object(value, {
      'schema_version',
      'operation_id',
      'revision',
      'node_id',
    });
    final id = _string(m['operation_id'], min: 64, max: 64);
    if (!RegExp(r'^[a-f0-9]{64}$').hasMatch(id)) _invalid();
    return MemoryOperationReceipt._(
      id,
      _integer(m['revision'], min: 1),
      memoryIdentifier(m['node_id']),
    );
  }
  final String operationId, nodeId;
  final int revision;
}

class MemoryOperationLookup {
  MemoryOperationLookup._(
    this.binding,
    this.operation,
    this.idempotencyKey,
    this.status,
    this.receipt,
    this.disposition,
    this.mutationFenced,
  );
  factory MemoryOperationLookup.fromJson(Object? value) {
    final m = _object(value, {
      'schema_version',
      'binding',
      'operation',
      'idempotency_key',
      'status',
      'receipt',
      'disposition',
      'mutation_fenced',
    });
    final status = _enum(m['status'], {'committed', 'not_found'});
    final receipt = m['receipt'] == null
        ? null
        : MemoryOperationReceipt.fromJson(m['receipt']);
    final disposition = m['disposition'] == null
        ? null
        : _enum(m['disposition'], {
            'stored_active',
            'stored_invalidated',
            'stored_tainted',
            'forgotten',
            'unavailable',
          });
    if (status == 'committed'
        ? receipt == null || disposition == null
        : receipt != null || disposition != null) {
      _invalid();
    }
    return MemoryOperationLookup._(
      MemoryBinding.fromJson(m['binding']),
      _enum(m['operation'], {'capture', 'forget'}),
      memoryOperationKey(m['idempotency_key']),
      status,
      receipt,
      disposition,
      _boolean(m['mutation_fenced']),
    );
  }
  final MemoryBinding binding;
  final String operation, idempotencyKey, status;
  final MemoryOperationReceipt? receipt;

  /// Proves no new effect under the original request, not its prior outcome.
  final bool mutationFenced;

  /// Stored metadata only; not a current recall-eligibility claim.
  final String? disposition;
}

class MemoryNode {
  MemoryNode._(
    this.binding,
    this.nodeId,
    this.ordinal,
    this.kind,
    this.sourceEventId,
    this.sourceRevision,
    this.sourceSequence,
    this.prerequisiteIds,
    this.body,
    this.fact,
    this.state,
    this.contentScope,
    this.provenance,
  );
  factory MemoryNode.fromJson(Object? value) {
    final m = _object(value, {
      ..._bindingKeys,
      'node_id',
      'ordinal',
      'kind',
      'source_event_id',
      'source_revision',
      'source_sequence',
      'prerequisite_ids',
      'body',
      'fact',
      'state',
      'content_scope',
      'provenance',
    });
    final state = _enum(m['state'], {
      'active',
      'invalidated',
      'tainted',
      'forgotten',
    });
    final kind = _enum(m['kind'], {'assertion', 'observation', 'inference'});
    final body = m['body'] == null
        ? null
        : _text(m['body'], max: 2048, nonblank: true);
    final fact = m['fact'] == null ? null : MemoryFact.fromJson(m['fact']);
    final parents = _identifiers(m['prerequisite_ids']);
    if (state == 'forgotten') {
      if (body != null || fact != null) _invalid();
    } else if (body == null ||
        (kind == 'inference'
            ? fact != null || parents.isEmpty
            : fact == null)) {
      _invalid();
    }
    return MemoryNode._(
      MemoryBinding._from(m),
      memoryIdentifier(m['node_id']),
      _integer(m['ordinal'], min: 1),
      kind,
      memoryIdentifier(m['source_event_id']),
      _integer(m['source_revision'], min: 1),
      _integer(m['source_sequence'], min: 1),
      parents,
      body,
      fact,
      state,
      _enum(m['content_scope'], {'local_only'}),
      _enum(m['provenance'], {'owner_curated'}),
    );
  }
  final MemoryBinding binding;
  final String nodeId;
  final int ordinal;
  final String kind;
  final String sourceEventId;
  final int sourceRevision;
  final int sourceSequence;
  final List<String> prerequisiteIds;
  final String? body;
  final MemoryFact? fact;
  final String state;
  final String contentScope;
  final String provenance;
}

class MemoryInspection {
  MemoryInspection._(this.state, this.node);
  factory MemoryInspection.fromJson(Object? value) {
    final m = _object(value, {'schema_version', 'state', 'node'});
    final state = MemoryNamespaceState.fromJson(m['state']);
    final node = MemoryNode.fromJson(m['node']);
    if (state.binding != node.binding) _invalid();
    return MemoryInspection._(state, node);
  }
  final MemoryNamespaceState state;
  final MemoryNode node;
  MemoryBinding get binding => state.binding;
}
