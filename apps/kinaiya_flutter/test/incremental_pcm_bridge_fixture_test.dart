import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/audio/incremental_pcm.dart';

void main() {
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
