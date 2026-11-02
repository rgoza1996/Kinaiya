// Independent frozen wire oracles. These packets do not qualify a producer or task.
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/agent_control.dart';
import 'package:kinaiya_flutter/api/standing_dispatch.dart';

typedef Json = Map<String, Object?>;

late Json _vectors;
const _marker = 'SYNTHETIC-PRIVATE-MARKER';
const _otherId = '00000000-0000-0000-0000-000000000099';
const _max = 9007199254740991;
String _repeat(String value, int count) => List.filled(count, value).join();

Json _map(Object? value) => (value as Map).cast<String, Object?>();
Json _copy(Object? value) => _map(jsonDecode(jsonEncode(value)));
Json _fixture(String name) => _copy(_vectors[name]);
Json _at(Json value, List<String> path) {
  for (final key in path) {
    value = _map(value[key]);
  }
  return value;
}

Object? _ordered(Object? value) {
  if (value is Map) {
    final raw = _map(value);
    final keys = raw.keys.toList()
      ..sort(); // All admitted dispatch keys are ASCII.
    return {for (final key in keys) key: _ordered(raw[key])};
  }
  if (value is List) return value.map(_ordered).toList();
  return value;
}

List<int> _canonical(Object? value) => utf8.encode(jsonEncode(_ordered(value)));

final _readers = <String, Object Function(Object?)>{
  'renewal': StandingProducerRenewal.fromJson,
  'producer_receipt': StandingProducerReceipt.fromJson,
  'state': StandingDispatchState.fromJson,
  'activation': StandingDispatchActivation.fromJson,
  'activation_receipt': StandingDispatchActivationReceipt.fromJson,
  'lookup': StandingDispatchActivationLookup.fromJson,
  'stop': StandingDispatchStop.fromJson,
  'stop_proof': StandingDispatchStopProof.fromJson,
};

Json _view(Object value) => switch (value) {
  StandingProducerRenewal p => p.body,
  StandingProducerReceipt p => p.json,
  StandingDispatchState p => p.json,
  StandingDispatchActivation p => p.body,
  StandingDispatchActivationReceipt p => p.json,
  StandingDispatchActivationLookup p => p.json,
  StandingDispatchStop p => p.body,
  StandingDispatchStopProof p => p.json,
  _ => throw StateError('Unexpected test packet'),
};

void _refuses(Object Function(Object?) read, Object? input) {
  expect(
    () => read(input),
    throwsA(
      isA<FormatException>().having(
        (e) => e.toString(),
        'no untrusted input echo',
        isNot(contains(_marker)),
      ),
    ),
  );
}

void _immutable(Object? value) {
  if (value is Map) {
    expect(
      () => value['synthetic_extra'] = true,
      throwsA(isA<UnsupportedError>()),
    );
    for (final nested in value.values) {
      _immutable(nested);
    }
  } else if (value is List) {
    expect(() => value.add(null), throwsA(isA<UnsupportedError>()));
    for (final nested in value) {
      _immutable(nested);
    }
  }
}

void main() {
  setUpAll(() {
    _vectors = _map(
      jsonDecode(
        File(
          'test/fixtures/standing-dispatch-independent-v1.json',
        ).readAsStringSync(),
      ),
    );
  });

  test('literal stdlib golden binds all eight public packet surfaces', () {
    final original = StandingDispatchActivation.fromJson(
      _fixture('activation'),
    );
    final renewal = StandingProducerRenewal.fromJson(_fixture('renewal'));
    final lease = StandingProducerReceipt.fromJson(
      _fixture('producer_receipt'),
      original: renewal,
    );
    final receipt = StandingDispatchActivationReceipt.fromJson(
      _fixture('activation_receipt'),
      original: original,
    );
    final lookup = StandingDispatchActivationLookup.fromJson(
      _fixture('lookup'),
      original: original,
    );
    final state = StandingDispatchState.fromJson(_fixture('state'));
    final stop = StandingDispatchStop.fromJson(_fixture('stop'));
    final stopped = StandingDispatchStopProof.fromJson(
      _fixture('stop_proof'),
      original: stop,
    );
    expect(
      utf8.decode(_canonical(original.body)),
      _map(_vectors['golden'])['activation_canonical'],
    );
    expect(
      sha256.convert(_canonical(original.body)).toString(),
      '51b32d7293ee73eb8858838be2e01c2f919934865a804c47feafa8006a1a75e5',
    );
    expect(
      receipt.activationDigest,
      '68721828ad43f5409d5def2caa9b5460bcec01313227ef6dd2b409823a756917',
    );
    expect(
      sha256
          .convert(
            utf8.encode(
              _map(_vectors['golden'])['activation_hash_input'] as String,
            ),
          )
          .toString(),
      receipt.activationDigest,
    );
    expect(original.modeChange.mode, AgentMode.autonomous);
    expect(original.expectedEpoch, 0);
    expect(
      original.expectedFenceToken,
      _fixture('activation')['expected_fence_token'],
    );
    expect(renewal.sequence, 7);
    expect(renewal.backendId, state.backendId);
    expect(renewal.instanceId, state.instanceId);
    expect(renewal.producerId, '00000000-0000-0000-0000-000000000101');
    expect(lease.request.matches(renewal), isTrue);
    expect(lease.leaseToken, '00000000-0000-0000-0000-000000000201');
    expect(lease.issuedMonotonicMs, 1000);
    expect(lease.expiresMonotonicMs, 16000);
    expect(receipt.request.matches(original), isTrue);
    expect(receipt.modeReceipt.request.matches(original.modeChange), isTrue);
    expect(receipt.epoch, 1);
    expect(lookup.status, 'recorded');
    expect(lookup.receipt!.activationDigest, receipt.activationDigest);
    expect(lookup.mutationFenced, isTrue);
    expect(state.mode, AgentMode.autonomous);
    expect(
      state.dispatchReady,
      isTrue,
    ); // Native observation, not browser-clock expiry.
    expect(state.producer!.request.matches(renewal), isTrue);
    expect(stopped.request.matches(stop), isTrue);
    expect(stopped.targetFenced, isTrue);
    expect(stopped.state.dispatchReady, isFalse);
    expect(
      stopped.state.mode,
      AgentMode.autonomous,
    ); // Saved intent is not a running task.
  });

  for (final entry in _readers.entries) {
    test(
      '${entry.key} requires every explicit field and rejects extras/version changes',
      () {
        final good = _fixture(entry.key);
        for (final key in good.keys) {
          final missing = _copy(good)..remove(key);
          _refuses(entry.value, missing);
        }
        _refuses(entry.value, {...good, 'extra': _marker});
        _refuses(entry.value, {...good, 'schema_version': '1.1'});
        _refuses(entry.value, {...good, 'schema_version': null});
      },
    );
  }

  for (final item in <(String, List<String>)>[
    ('activation', ['mode_change']),
    ('activation_receipt', ['request', 'mode_change']),
    ('activation_receipt', ['mode_receipt']),
    ('activation_receipt', ['mode_receipt', 'request']),
    ('state', ['producer', 'request']),
    ('lookup', ['receipt', 'mode_receipt', 'request']),
    ('stop_proof', ['state', 'producer', 'request']),
  ]) {
    test(
      'whole graph rejects nested unknown ${item.$1}/${item.$2.join('/')}',
      () {
        final raw = _fixture(item.$1);
        _at(raw, item.$2)['extra'] = _marker;
        _refuses(_readers[item.$1]!, raw);
      },
    );
  }

  test('all retained graphs are detached recursively immutable maps', () {
    for (final entry in _readers.entries) {
      final input = _fixture(entry.key);
      final parsed = entry.value(input);
      final before = _canonical(_view(parsed));
      input['schema_version'] = 'changed after construction';
      if (input['request'] is Map) {
        _map(input['request'])['schema_version'] = 'changed';
      }
      if (input['mode_change'] is Map) {
        _map(input['mode_change'])['idempotency_key'] = 'changed';
      }
      expect(_canonical(_view(parsed)), before, reason: entry.key);
      _immutable(_view(parsed));
    }
  });

  test(
    'request matches and original proof binding include every variable identity',
    () {
      final original = StandingDispatchActivation.fromJson(
        _fixture('activation'),
      );
      for (final change in <(List<String>, String, Object)>[
        (['mode_change'], 'idempotency_key', 'different-key'),
        (['mode_change'], 'expected_backend', _repeat('b', 32)),
        (['mode_change'], 'expected_instance', _otherId),
        (['mode_change'], 'expected_revision', 1),
        ([], 'expected_epoch', 1),
        ([], 'expected_fence_token', _otherId),
      ]) {
        final raw = _fixture('activation');
        _at(raw, change.$1)[change.$2] = change.$3;
        final different = StandingDispatchActivation.fromJson(raw);
        expect(original.matches(different), isFalse);
        expect(different.matches(original), isFalse);
        expect(
          () => StandingDispatchActivationReceipt.fromJson(
            _fixture('activation_receipt'),
            original: different,
          ),
          throwsFormatException,
        );
        expect(
          () => StandingDispatchActivationLookup.fromJson(
            _fixture('lookup'),
            original: different,
          ),
          throwsFormatException,
        );
      }
      expect(
        original.matches(
          StandingDispatchActivation.fromJson(_fixture('activation')),
        ),
        isTrue,
      );
      final renewal = StandingProducerRenewal.fromJson(_fixture('renewal'));
      for (final field in [
        'expected_backend',
        'expected_instance',
        'producer_id',
        'sequence',
      ]) {
        final raw = _fixture('renewal');
        raw[field] = field == 'sequence'
            ? 8
            : field == 'expected_backend'
            ? _repeat('b', 32)
            : _otherId;
        final different = StandingProducerRenewal.fromJson(raw);
        expect(renewal.matches(different), isFalse);
        expect(
          () => StandingProducerReceipt.fromJson(
            _fixture('producer_receipt'),
            original: different,
          ),
          throwsFormatException,
        );
      }
      final stop = StandingDispatchStop.fromJson(_fixture('stop'));
      for (final field in [
        'expected_backend',
        'expected_instance',
        'activation_digest',
        'expected_fence_token',
      ]) {
        final raw = _fixture('stop');
        raw[field] = field == 'expected_backend'
            ? _repeat('b', 32)
            : field == 'activation_digest'
            ? _repeat('d', 64)
            : _otherId;
        final different = StandingDispatchStop.fromJson(raw);
        expect(stop.matches(different), isFalse);
        expect(
          () => StandingDispatchStopProof.fromJson(
            _fixture('stop_proof'),
            original: different,
          ),
          throwsFormatException,
        );
      }
      expect(
        stop.matches(StandingDispatchStop.fromJson(_fixture('zero_stop'))),
        isFalse,
      );
      expect(
        () => StandingDispatchStopProof.fromJson(
          _fixture('stop_proof'),
          original: StandingDispatchStop.fromJson(_fixture('zero_stop')),
        ),
        throwsFormatException,
      );
    },
  );

  test(
    'strict scalars refuse booleans, floats, unsafe numbers and identity normalization',
    () {
      for (final value in <Object?>[
        true,
        false,
        0,
        1.0,
        -1,
        _max + 1,
        '7',
        null,
      ]) {
        _refuses(StandingProducerRenewal.fromJson, {
          ..._fixture('renewal'),
          'sequence': value,
        });
      }
      for (final value in <Object?>[0, 1, 'true', null]) {
        _refuses(StandingDispatchState.fromJson, {
          ..._fixture('state'),
          'control_writable': value,
        });
        _refuses(StandingDispatchState.fromJson, {
          ..._fixture('state'),
          'dispatch_ready': value,
        });
        _refuses(StandingDispatchStopProof.fromJson, {
          ..._fixture('stop_proof'),
          'target_fenced': value,
        });
      }
      for (final value in [
        _repeat('A', 32),
        '${_repeat('a', 32)}\n',
        ' ${_repeat('a', 32)}',
        _repeat('é', 32),
        String.fromCharCode(0xd800),
      ]) {
        _refuses(StandingProducerRenewal.fromJson, {
          ..._fixture('renewal'),
          'expected_backend': value,
        });
      }
      for (final value in [
        _otherId.toUpperCase().replaceFirst('0000', 'ABCD'),
        '$_otherId\n',
        ' $_otherId',
        String.fromCharCode(0xdc00),
      ]) {
        _refuses(StandingProducerRenewal.fromJson, {
          ..._fixture('renewal'),
          'producer_id': value,
        });
      }
      final maximum = StandingProducerRenewal.fromJson({
        ..._fixture('renewal'),
        'sequence': _max,
      });
      expect(maximum.sequence, _max);
    },
  );

  test(
    'arbitrary, deep, cyclic and non-string-key graphs refuse with fixed private errors',
    () {
      final cycle = <String, Object?>{};
      cycle['cycle'] = cycle;
      Object? depth;
      for (var n = 0; n < 14; n++) {
        depth = {'x': depth};
      }
      for (final input in <Object?>[
        null,
        true,
        1,
        double.nan,
        double.infinity,
        [],
        <Object?, Object?>{1: _marker},
        cycle,
        depth,
        {'many': List.filled(2049, 0)},
      ]) {
        _refuses(StandingDispatchActivation.fromJson, input);
      }
      final messages = <String>{};
      for (final marker in [_marker, 'OTHER-PRIVATE-MARKER']) {
        try {
          StandingDispatchActivation.fromJson({
            ..._fixture('activation'),
            marker: marker,
          });
          fail('Invalid graph was accepted');
        } on FormatException catch (error) {
          messages.add(error.toString());
          expect(error.toString(), isNot(contains(marker)));
        }
      }
      expect(messages.length, 1);
    },
  );

  test(
    'producer proof has an exact safe 15000ms interval without local liveness inference',
    () {
      final maximum = {
        ..._fixture('producer_receipt'),
        'issued_monotonic_ms': _max - 15000,
        'expires_monotonic_ms': _max,
      };
      expect(
        StandingProducerReceipt.fromJson(maximum).expiresMonotonicMs,
        _max,
      );
      for (final mutation in <Json>[
        {'issued_monotonic_ms': -1},
        {'issued_monotonic_ms': true},
        {'expires_monotonic_ms': 15999},
        {'expires_monotonic_ms': 16001},
        {'issued_monotonic_ms': _max - 14999, 'expires_monotonic_ms': _max + 1},
        {'lease_token': _repeat('A', 36)},
      ]) {
        _refuses(StandingProducerReceipt.fromJson, {
          ..._fixture('producer_receipt'),
          ...mutation,
        });
      }
      expect(
        StandingDispatchState.fromJson(_fixture('state')).dispatchReady,
        isTrue,
      );
    },
  );

  test(
    'null-controller emergency history is valid only without writer or producer claims',
    () {
      final state = StandingDispatchState.fromJson(
        _fixture('null_controller_state'),
      );
      expect(state.instanceId, isNull);
      expect(state.controlWritable, isFalse);
      expect(state.producer, isNull);
      expect(state.mode, AgentMode.autonomous);
      expect(state.epoch, 1);
      expect(state.dispatchReady, isFalse);
      for (final mutation in <Json>[
        {'control_writable': true},
        {'producer': _fixture('producer_receipt')},
        {'dispatch_ready': true},
      ]) {
        _refuses(StandingDispatchState.fromJson, {
          ..._fixture('null_controller_state'),
          ...mutation,
        });
      }
    },
  );

  test('readiness equals the complete native conjunction, not mode alone', () {
    for (final mutation in <Json>[
      {'control_writable': false},
      {'mode': 'off'},
      {'mode': 'on_demand'},
      {'stopped_through_epoch': 1},
      {'control_revision': 2},
      {'producer': null},
    ]) {
      final raw = {..._fixture('state'), ...mutation, 'dispatch_ready': false};
      expect(StandingDispatchState.fromJson(raw).dispatchReady, isFalse);
      _refuses(StandingDispatchState.fromJson, {
        ...raw,
        'dispatch_ready': true,
      });
    }
    _refuses(StandingDispatchState.fromJson, {
      ..._fixture('state'),
      'dispatch_ready': false,
    });
    expect(
      StandingDispatchState.fromJson(_fixture('zero_state')).activationDigest,
      isNull,
    );
  });

  test(
    'state epoch, history, activation revision and producer authority stay closed',
    () {
      for (final mutation in <Json>[
        {'stopped_through_epoch': 2},
        {'epoch': -1},
        {'epoch': 10001},
        {'epoch': true},
        {'activation_digest': null},
        {'activation_digest': _repeat('D', 64)},
        {'activation_control_revision': null},
        {'activation_control_revision': 0},
        {'activation_control_revision': 2},
        {'control_revision': _max + 1},
      ]) {
        _refuses(StandingDispatchState.fromJson, {
          ..._fixture('state'),
          ...mutation,
        });
      }
      for (final field in [
        'activation_digest',
        'activation_control_revision',
      ]) {
        _refuses(StandingDispatchState.fromJson, {
          ..._fixture('zero_state'),
          field: field == 'activation_digest' ? _repeat('d', 64) : 1,
        });
      }
      for (final field in ['expected_backend', 'expected_instance']) {
        final raw = _fixture('state');
        _at(raw, ['producer', 'request'])[field] = field == 'expected_backend'
            ? _repeat('b', 32)
            : _otherId;
        _refuses(StandingDispatchState.fromJson, raw);
      }
    },
  );

  test(
    'activation accepts the final epoch but only complete autonomous mode requests',
    () {
      expect(
        StandingDispatchActivation.fromJson(
          _fixture('terminal_activation'),
        ).expectedEpoch,
        9999,
      );
      expect(
        StandingDispatchActivationReceipt.fromJson(
          _fixture('terminal_receipt'),
        ).epoch,
        10000,
      );
      for (final epoch in <Object?>[-1, 10000, true, 0.0, null]) {
        _refuses(StandingDispatchActivation.fromJson, {
          ..._fixture('activation'),
          'expected_epoch': epoch,
        });
      }
      for (final mode in ['off', 'on_demand', 'Autonomous']) {
        final raw = _fixture('activation');
        _map(raw['mode_change'])['mode'] = mode;
        _refuses(StandingDispatchActivation.fromJson, raw);
      }
    },
  );

  test(
    'activation receipt recomputes digest and binds ordinary mode ACK plus next epoch',
    () {
      for (final field in ['epoch', 'activation_digest']) {
        final raw = _fixture('activation_receipt');
        raw[field] = field == 'epoch' ? 2 : _repeat('d', 64);
        _refuses(StandingDispatchActivationReceipt.fromJson, raw);
      }
      for (final field in [
        'backend_id',
        'service_instance',
        'revision',
        'status',
      ]) {
        final raw = _fixture('activation_receipt');
        _map(raw['mode_receipt'])[field] = switch (field) {
          'backend_id' => _repeat('b', 32),
          'service_instance' => _otherId,
          'revision' => 2,
          _ => 'not_saved',
        };
        _refuses(StandingDispatchActivationReceipt.fromJson, raw);
      }
      final different = _fixture('activation_receipt');
      _at(different, ['mode_receipt', 'request'])['idempotency_key'] =
          'other-key';
      _refuses(StandingDispatchActivationReceipt.fromJson, different);
      final validOther = StandingDispatchActivationReceipt.fromJson(
        _fixture('other_receipt'),
      );
      expect(
        validOther.request.matches(
          StandingDispatchActivation.fromJson(_fixture('activation')),
        ),
        isFalse,
      );
    },
  );

  test(
    'recorded lookup requires full receipt and current-epoch digest/revision binding',
    () {
      for (final mutation in <Json>[
        {'receipt': null},
        {'status': 'unobserved'},
        {'mutation_fenced': false},
      ]) {
        _refuses(StandingDispatchActivationLookup.fromJson, {
          ..._fixture('lookup'),
          ...mutation,
        });
      }
      for (final mutation in <Json>[
        {'activation_digest': _repeat('d', 64)},
        {
          'activation_control_revision': 2,
          'control_revision': 2,
          'dispatch_ready': true,
        },
        {
          'backend_id': _repeat('b', 32),
          'producer': null,
          'dispatch_ready': false,
        },
      ]) {
        final raw = _fixture('lookup');
        raw['state'] = {..._map(raw['state']), ...mutation};
        _refuses(StandingDispatchActivationLookup.fromJson, raw);
      }
      final historical = _fixture('lookup');
      historical['state'] = {
        ..._fixture('stopped_state'),
        'epoch': 2,
        'control_revision': 2,
        'activation_control_revision': 2,
        'activation_digest': _repeat('d', 64),
        'producer': null,
      };
      expect(
        StandingDispatchActivationLookup.fromJson(historical).receipt!.epoch,
        1,
      );
    },
  );

  test(
    'unobserved lookup fences exactly irreversible identity/revision/epoch/fence changes',
    () {
      final unchanged = StandingDispatchActivationLookup.fromJson(
        _fixture('unobserved_lookup'),
      );
      expect(unchanged.receipt, isNull);
      expect(unchanged.mutationFenced, isFalse);
      _refuses(StandingDispatchActivationLookup.fromJson, {
        ..._fixture('unobserved_lookup'),
        'mutation_fenced': true,
      });
      _refuses(StandingDispatchActivationLookup.fromJson, {
        ..._fixture('unobserved_lookup'),
        'receipt': _fixture('activation_receipt'),
      });
      for (final mutation in <Json>[
        {'instance_id': _otherId},
        {'instance_id': null, 'control_writable': false},
        {'control_revision': 1},
        {'fence_token': _otherId},
        {
          'epoch': 1,
          'control_revision': 1,
          'activation_control_revision': 1,
          'activation_digest': _repeat('d', 64),
        },
      ]) {
        final raw = _fixture('unobserved_lookup');
        raw['state'] = {..._map(raw['state']), ...mutation};
        raw['mutation_fenced'] = true;
        expect(
          StandingDispatchActivationLookup.fromJson(raw).mutationFenced,
          isTrue,
        );
        _refuses(StandingDispatchActivationLookup.fromJson, {
          ...raw,
          'mutation_fenced': false,
        });
      }
      final modeOnly = _fixture('unobserved_lookup');
      _map(modeOnly['state'])['mode'] = 'off';
      expect(
        StandingDispatchActivationLookup.fromJson(modeOnly).mutationFenced,
        isFalse,
      );
      for (final field in ['expected_revision', 'expected_epoch']) {
        final raw = _fixture('unobserved_lookup');
        if (field == 'expected_epoch') {
          _map(raw['request'])[field] = 1;
        } else {
          _at(raw, ['request', 'mode_change'])[field] = 1;
        }
        _refuses(StandingDispatchActivationLookup.fromJson, raw);
      }
    },
  );

  test(
    'Stop proof fences only its target and may leave a later epoch ready',
    () {
      final zero = StandingDispatchStop.fromJson(_fixture('zero_stop'));
      expect(zero.issuedEpoch, 0);
      expect(zero.activationDigest, isNull);
      expect(
        StandingDispatchStopProof.fromJson(
          _fixture('zero_stop_proof'),
          original: zero,
        ).targetFenced,
        isTrue,
      );
      _refuses(StandingDispatchStop.fromJson, {
        ..._fixture('zero_stop'),
        'activation_digest': _repeat('d', 64),
      });
      _refuses(StandingDispatchStop.fromJson, {
        ..._fixture('stop'),
        'activation_digest': null,
      });
      for (final mutation in <Json>[
        {'backend_id': _repeat('b', 32), 'producer': null},
        {'stopped_through_epoch': 0, 'dispatch_ready': true},
        {'activation_digest': _repeat('d', 64)},
      ]) {
        final raw = _fixture('stop_proof');
        raw['state'] = {..._map(raw['state']), ...mutation};
        _refuses(StandingDispatchStopProof.fromJson, raw);
      }
      _refuses(StandingDispatchStopProof.fromJson, {
        ..._fixture('stop_proof'),
        'target_fenced': false,
      });
      final later = _fixture('stop_proof');
      later['state'] = {
        ..._fixture('state'),
        'epoch': 2,
        'stopped_through_epoch': 1,
        'control_revision': 2,
        'activation_control_revision': 2,
        'activation_digest': _repeat('d', 64),
      };
      final proof = StandingDispatchStopProof.fromJson(later);
      expect(proof.targetFenced, isTrue);
      expect(proof.state.dispatchReady, isTrue);
      expect(proof.state.stoppedThroughEpoch, lessThan(proof.state.epoch));
    },
  );
}
