/// Local browser PCM observations. These carry no native inference authority.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart';

const maxIncrementalPcmSamples = 960000;
const maxIncrementalPcmChunkSamples = 16000;
const maxIncrementalPcmEpoch = 9007199254740991;

const _invalid = FormatException('Invalid incremental microphone observation');

Map<String, Object?> _object(Object? value, Set<String> fields) {
  if (value is! Map<String, Object?> ||
      value.keys.toSet().difference(fields).isNotEmpty ||
      fields.difference(value.keys.toSet()).isNotEmpty) {
    throw _invalid;
  }
  return value;
}

int _integer(Object? value, int maximum, {int minimum = 0}) {
  if (value is! int || value < minimum || value > maximum) throw _invalid;
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) throw _invalid;
  return value;
}

class IncrementalPcmState {
  IncrementalPcmState.fromJson(Object? value) {
    final raw = _object(value, {
      'capture_epoch',
      'sample_rate',
      'buffered_samples',
      'received_samples',
      'sample_offset',
      'next_sequence',
      'active',
      'fault',
      'cleanup_pending',
      'max_stream_samples',
      'max_chunk_samples',
    });
    _integer(raw['sample_rate'], 16000, minimum: 16000);
    _integer(
      raw['max_stream_samples'],
      maxIncrementalPcmSamples,
      minimum: maxIncrementalPcmSamples,
    );
    _integer(
      raw['max_chunk_samples'],
      maxIncrementalPcmChunkSamples,
      minimum: maxIncrementalPcmChunkSamples,
    );
    epoch = raw['capture_epoch'] == null
        ? null
        : _integer(raw['capture_epoch'], maxIncrementalPcmEpoch, minimum: 1);
    bufferedSamples = _integer(
      raw['buffered_samples'],
      maxIncrementalPcmSamples,
    );
    receivedSamples = _integer(
      raw['received_samples'],
      maxIncrementalPcmSamples,
    );
    sampleOffset = _integer(raw['sample_offset'], maxIncrementalPcmSamples);
    nextSequence = _integer(raw['next_sequence'], 100000, minimum: 1);
    active = _flag(raw['active']);
    cleanupPending = _flag(raw['cleanup_pending']);
    if (raw['fault'] != null &&
        !const {
          'invalid_pcm',
          'stream_limit',
          'sequence_limit',
          'capture_unavailable',
        }.contains(raw['fault'])) {
      throw _invalid;
    }
    fault = raw['fault'] as String?;
    if (sampleOffset + bufferedSamples != receivedSamples ||
        sampleOffset < nextSequence - 1 ||
        sampleOffset > (nextSequence - 1) * maxIncrementalPcmChunkSamples ||
        (active && (epoch == null || fault != null)) ||
        (epoch == null &&
            (receivedSamples != 0 || nextSequence != 1 || fault != null))) {
      throw _invalid;
    }
  }

  late final int? epoch;
  late final int bufferedSamples, receivedSamples, sampleOffset, nextSequence;
  late final bool active, cleanupPending;
  late final String? fault;
}

class IncrementalPcmPacket {
  IncrementalPcmPacket.fromJson(Object? value) {
    final raw = _object(value, {
      'capture_epoch',
      'sequence',
      'sample_offset',
      'sample_count',
      'sample_rate',
      'user_speaking',
      'pcm16_base64',
    });
    epoch = _integer(raw['capture_epoch'], maxIncrementalPcmEpoch, minimum: 1);
    sequence = _integer(raw['sequence'], 99999, minimum: 1);
    sampleOffset = _integer(raw['sample_offset'], maxIncrementalPcmSamples);
    sampleCount = _integer(
      raw['sample_count'],
      maxIncrementalPcmChunkSamples,
      minimum: 1,
    );
    userSpeaking = _flag(raw['user_speaking']);
    final encoded = raw['pcm16_base64'];
    _integer(raw['sample_rate'], 16000, minimum: 16000);
    if (sampleOffset + sampleCount > maxIncrementalPcmSamples ||
        sampleOffset < sequence - 1 ||
        sampleOffset > (sequence - 1) * maxIncrementalPcmChunkSamples ||
        encoded is! String ||
        encoded.isEmpty ||
        encoded.length > 42668) {
      throw _invalid;
    }
    try {
      final bytes = base64Decode(encoded);
      if (bytes.length != sampleCount * 2 || base64Encode(bytes) != encoded) {
        throw _invalid;
      }
      pcmSha256 = sha256.convert(bytes).toString();
    } on FormatException {
      throw _invalid;
    }
    pcm16Base64 = encoded;
  }

  late final int epoch, sequence, sampleOffset, sampleCount;
  late final bool userSpeaking;
  late final String pcm16Base64, pcmSha256;

  Map<String, Object?> get identity => Map.unmodifiable({
    'capture_epoch': epoch,
    'sequence': sequence,
    'sample_offset': sampleOffset,
    'sample_count': sampleCount,
  });

  /// Continuity only: caller still owns native availability, scope, pending
  /// operation, stop/fault and exact publication checks before dispatch.
  bool continues(IncrementalPcmState state) =>
      state.epoch == epoch &&
      state.nextSequence == sequence &&
      state.sampleOffset == sampleOffset &&
      state.bufferedSamples >= sampleCount;
}

/// Exact acknowledged local prefix, separate from native finish authority.
class IncrementalPcmBoundary {
  IncrementalPcmBoundary.fromJson(Object? value) {
    final raw = _object(value, {
      'capture_epoch',
      'next_sequence',
      'sample_offset',
    });
    epoch = _integer(raw['capture_epoch'], maxIncrementalPcmEpoch, minimum: 1);
    nextSequence = _integer(raw['next_sequence'], 100000, minimum: 1);
    sampleOffset = _integer(raw['sample_offset'], maxIncrementalPcmSamples);
    if (sampleOffset < nextSequence - 1 ||
        sampleOffset > (nextSequence - 1) * maxIncrementalPcmChunkSamples) {
      throw _invalid;
    }
  }
  factory IncrementalPcmBoundary.fromState(IncrementalPcmState original) =>
      IncrementalPcmBoundary.fromJson({
        'capture_epoch': original.epoch,
        'next_sequence': original.nextSequence,
        'sample_offset': original.sampleOffset,
      });
  late final int epoch, nextSequence, sampleOffset;
  Map<String, Object?> get body => Map.unmodifiable({
    'capture_epoch': epoch,
    'next_sequence': nextSequence,
    'sample_offset': sampleOffset,
  });
  bool matches(IncrementalPcmBoundary other) =>
      epoch == other.epoch &&
      nextSequence == other.nextSequence &&
      sampleOffset == other.sampleOffset;
}

/// Local rotation only: no native inference, physical-stop or utterance claim.
class IncrementalPcmRotation {
  IncrementalPcmRotation.fromJson(
    Object? value,
    IncrementalPcmBoundary expected,
  ) {
    final raw = _object(value, {
      'schema_version',
      'protocol',
      'original',
      'capture_epoch',
    });
    original = IncrementalPcmBoundary.fromJson(raw['original']);
    epoch = _integer(raw['capture_epoch'], maxIncrementalPcmEpoch, minimum: 1);
    if (raw['schema_version'] != '1.0' ||
        raw['protocol'] != 'browser_pcm_segment_v1' ||
        !original.matches(expected) ||
        epoch <= original.epoch) {
      throw _invalid;
    }
  }
  late final IncrementalPcmBoundary original;
  late final int epoch;
}
