/// Codex-authored standing dispatch metadata; historical proof grants no launch.
library;

import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'agent_control.dart';

const _maxInteger = 9007199254740991;
const _maxEpoch = 10000;
const _producerTtl = 15000;
const _uuid = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}';
const _invalid = FormatException('Invalid standing dispatch proof');

void _unicode(String value) {
  for (var i = 0; i < value.length; i++) {
    final unit = value.codeUnitAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      if (++i >= value.length ||
          value.codeUnitAt(i) < 0xdc00 ||
          value.codeUnitAt(i) > 0xdfff) {
        throw _invalid;
      }
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      throw _invalid;
    }
  }
}

void _tree(Object? value, [int depth = 1, List<int>? count]) {
  count ??= [0];
  if (depth > 12 || ++count[0] > 2048) throw _invalid;
  if (value is Map) {
    for (final entry in value.entries) {
      if (entry.key is! String) throw _invalid;
      _tree(entry.key, depth + 1, count);
      _tree(entry.value, depth + 1, count);
    }
  } else if (value is List) {
    for (final item in value) {
      _tree(item, depth + 1, count);
    }
  } else if (value is String) {
    _unicode(value);
  } else if (value is int) {
    if (value < -_maxInteger || value > _maxInteger) throw _invalid;
  } else if (value != null && value is! bool) {
    throw _invalid;
  }
}

Map<String, Object?> _closed(Object? value, Set<String> fields) {
  // Inspect the actual full graph before any older mode constructor can drop
  // or rebuild nested data. Every nested packet also checks its exact keys.
  _tree(value);
  if (value is! Map || value.length != fields.length) throw _invalid;
  final result = <String, Object?>{};
  for (final entry in value.entries) {
    final key = entry.key;
    if (key is! String || !fields.contains(key)) throw _invalid;
    result[key] = entry.value;
  }
  return result;
}

int _compare(String left, String right) {
  final a = left.runes.iterator, b = right.runes.iterator;
  while (true) {
    final moreA = a.moveNext(), moreB = b.moveNext();
    if (!moreA || !moreB) {
      return moreA == moreB
          ? 0
          : moreA
          ? 1
          : -1;
    }
    if (a.current != b.current) return a.current.compareTo(b.current);
  }
}

Object? _freeze(Object? value) {
  if (value is Map) {
    final keys = value.keys.cast<String>().toList()..sort(_compare);
    return Map<String, Object?>.unmodifiable({
      for (final key in keys) key: _freeze(value[key]),
    });
  }
  if (value is List) return List<Object?>.unmodifiable(value.map(_freeze));
  return value;
}

List<int> _bytes(Object? value) => utf8.encode(jsonEncode(_freeze(value)));

Map<String, Object?> _bounded(Map<String, Object?> raw, int maximum) {
  _tree(raw);
  if (_bytes(raw).length > maximum) throw _invalid;
  return _freeze(raw) as Map<String, Object?>;
}

bool _same(Object? left, Object? right) =>
    jsonEncode(_freeze(left)) == jsonEncode(_freeze(right));

String _match(Object? value, String pattern) {
  if (value is! String ||
      RegExp(pattern).matchAsPrefix(value)?.end != value.length) {
    throw _invalid;
  }
  return value;
}

String _backend(Object? value) => _match(value, r'[a-f0-9]{32}');
String _instance(Object? value) => _match(value, _uuid);
String _digest(Object? value) => _match(value, r'[a-f0-9]{64}');

int _integer(Object? value, int minimum, [int maximum = _maxInteger]) {
  if (value is! int || value < minimum || value > maximum) throw _invalid;
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) throw _invalid;
  return value;
}

AgentMode _mode(Object? value) {
  if (value is String) {
    for (final mode in AgentMode.values) {
      if (value == mode.wire) return mode;
    }
  }
  throw _invalid;
}

void _version(Map<String, Object?> raw) {
  if (raw['schema_version'] != '1.0') throw _invalid;
}

AgentModeChange _modeChange(Object? value) {
  final raw = _closed(value, {
    'schema_version',
    'idempotency_key',
    'expected_backend',
    'expected_instance',
    'expected_revision',
    'mode',
    'active_work_policy',
  });
  try {
    return AgentModeChange.fromJson(raw);
  } on FormatException {
    throw _invalid;
  }
}

AgentModeReceipt _modeReceipt(Object? value, AgentModeChange original) {
  final raw = _closed(value, {
    'schema_version',
    'status',
    'backend_id',
    'service_instance',
    'revision',
    'request',
  });
  _modeChange(raw['request']);
  try {
    return AgentModeReceipt.fromJson(raw, original);
  } on FormatException {
    throw _invalid;
  }
}

String _activationDigest(StandingDispatchActivation request, int epoch) =>
    sha256.convert([
      ...utf8.encode('kinaiya.standing.dispatch.activation.v1\u0000'),
      ..._bytes({'epoch': epoch, 'request': request.body}),
    ]).toString();

/// A renewal identity. Only the native controller samples lease time.
final class StandingProducerRenewal {
  StandingProducerRenewal.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'expected_backend',
      'expected_instance',
      'producer_id',
      'sequence',
    });
    _version(raw);
    backendId = _backend(raw['expected_backend']);
    instanceId = _instance(raw['expected_instance']);
    producerId = _instance(raw['producer_id']);
    sequence = _integer(raw['sequence'], 1);
    body = _bounded(raw, 4096);
  }

  late final String backendId, instanceId, producerId;
  late final int sequence;
  late final Map<String, Object?> body;

  bool matches(StandingProducerRenewal other) => _same(body, other.body);
}

/// Historical lease acknowledgment, without a browser-clock liveness claim.
final class StandingProducerReceipt {
  StandingProducerReceipt.fromJson(
    Object? value, {
    StandingProducerRenewal? original,
  }) {
    final raw = _closed(value, {
      'schema_version',
      'request',
      'lease_token',
      'issued_monotonic_ms',
      'expires_monotonic_ms',
    });
    _version(raw);
    request = StandingProducerRenewal.fromJson(raw['request']);
    if (original != null && !request.matches(original)) throw _invalid;
    leaseToken = _instance(raw['lease_token']);
    issuedMonotonicMs = _integer(
      raw['issued_monotonic_ms'],
      0,
      _maxInteger - _producerTtl,
    );
    expiresMonotonicMs = _integer(raw['expires_monotonic_ms'], _producerTtl);
    if (expiresMonotonicMs - issuedMonotonicMs != _producerTtl) throw _invalid;
    json = _bounded(raw, 8192);
  }

  late final StandingProducerRenewal request;
  late final String leaseToken;
  late final int issuedMonotonicMs, expiresMonotonicMs;
  late final Map<String, Object?> json;
}

/// Native gate observation; readiness does not authorize an individual task.
final class StandingDispatchState {
  StandingDispatchState.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'backend_id',
      'instance_id',
      'control_revision',
      'mode',
      'control_writable',
      'epoch',
      'stopped_through_epoch',
      'fence_token',
      'activation_digest',
      'activation_control_revision',
      'producer',
      'dispatch_ready',
    });
    _version(raw);
    backendId = _backend(raw['backend_id']);
    instanceId = raw['instance_id'] == null
        ? null
        : _instance(raw['instance_id']);
    controlRevision = _integer(raw['control_revision'], 0);
    mode = _mode(raw['mode']);
    controlWritable = _flag(raw['control_writable']);
    epoch = _integer(raw['epoch'], 0, _maxEpoch);
    stoppedThroughEpoch = _integer(raw['stopped_through_epoch'], 0, _maxEpoch);
    fenceToken = _instance(raw['fence_token']);
    activationDigest = raw['activation_digest'] == null
        ? null
        : _digest(raw['activation_digest']);
    activationControlRevision = raw['activation_control_revision'] == null
        ? null
        : _integer(raw['activation_control_revision'], 1);
    producer = raw['producer'] == null
        ? null
        : StandingProducerReceipt.fromJson(raw['producer']);
    dispatchReady = _flag(raw['dispatch_ready']);
    if ((instanceId == null && (controlWritable || producer != null)) ||
        stoppedThroughEpoch > epoch ||
        (epoch == 0 &&
            (activationDigest != null || activationControlRevision != null)) ||
        (epoch > 0 &&
            (activationDigest == null ||
                activationControlRevision == null ||
                activationControlRevision! > controlRevision)) ||
        (producer != null &&
            (producer!.request.backendId != backendId ||
                producer!.request.instanceId != instanceId))) {
      throw _invalid;
    }
    final ready =
        controlWritable &&
        mode == AgentMode.autonomous &&
        epoch > stoppedThroughEpoch &&
        activationControlRevision == controlRevision &&
        producer != null;
    if (dispatchReady != ready) throw _invalid;
    json = _bounded(raw, 16384);
  }

  late final String backendId, fenceToken;
  late final String? instanceId, activationDigest;
  late final int controlRevision, epoch, stoppedThroughEpoch;
  late final int? activationControlRevision;
  late final AgentMode mode;
  late final bool controlWritable, dispatchReady;
  late final StandingProducerReceipt? producer;
  late final Map<String, Object?> json;
}

/// Complete owner activation request, reused unchanged for uncertain recovery.
final class StandingDispatchActivation {
  StandingDispatchActivation.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'mode_change',
      'expected_epoch',
      'expected_fence_token',
    });
    _version(raw);
    modeChange = _modeChange(raw['mode_change']);
    if (modeChange.mode != AgentMode.autonomous) throw _invalid;
    expectedEpoch = _integer(raw['expected_epoch'], 0, _maxEpoch - 1);
    expectedFenceToken = _instance(raw['expected_fence_token']);
    body = _bounded(raw, 4096);
  }

  late final AgentModeChange modeChange;
  late final int expectedEpoch;
  late final String expectedFenceToken;
  late final Map<String, Object?> body;

  bool matches(StandingDispatchActivation other) => _same(body, other.body);
}

/// Full historical activation proof; replay never rearms a stopped epoch.
final class StandingDispatchActivationReceipt {
  StandingDispatchActivationReceipt.fromJson(
    Object? value, {
    StandingDispatchActivation? original,
  }) {
    final raw = _closed(value, {
      'schema_version',
      'request',
      'mode_receipt',
      'epoch',
      'activation_digest',
    });
    _version(raw);
    request = StandingDispatchActivation.fromJson(raw['request']);
    if (original != null && !request.matches(original)) throw _invalid;
    modeReceipt = _modeReceipt(raw['mode_receipt'], request.modeChange);
    epoch = _integer(raw['epoch'], 1, _maxEpoch);
    activationDigest = _digest(raw['activation_digest']);
    if (epoch != request.expectedEpoch + 1 ||
        activationDigest != _activationDigest(request, epoch)) {
      throw _invalid;
    }
    json = _bounded(raw, 16384);
  }

  late final StandingDispatchActivation request;
  late final AgentModeReceipt modeReceipt;
  late final int epoch;
  late final String activationDigest;
  late final Map<String, Object?> json;
}

/// Serialized original-request observation; no launch or rearm semantics.
final class StandingDispatchActivationLookup {
  StandingDispatchActivationLookup.fromJson(
    Object? value, {
    StandingDispatchActivation? original,
  }) {
    final raw = _closed(value, {
      'schema_version',
      'status',
      'request',
      'state',
      'receipt',
      'mutation_fenced',
    });
    _version(raw);
    request = StandingDispatchActivation.fromJson(raw['request']);
    if (original != null && !request.matches(original)) throw _invalid;
    state = StandingDispatchState.fromJson(raw['state']);
    mutationFenced = _flag(raw['mutation_fenced']);
    final mode = request.modeChange;
    if (state.backendId != mode.backendId ||
        state.controlRevision < mode.revision ||
        state.epoch < request.expectedEpoch) {
      throw _invalid;
    }
    if (raw['status'] == 'recorded') {
      status = 'recorded';
      receipt = StandingDispatchActivationReceipt.fromJson(
        raw['receipt'],
        original: request,
      );
      if (state.epoch < receipt!.epoch ||
          state.controlRevision < receipt!.modeReceipt.revision ||
          (state.epoch == receipt!.epoch &&
              (state.activationDigest != receipt!.activationDigest ||
                  state.activationControlRevision !=
                      receipt!.modeReceipt.revision))) {
        throw _invalid;
      }
    } else {
      if (raw['status'] != 'unobserved' || raw['receipt'] != null) {
        throw _invalid;
      }
      status = 'unobserved';
      receipt = null;
    }
    final fenced =
        receipt != null ||
        state.instanceId != mode.instanceId ||
        state.controlRevision > mode.revision ||
        state.epoch > request.expectedEpoch ||
        state.fenceToken != request.expectedFenceToken;
    if (mutationFenced != fenced) throw _invalid;
    json = _bounded(raw, 32768);
  }

  late final StandingDispatchActivation request;
  late final String status;
  late final StandingDispatchState state;
  late final StandingDispatchActivationReceipt? receipt;
  late final bool mutationFenced;
  late final Map<String, Object?> json;
}

/// Read-only target template. It neither stops nor assigns a controller.
final class StandingDispatchStop {
  StandingDispatchStop.fromJson(Object? value) {
    final raw = _closed(value, {
      'schema_version',
      'expected_backend',
      'expected_instance',
      'issued_epoch',
      'activation_digest',
      'expected_fence_token',
    });
    _version(raw);
    backendId = _backend(raw['expected_backend']);
    instanceId = _instance(raw['expected_instance']);
    issuedEpoch = _integer(raw['issued_epoch'], 0, _maxEpoch);
    activationDigest = raw['activation_digest'] == null
        ? null
        : _digest(raw['activation_digest']);
    expectedFenceToken = _instance(raw['expected_fence_token']);
    if ((issuedEpoch == 0) != (activationDigest == null)) throw _invalid;
    body = _bounded(raw, 4096);
  }

  late final String backendId, instanceId, expectedFenceToken;
  late final int issuedEpoch;
  late final String? activationDigest;
  late final Map<String, Object?> body;

  bool matches(StandingDispatchStop other) => _same(body, other.body);
}

/// Only the requested target is fenced; admitted effects may remain unsettled.
final class StandingDispatchStopProof {
  StandingDispatchStopProof.fromJson(
    Object? value, {
    StandingDispatchStop? original,
  }) {
    final raw = _closed(value, {
      'schema_version',
      'request',
      'state',
      'target_fenced',
    });
    _version(raw);
    request = StandingDispatchStop.fromJson(raw['request']);
    if (original != null && !request.matches(original)) throw _invalid;
    state = StandingDispatchState.fromJson(raw['state']);
    targetFenced = _flag(raw['target_fenced']);
    if (!targetFenced ||
        state.backendId != request.backendId ||
        state.epoch < request.issuedEpoch ||
        state.stoppedThroughEpoch < request.issuedEpoch ||
        (state.epoch == request.issuedEpoch &&
            state.activationDigest != request.activationDigest)) {
      throw _invalid;
    }
    json = _bounded(raw, 32768);
  }

  late final StandingDispatchStop request;
  late final StandingDispatchState state;
  late final bool targetFenced;
  late final Map<String, Object?> json;
}
