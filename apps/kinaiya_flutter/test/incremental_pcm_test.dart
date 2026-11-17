import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/audio/incremental_pcm.dart';

Map<String, Object?> snapshot() => {
  'capture_epoch': 7,
  'sample_rate': 16000,
  'buffered_samples': 3,
  'received_samples': 3,
  'sample_offset': 0,
  'next_sequence': 1,
  'active': true,
  'fault': null,
  'cleanup_pending': false,
  'max_stream_samples': 960000,
  'max_chunk_samples': 16000,
};
Map<String, Object?> packet() => {
  'capture_epoch': 7,
  'sequence': 1,
  'sample_offset': 0,
  'sample_count': 3,
  'sample_rate': 16000,
  'user_speaking': true,
  'pcm16_base64': base64Encode([0, 0, 255, 127, 0, 128]),
};

void main() {
  test(
    'local segment boundaries detach exact counters and bind only a newer local epoch',
    () {
      final raw = {'capture_epoch': 7, 'next_sequence': 2, 'sample_offset': 3};
      final original = IncrementalPcmBoundary.fromJson(raw);
      raw['sample_offset'] = 4;
      expect(original.sampleOffset, 3);
      expect(() => original.body['sample_offset'] = 4, throwsUnsupportedError);
      final valid = {
        'schema_version': '1.0',
        'protocol': 'browser_pcm_segment_v1',
        'original': original.body,
        'capture_epoch': 8,
      };
      final proof = IncrementalPcmRotation.fromJson(valid, original);
      expect(proof.epoch, 8);
      for (final patch in [
        {'schema_version': '2.0'},
        {'protocol': 'converse_live_stream_v2'},
        {'capture_epoch': 7},
        {'capture_epoch': 8.0},
        {'capture_epoch': 9007199254740992},
        {'extra': true},
        {
          'original': {...original.body, 'sample_offset': 4},
        },
      ]) {
        expect(
          () => IncrementalPcmRotation.fromJson({...valid, ...patch}, original),
          throwsFormatException,
        );
      }
      for (final patch in [
        {'capture_epoch': true},
        {'next_sequence': 100001},
        {'sample_offset': 960001},
        {'sample_offset': 0},
        {'next_sequence': 1},
        {'extra': 0},
      ]) {
        expect(
          () => IncrementalPcmBoundary.fromJson({...original.body, ...patch}),
          throwsFormatException,
        );
      }
    },
  );
  test(
    'canonical PCM packet detaches its bytes and exact local acknowledgment identity',
    () {
      final raw = packet();
      final parsed = IncrementalPcmPacket.fromJson(raw);
      expect(parsed.epoch, 7);
      expect(parsed.sequence, 1);
      expect(parsed.sampleOffset, 0);
      expect(parsed.sampleCount, 3);
      expect(parsed.userSpeaking, isTrue);
      expect(base64Decode(parsed.pcm16Base64), [0, 0, 255, 127, 0, 128]);
      expect(
        parsed.pcmSha256,
        '6d1e4904d7ad52298e9f329ef739dfdb1ea033af5bbca23790841ef1d1cab3ee',
      );
      raw['capture_epoch'] = 8;
      expect(parsed.epoch, 7);
      final detached = IncrementalPcmPacket.fromJson({...packet()});
      final identity = detached.identity;
      expect(identity, {
        'capture_epoch': 7,
        'sequence': 1,
        'sample_offset': 0,
        'sample_count': 3,
      });
      expect(() => identity['sequence'] = 2, throwsUnsupportedError);
      expect(
        parsed.continues(IncrementalPcmState.fromJson(snapshot())),
        isTrue,
      );
    },
  );

  test(
    'packet continuity binds the current epoch, native cursor and available prefix',
    () {
      final p = IncrementalPcmPacket.fromJson(packet());
      for (final next in [
        {...snapshot(), 'capture_epoch': 8},
        {...snapshot(), 'buffered_samples': 2, 'received_samples': 2},
        {
          ...snapshot(),
          'sample_offset': 3,
          'next_sequence': 2,
          'received_samples': 6,
        },
      ]) {
        expect(p.continues(IncrementalPcmState.fromJson(next)), isFalse);
      }
      final stopped = IncrementalPcmState.fromJson({
        ...snapshot(),
        'active': false,
        'fault': 'invalid_pcm',
        'cleanup_pending': true,
      });
      expect(stopped.active, isFalse);
      expect(stopped.cleanupPending, isTrue);
      expect(
        p.continues(stopped),
        isTrue,
        reason: 'Continuity alone grants no dispatch or cleanup authority',
      );
    },
  );

  test(
    'untrusted state rejects wrong types, contradictory counters and unsupported bounds',
    () {
      for (final bad in [
        {...snapshot(), 'extra': null},
        {...snapshot()}..remove('cleanup_pending'),
        {...snapshot(), 'capture_epoch': 0},
        {...snapshot(), 'capture_epoch': 9007199254740992},
        {...snapshot(), 'sample_rate': 16000.0},
        {...snapshot(), 'max_stream_samples': 960000.0},
        {...snapshot(), 'max_chunk_samples': 16000.0},
        {...snapshot(), 'sample_rate': 48000},
        {...snapshot(), 'active': 1},
        {...snapshot(), 'cleanup_pending': null},
        {...snapshot(), 'received_samples': 3.0},
        {...snapshot(), 'buffered_samples': -1},
        {...snapshot(), 'received_samples': 960001},
        {...snapshot(), 'sample_offset': 1},
        {...snapshot(), 'next_sequence': 0},
        {...snapshot(), 'next_sequence': 100001},
        {...snapshot(), 'next_sequence': 2},
        {...snapshot(), 'fault': 'quality_low'},
        {...snapshot(), 'fault': 'invalid_pcm'},
        {...snapshot(), 'capture_epoch': null},
      ]) {
        expect(() => IncrementalPcmState.fromJson(bad), throwsFormatException);
      }
    },
  );

  test(
    'empty observations and retained faults never imply current microphone activity',
    () {
      final empty = IncrementalPcmState.fromJson({
        ...snapshot(),
        'capture_epoch': null,
        'buffered_samples': 0,
        'received_samples': 0,
        'active': false,
        'cleanup_pending': true,
      });
      expect(empty.epoch, isNull);
      expect(empty.active, isFalse);
      expect(empty.cleanupPending, isTrue);
      for (final fault in [
        'invalid_pcm',
        'stream_limit',
        'sequence_limit',
        'capture_unavailable',
      ]) {
        final f = IncrementalPcmState.fromJson({
          ...snapshot(),
          'active': false,
          'fault': fault,
        });
        expect(f.fault, fault);
        expect(f.bufferedSamples, 3);
      }
    },
  );

  test(
    'untrusted PCM refuses malformed, oversized or noncanonical original bytes',
    () {
      for (final bad in [
        {...packet(), 'extra': false},
        {...packet()}..remove('user_speaking'),
        {...packet(), 'capture_epoch': true},
        {...packet(), 'sequence': 0},
        {...packet(), 'sequence': 100000},
        {...packet(), 'sequence': 2},
        {...packet(), 'sample_offset': 1},
        {...packet(), 'sample_count': 0},
        {...packet(), 'sample_count': 16001},
        {...packet(), 'sample_rate': 16000.0},
        {...packet(), 'user_speaking': 1},
        {...packet(), 'pcm16_base64': ''},
        {...packet(), 'pcm16_base64': 'bad!'},
        {...packet(), 'pcm16_base64': 'AA=='},
        {
          ...packet(),
          'pcm16_base64': base64Encode([
            0,
            0,
            255,
            127,
            0,
            128,
          ]).replaceAll('A', '%41'),
        },
        {...packet(), 'pcm16_base64': 'A' * 42669},
      ]) {
        expect(() => IncrementalPcmPacket.fromJson(bad), throwsFormatException);
      }
    },
  );

  test('full size last packet retains exact native stream limits', () {
    final raw = {
      ...packet(),
      'sequence': 61,
      'sample_offset': 944000,
      'sample_count': 16000,
      'pcm16_base64': base64Encode(List<int>.filled(32000, 0)),
    };
    final p = IncrementalPcmPacket.fromJson(raw);
    expect(p.sampleOffset + p.sampleCount, 960000);
    expect(p.pcm16Base64.length, 42668);
    expect(
      () => IncrementalPcmPacket.fromJson({...raw, 'sample_offset': 944001}),
      throwsFormatException,
    );
  });
}
