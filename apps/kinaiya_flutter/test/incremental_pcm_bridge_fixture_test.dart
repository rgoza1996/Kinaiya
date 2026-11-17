import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/audio/incremental_pcm.dart';

void main() {
  test('Dart binds actual bridge rollover and every retained tail sample', () {
    final f =
        jsonDecode(
              File(
                'test/fixtures/incremental_pcm_rollover_bridge.json',
              ).readAsStringSync(),
            )
            as Map<String, Object?>;
    final original = IncrementalPcmBoundary.fromState(
      IncrementalPcmState.fromJson(f['before']),
    );
    final proof = IncrementalPcmRotation.fromJson(f['rotation'], original);
    final after = IncrementalPcmState.fromJson(f['after']);
    final packet = IncrementalPcmPacket.fromJson(f['packet']);
    expect(original.body, f['original']);
    expect(proof.epoch, greaterThan(original.epoch));
    expect(after.epoch, proof.epoch);
    expect(after.sampleOffset, 0);
    expect(after.nextSequence, 1);
    expect(after.receivedSamples, 3);
    expect(after.bufferedSamples, 3);
    expect(after.active, isTrue);
    expect(packet.continues(after), isTrue);
    expect(packet.userSpeaking, isTrue);
    expect(base64Decode(packet.pcm16Base64), [255, 31, 0, 224, 255, 127]);
    expect(IncrementalPcmState.fromJson(f['stopped']).epoch, proof.epoch);
  });
  test(
    'Dart consumes actual browser bridge PCM and retirement observations',
    () {
      final fixture =
          jsonDecode(
                File(
                  'test/fixtures/incremental_pcm_bridge.json',
                ).readAsStringSync(),
              )
              as Map<String, Object?>;
      final before = IncrementalPcmState.fromJson(fixture['before']);
      final original = IncrementalPcmPacket.fromJson(fixture['packet']);
      expect(original.continues(before), isTrue);
      expect(original.userSpeaking, isTrue);
      expect(base64Decode(original.pcm16Base64), [
        0,
        0,
        255,
        63,
        0,
        192,
        255,
        127,
        0,
        128,
      ]);
      final after = IncrementalPcmState.fromJson(fixture['after']);
      expect(original.continues(after), isFalse);
      expect(after.sampleOffset, 5);
      expect(after.receivedSamples, 5);
      expect(after.bufferedSamples, 0);
      expect(after.nextSequence, 2);
      final stopped = IncrementalPcmState.fromJson(fixture['stopped']);
      expect(stopped.epoch, original.epoch);
      expect(stopped.active, isFalse);
      expect(stopped.cleanupPending, isFalse);
      final released = IncrementalPcmState.fromJson(fixture['released']);
      expect(released.epoch, isNull);
      expect(released.active, isFalse);
    },
  );
}
