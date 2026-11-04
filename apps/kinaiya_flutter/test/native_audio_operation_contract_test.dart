import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/converse_models.dart';
import 'package:kinaiya_flutter/api/stt_model_settings.dart';
import 'package:kinaiya_flutter/api/tts_model_settings.dart';

// Frozen public names, independent of production key inventories. These are
// metadata parsers; no provider/native operation or cleanup authority is faked.
const _base = {
  'preparing',
  'text',
  'text_admitting',
  'windows',
  'speech',
  'stt',
  'stt_cleanup',
  'native_audio',
  'playback_pending',
  'memory_pending',
};
const _max = 9007199254740990;
const _hash =
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const _authority = '0123456789abcdef0123456789abcdef';

Map<String, Object?> _snapshot(String role, {int native = 0}) {
  final operations = <String, Object?>{
    for (final key in _base) key: key == 'native_audio' ? native : 0,
    'leases': 0,
    'cleanup_pending': 0,
    'cleanup_failed': 0,
    if (role != 'converse') 'cleanup_unknown': 0,
  };
  final common = <String, Object?>{
    'schema_version': '1.0',
    'instance_id': role == 'converse'
        ? '01234567-89ab-4cde-8abc-0123456789ab'
        : _authority,
    'revision': 0,
    'state_token': _hash,
    'selection': {
      'source': 'host',
      'catalog_binding': null,
      if (role == 'converse') 'reasoning': null,
    },
    'selected_info': null,
    'selected_error': 'Host provider is unavailable',
    'host': {
      'provider': null,
      'selectable': false,
      if (role == 'converse') 'reasoning': null,
      if (role == 'tts') ...{'profile': null, 'voice': null},
    },
    'catalog_profiles': <Object?>[],
    'catalog_error': null,
    'busy': native != 0,
    'operation_state': operations,
    'change_boundary': 'settled_converse_operations',
    'qualification': 'not_checked',
    'readiness': 'unknown',
    'limits': {'leases': 64, 'receipts': 10000},
  };
  if (role == 'converse') {
    common['selected_reasoning'] = null;
  } else {
    common.addAll({
      'role': role,
      'backend_id': _authority,
      'selected_available': false,
    });
  }
  if (role == 'tts') {
    common.addAll({
      'selected_profile': null,
      'selected_voice': null,
      'budget': {
        'configured': false,
        'source': 'unavailable',
        'reason': 'No host budget',
      },
    });
  }
  return common;
}

Map<String, int> _parse(String role, Map<String, Object?> body) =>
    switch (role) {
      'converse' => ConverseModelsSnapshot.fromJson(body).operationState,
      'stt' => SttModelsSnapshot.fromJson(body).operationState,
      'tts' => TtsModelsSnapshot.fromJson(body).operationState,
      _ => throw StateError('Unknown fixture role'),
    };

Map<String, Object?> _operations(Map<String, Object?> body) =>
    body['operation_state'] as Map<String, Object?>;

void main() {
  for (final role in ['converse', 'stt', 'tts']) {
    test('$role retains native pending count and exact established keys', () {
      final idle = _parse(role, _snapshot(role));
      final expected = {
        ..._base,
        'leases',
        'cleanup_pending',
        'cleanup_failed',
        if (role != 'converse') 'cleanup_unknown',
      };
      expect(idle.keys.toSet(), expected);
      expect(idle.values, everyElement(0));
      final pending = _parse(role, _snapshot(role, native: 1));
      expect(pending['native_audio'], 1);
      expect(pending.keys.toSet(), expected);
      expect(_parse(role, _snapshot(role, native: _max))['native_audio'], _max);
    });

    test('$role requires every frozen base operation field', () {
      for (final key in _base) {
        final body = _snapshot(role);
        _operations(body).remove(key);
        expect(() => _parse(role, body), throwsFormatException, reason: key);
      }
    });

    test(
      '$role rejects unknown operation names and malformed native counts',
      () {
        final unknown = _snapshot(role);
        _operations(unknown)['unrecognized_operation'] = 0;
        expect(() => _parse(role, unknown), throwsFormatException);
        for (final count in <Object?>[true, -1, 1.0, '1', null, _max + 1]) {
          final body = _snapshot(role);
          _operations(body)['native_audio'] = count;
          expect(() => _parse(role, body), throwsFormatException);
        }
      },
    );

    test('$role cannot call a nonzero native owner idle', () {
      final body = _snapshot(role, native: 1);
      body['busy'] = false;
      expect(() => _parse(role, body), throwsFormatException);
    });

    test('$role still requires established lease and cleanup observations', () {
      for (final key in {
        'leases',
        'cleanup_pending',
        'cleanup_failed',
        if (role != 'converse') 'cleanup_unknown',
      }) {
        final body = _snapshot(role);
        _operations(body).remove(key);
        expect(() => _parse(role, body), throwsFormatException, reason: key);
      }
    });
  }
}
