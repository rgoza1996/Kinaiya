/// Exact v2 mic/decoder originals. Native permission is separate from physical stop.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart';

const liveStreamProtocol = 'converse_live_stream_v2';
const _maxInteger = 9007199254740991;
const _invalid = FormatException('Invalid original Adaptive Live stream proof');
const _identity = {
  'schema_version',
  'protocol',
  'owner_instance',
  'capture_id',
};
const _scope = {
  ..._identity,
  'session_id',
  'thread_id',
  'epoch',
  'utterance_id',
  'content_scope',
};
const _segment = {..._scope, 'segment_index'};
const _advance = {
  ..._segment,
  'stream_id',
  'worker_incarnation',
  'sequence',
  'sample_offset',
  'user_speaking',
};
const _reasons = {
  'owner_off',
  'cancelled',
  'disconnected',
  'replaced',
  'shutdown',
};

Map<String, Object?> _object(Object? value, [Set<String>? fields]) {
  if (value is! Map<String, Object?> ||
      (fields != null &&
          (value.length != fields.length ||
              !value.keys.every(fields.contains)))) {
    throw _invalid;
  }
  return value;
}

Object? _freeze(Object? value) => value is Map<String, Object?>
    ? Map<String, Object?>.unmodifiable(
        value.map((k, v) => MapEntry(k, _freeze(v))),
      )
    : value is List
    ? List<Object?>.unmodifiable(value.map(_freeze))
    : value;

Object? _canonical(Object? value) => value is Map<String, Object?>
    ? {
        for (final key in value.keys.toList()..sort())
          key: _canonical(value[key]),
      }
    : value is List
    ? value.map(_canonical).toList()
    : value;

String _digest(Object? value) =>
    sha256.convert(utf8.encode(jsonEncode(_canonical(value)))).toString();
bool _equal(Object? a, Object? b) =>
    jsonEncode(_canonical(a)) == jsonEncode(_canonical(b));

String _hex(Object? value, int size) {
  if (value is! String ||
      value.length != size ||
      !RegExp(r'^[a-f0-9]+$').hasMatch(value)) {
    throw _invalid;
  }
  return value;
}

String _text(Object? value, int maximum) {
  if (value is! String || value.isEmpty || value.runes.length > maximum) {
    throw _invalid;
  }
  return value;
}

int _integer(Object? value, int maximum, [int minimum = 0]) {
  if (value is! int || value < minimum || value > maximum) throw _invalid;
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) throw _invalid;
  return value;
}

void _version(
  Map<String, Object?> value,
  String protocol, [
  String schema = '2.0',
]) {
  if (value['schema_version'] != schema || value['protocol'] != protocol) {
    throw _invalid;
  }
}

void _sameFields(
  Map<String, Object?> a,
  Map<String, Object?> b,
  Iterable<String> keys,
) {
  if (keys.any((key) => !_equal(a[key], b[key]))) throw _invalid;
}

void _validateIdentity(Map<String, Object?> value) {
  _version(value, liveStreamProtocol);
  _hex(value['owner_instance'], 32);
  _hex(value['capture_id'], 32);
}

enum LiveStreamPhase { begin, pcm, finish }

class LiveStreamCapability {
  LiveStreamCapability.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'protocol',
      'owner_instance',
      'available',
    });
    _version(raw, liveStreamProtocol);
    ownerInstance = _hex(raw['owner_instance'], 32);
    available = _flag(raw['available']);
  }
  late final String ownerInstance;
  late final bool available;
}

class LiveUtteranceCapability {
  LiveUtteranceCapability.fromJson(Object? value) {
    final raw = _object(value, {
      'schema_version',
      'protocol',
      'owner_instance',
      'available',
    });
    _version(raw, 'native_live_utterance_v1', '1.0');
    ownerInstance = _hex(raw['owner_instance'], 32);
    available = _flag(raw['available']);
  }
  late final String ownerInstance;
  late final bool available;
}

class LiveStreamAbortCapability {
  LiveStreamAbortCapability.fromJson(Object? value)
    : this._(value, 'native_live_abort_v1');
  LiveStreamAbortCapability._(Object? value, String protocol) {
    final raw = _object(value, {
      'schema_version',
      'protocol',
      'owner_instance',
    });
    _version(raw, protocol, '1.0');
    ownerInstance = _hex(raw['owner_instance'], 32);
  }
  late final String ownerInstance;
}

class LiveStreamRetireCapability extends LiveStreamAbortCapability {
  LiveStreamRetireCapability.fromJson(Object? value)
    : super._(value, 'native_live_retire_v1');
}

class LiveStreamAbortOperation {
  LiveStreamAbortOperation(LiveStreamOperation initial, String reason)
    : this._(initial, reason, 'native_live_abort_v1');
  LiveStreamAbortOperation._(this.initial, String reason, String protocol) {
    if (initial.phase != LiveStreamPhase.begin ||
        initial.isUtteranceBoundary ||
        initial.body['segment_index'] != 0 ||
        !_reasons.contains(reason)) {
      throw _invalid;
    }
    body =
        _freeze({
              'schema_version': '1.0',
              'protocol': protocol,
              'initial': initial.body,
              'reason': reason,
            })
            as Map<String, Object?>;
    if (utf8.encode(jsonEncode(body)).length > 4096) throw _invalid;
    digest = _digest(body);
  }
  final LiveStreamOperation initial;
  late final Map<String, Object?> body;
  late final String digest;
}

class LiveStreamRetireOperation extends LiveStreamAbortOperation {
  LiveStreamRetireOperation(LiveStreamOperation initial, String reason)
    : super._(initial, reason, 'native_live_retire_v1');
}

class LiveStreamAbortProof {
  LiveStreamAbortProof.fromJson(
    Object? value,
    LiveStreamAbortOperation original,
  ) : this._(value, original, 'native_live_abort_v1');
  LiveStreamAbortProof._(
    Object? value,
    this.original,
    String protocol, {
    bool permitObsolete = false,
  }) {
    final raw = _object(value, {
      'schema_version',
      'protocol',
      'original',
      'request_sha256',
      'outcome',
      'capture',
      'cleanup_confirmed',
    });
    _version(raw, protocol, '1.0');
    if (!_equal(raw['original'], original.body) ||
        _hex(raw['request_sha256'], 64) != original.digest ||
        !{'unknown', 'in_progress', 'confirmed'}.contains(raw['outcome'])) {
      throw _invalid;
    }
    outcome = raw['outcome'] as String;
    capture = raw['capture'] == null
        ? null
        : LiveCaptureObservation.fromJson(
            raw['capture'],
            original.initial.body,
          );
    cleanupConfirmed = _flag(raw['cleanup_confirmed']);
    if ((confirmed &&
            (capture == null ||
                capture!.captureOpen ||
                !(capture!.body['fence'] != null ||
                    (permitObsolete && capture!.permanentlyClosed)))) ||
        (cleanupConfirmed && !confirmed)) {
      throw _invalid;
    }
  }
  final LiveStreamAbortOperation original;
  late final String outcome;
  late final LiveCaptureObservation? capture;
  late final bool cleanupConfirmed;
  bool get confirmed => outcome == 'confirmed';
}

class LiveStreamRetireProof extends LiveStreamAbortProof {
  LiveStreamRetireProof.fromJson(
    Object? value,
    LiveStreamRetireOperation original,
  ) : super._(value, original, 'native_live_retire_v1', permitObsolete: true);
}

/// Detach and hash once. Recovery never recreates or resends a mutation.
class LiveStreamOperation {
  LiveStreamOperation.fromJson(LiveStreamPhase phase, Object? value)
    : this._(phase, value, false);
  LiveStreamOperation.nextUtterance(Object? value)
    : this._(LiveStreamPhase.begin, value, true);

  factory LiveStreamOperation.advanceUtterance(LiveStreamProof previous) {
    if (!previous.segmentRetired ||
        previous.capture?.captureOpen != true ||
        previous.original.body['capture_ended'] == true) {
      throw _invalid;
    }
    final body = previous.original.body;
    final index = (body['segment_index'] as int) + 1;
    return LiveStreamOperation.nextUtterance({
      for (final key in _segment) key: body[key],
      'segment_index': index,
      'utterance_id': '${body['capture_id']}_$index',
      'boundary_protocol': 'native_live_utterance_v1',
      'previous_utterance_id': body['utterance_id'],
      'previous_finish_sha256': previous.original.digest,
    });
  }

  LiveStreamOperation._(this.phase, Object? value, this.isUtteranceBoundary) {
    final fields = isUtteranceBoundary
        ? {
            ..._segment,
            'boundary_protocol',
            'previous_utterance_id',
            'previous_finish_sha256',
          }
        : switch (phase) {
            LiveStreamPhase.begin => _segment,
            LiveStreamPhase.pcm => {..._advance, 'pcm16_base64'},
            LiveStreamPhase.finish => {..._advance, 'capture_ended'},
          };
    final raw = _object(value, fields);
    _validateIdentity(raw);
    for (final key in ['session_id', 'thread_id']) {
      _text(raw[key], 128);
    }
    _integer(raw['epoch'], _maxInteger, 1);
    final utterance = _text(raw['utterance_id'], 128);
    if (!RegExp(r'^[A-Za-z0-9_-]+$').hasMatch(utterance) ||
        !{
          'local_only',
          'public_synthetic',
          'private_cloud_authorized',
        }.contains(raw['content_scope'])) {
      throw _invalid;
    }
    _integer(raw['segment_index'], 1000000);
    if (isUtteranceBoundary) {
      final index = _integer(raw['segment_index'], 1000000, 1);
      final previous = _text(raw['previous_utterance_id'], 128);
      _hex(raw['previous_finish_sha256'], 64);
      if (raw['boundary_protocol'] != 'native_live_utterance_v1' ||
          !RegExp(r'^[A-Za-z0-9_-]+$').hasMatch(previous) ||
          utterance != '${raw['capture_id']}_$index' ||
          utterance == previous) {
        throw _invalid;
      }
    }
    if (phase != LiveStreamPhase.begin) {
      _hex(raw['stream_id'], 64);
      _hex(raw['worker_incarnation'], 64);
      _integer(raw['sequence'], 100000, 1);
      final offset = _integer(raw['sample_offset'], 960000);
      _flag(raw['user_speaking']);
      if (phase == LiveStreamPhase.pcm) {
        final encoded = _text(raw['pcm16_base64'], 64000);
        final bytes = base64Decode(encoded);
        if (bytes.length.isOdd ||
            bytes.isEmpty ||
            bytes.length > 32000 ||
            base64Encode(bytes) != encoded ||
            offset + bytes.length ~/ 2 > 960000) {
          throw _invalid;
        }
      } else if (_flag(raw['capture_ended']) && raw['user_speaking'] == true) {
        throw _invalid;
      }
    }
    if (utf8.encode(jsonEncode(raw)).length >
        (phase == LiveStreamPhase.pcm ? 96000 : 4096)) {
      throw _invalid;
    }
    body = _freeze(raw) as Map<String, Object?>;
    digest = _digest(body);
  }
  final LiveStreamPhase phase;
  final bool isUtteranceBoundary;
  String get route => isUtteranceBoundary ? 'next-utterance' : phase.name;
  late final Map<String, Object?> body;
  late final String digest;
  Map<String, Object?> get identity =>
      Map.unmodifiable({for (final key in _identity) key: body[key]});
  // The boundary original keeps its extra final-source proof. Subsequent PCM,
  // finish and ordinary decoder starts carry only the declared segment scope.
  Map<String, Object?> get segment =>
      Map.unmodifiable({for (final key in _segment) key: body[key]});
  Map<String, Object?> get lookup => Map.unmodifiable({
    ...identity,
    'phase': phase.name,
    'request_sha256': digest,
  });
}

Map<String, Object?> _backend(Object? value) {
  final raw = _object(value, {
    'schema_version',
    'protocol',
    'backend_id',
    'controller_id',
  });
  _version(raw, 'native_live_capture_v2');
  _hex(raw['backend_id'], 32);
  _hex(raw['controller_id'], 32);
  return raw;
}

class LiveCaptureObservation {
  LiveCaptureObservation.fromJson(Object? value, Map<String, Object?> binding) {
    final raw = _object(value, {
      'schema_version',
      'protocol',
      'original',
      'backend',
      'receipt',
      'fence',
      'scope_current',
      'capture_open',
    });
    _version(raw, 'native_live_capture_v2');
    final original = _object(raw['original'], {
      'schema_version',
      'protocol',
      'backend',
      'owner_instance',
      'capture_id',
      'session_id',
      'thread_id',
      'epoch',
    });
    _version(original, 'native_live_capture_v2');
    _hex(original['owner_instance'], 32);
    _hex(original['capture_id'], 32);
    _text(original['session_id'], 128);
    _text(original['thread_id'], 128);
    _integer(original['epoch'], _maxInteger, 1);
    _sameFields(original, binding, [
      'owner_instance',
      'capture_id',
      if (binding.containsKey('session_id')) ...[
        'session_id',
        'thread_id',
        'epoch',
      ],
    ]);
    final current = _backend(raw['backend']);
    final registeredBackend = _backend(original['backend']);
    registered = raw['receipt'] != null;
    if (registered) {
      final receipt = _object(raw['receipt'], {
        'schema_version',
        'protocol',
        'original',
        'registration_id',
        'registered_sequence',
      });
      _version(receipt, 'native_live_capture_v2');
      if (!_equal(receipt['original'], original)) throw _invalid;
      _hex(receipt['registration_id'], 32);
      _integer(receipt['registered_sequence'], _maxInteger);
    }
    if (raw['fence'] != null) {
      final fence = _object(raw['fence'], {
        'schema_version',
        'protocol',
        'request',
        'fence_id',
      });
      _version(fence, 'native_live_capture_v2');
      _hex(fence['fence_id'], 32);
      final request = _object(fence['request'], {
        'schema_version',
        'protocol',
        'original',
        'request_id',
        'reason',
      });
      _version(request, 'native_live_capture_v2');
      _hex(request['request_id'], 32);
      if (!_reasons.contains(request['reason']) ||
          !_equal(request['original'], original)) {
        throw _invalid;
      }
    }
    captureOpen = _flag(raw['capture_open']);
    final expected =
        registered &&
        raw['fence'] == null &&
        _flag(raw['scope_current']) &&
        _equal(current, registeredBackend);
    // Validate the flag even when a preceding conjunction is false.
    _flag(raw['scope_current']);
    if (captureOpen != expected) throw _invalid;
    body = _freeze(raw) as Map<String, Object?>;
  }
  late final bool registered, captureOpen;
  late final Map<String, Object?> body;
  bool get permanentlyClosed =>
      !captureOpen &&
      (body['fence'] != null ||
          !_equal(_object(body['original'])['backend'], body['backend']) ||
          body['scope_current'] == false);
}

Map<String, Object?> _entry(Object? value, Map<String, Object?> binding) {
  final raw = _object(value, {
    'schema_version',
    'session_id',
    'thread_id',
    'epoch',
    'kind',
    'payload',
    'causes',
    'task_refs',
    'idempotency_key',
    'event_id',
    'sequence',
    'origin',
    'created_at',
  });
  if (raw['schema_version'] != '1.0') throw _invalid;
  _sameFields(raw, binding, ['session_id', 'thread_id', 'epoch']);
  _text(raw['event_id'], 128);
  _text(raw['idempotency_key'], 256);
  _integer(raw['sequence'], _maxInteger - 1, 1);
  if (raw['created_at'] is! String ||
      raw['causes'] is! List ||
      raw['task_refs'] is! List ||
      (raw['task_refs'] as List).isNotEmpty) {
    throw _invalid;
  }
  _object(raw['payload']);
  return raw;
}

void _publication(
  Object? value,
  LiveStreamOperation operation,
  Object? nativeEntry,
) {
  final raw = _object(value, {
    'schema_version',
    'protocol',
    'original',
    'source',
    'capture',
  });
  _version(raw, 'live_window_publication_v2');
  final ref = _object(raw['original'], {
    'schema_version',
    'protocol',
    'owner_instance',
    'capture_id',
    'session_id',
    'thread_id',
    'epoch',
    'utterance_id',
    'idempotency_key',
    'request_sha256',
  });
  _version(ref, 'live_window_publication_v2');
  _sameFields(ref, operation.body, [
    'owner_instance',
    'capture_id',
    'session_id',
    'thread_id',
    'epoch',
    'utterance_id',
  ]);
  _hex(ref['request_sha256'], 64);
  final source = _entry(raw['source'], operation.body),
      capture = _entry(raw['capture'], operation.body);
  final window = _object(source['payload'], {
    'schema_version',
    'session_id',
    'thread_id',
    'epoch',
    'utterance_id',
    'window_index',
    'revision',
    'text',
    'start_ms',
    'end_ms',
    'user_speaking',
    'capture_ended',
    'audio_sha256',
    'stt_model',
    'content_scope',
  });
  _sameFields(window, operation.body, [
    'session_id',
    'thread_id',
    'epoch',
    'utterance_id',
    'content_scope',
  ]);
  if (window['schema_version'] != '1.0' ||
      window['text'] is! String ||
      (window['text'] as String).runes.length > 16000) {
    throw _invalid;
  }
  _integer(window['window_index'], 1000000);
  _integer(window['revision'], _maxInteger, 1);
  final start = _integer(window['start_ms'], _maxInteger),
      end = _integer(window['end_ms'], _maxInteger);
  if (end < start || end - start > 60000) throw _invalid;
  _flag(window['user_speaking']);
  _flag(window['capture_ended']);
  if (window['capture_ended'] == true && window['user_speaking'] == true) {
    throw _invalid;
  }
  if (window['audio_sha256'] != null) _hex(window['audio_sha256'], 64);
  if (window['stt_model'] != null) _object(window['stt_model']);
  final marker = _object(capture['payload'], {
    'schema_version',
    'protocol',
    'capture_mode',
    'original',
    'source_event_id',
    'source_sequence',
    'source_revision',
    'input_sha256',
    'eligible_revision',
    'eligible_source_event_id',
    'eligible_source_sequence',
    'eligible_source_revision',
  });
  _version(marker, 'live_window_publication_v2');
  final sourceRequest = {
    for (final key in source.keys)
      if (!{'event_id', 'sequence', 'origin', 'created_at'}.contains(key))
        key: source[key],
  };
  if ((nativeEntry != null && !_equal(source, nativeEntry)) ||
      source['kind'] != 'conversation.window' ||
      source['origin'] != 'authenticated-user' ||
      (source['causes'] as List).isNotEmpty ||
      source['idempotency_key'] != ref['idempotency_key'] ||
      _digest(sourceRequest) != ref['request_sha256'] ||
      marker['capture_mode'] != 'adaptive_live' ||
      !_equal(marker['original'], ref) ||
      marker['source_event_id'] != source['event_id'] ||
      marker['source_sequence'] != source['sequence'] ||
      marker['source_revision'] != window['revision'] ||
      marker['input_sha256'] !=
          _digest({
            for (final key in [
              'text',
              'user_speaking',
              'capture_ended',
              'content_scope',
              'stt_model',
            ])
              key: window[key],
          }) ||
      capture['kind'] != 'conversation.window_capture' ||
      capture['origin'] != 'converse' ||
      !_equal(capture['causes'], [source['event_id']]) ||
      capture['idempotency_key'] !=
          'live-window-capture:${source['event_id']}' ||
      capture['sequence'] != (source['sequence'] as int) + 1) {
    throw _invalid;
  }
  _integer(marker['source_sequence'], _maxInteger - 1, 1);
  _integer(marker['source_revision'], _maxInteger, 1);
  _integer(marker['eligible_revision'], _maxInteger, 1);
  _integer(marker['eligible_source_revision'], _maxInteger, 1);
  _text(marker['eligible_source_event_id'], 128);
  final eligible = _integer(
    marker['eligible_source_sequence'],
    _maxInteger - 1,
    1,
  );
  if (eligible > (source['sequence'] as int) ||
      (eligible == source['sequence'] &&
          (marker['eligible_source_event_id'] != source['event_id'] ||
              marker['eligible_source_revision'] != window['revision']))) {
    throw _invalid;
  }
}

class LiveStreamProof {
  LiveStreamProof.fromJson(Object? value, this.original) {
    final raw = _object(value, {
      ..._identity,
      'phase',
      'request_sha256',
      'segment_index',
      'segment_sample_offset',
      'capture',
      'native',
      'publication',
    });
    _validateIdentity(raw);
    _sameFields(raw, original.body, _identity);
    if (raw['phase'] != original.phase.name ||
        raw['request_sha256'] != original.digest) {
      throw _invalid;
    }
    if (raw['segment_index'] != null &&
        _integer(raw['segment_index'], 1000000) !=
            original.body['segment_index']) {
      throw _invalid;
    }
    segmentSampleOffset = raw['segment_sample_offset'] == null
        ? null
        : _integer(raw['segment_sample_offset'], _maxInteger);
    capture = raw['capture'] == null
        ? null
        : LiveCaptureObservation.fromJson(raw['capture'], original.body);
    final native = _object(raw['native'], {
      'schema_version',
      'protocol',
      'owner_instance',
      'phase',
      'request_sha256',
      'outcome',
      'result',
      'native_original',
      'decoder_finished',
      'process_released',
      'never_dispatched',
      'ownership_retired',
    });
    _version(native, 'converse_stream_owner_v1', '1.0');
    nativeOwner = _hex(native['owner_instance'], 32);
    if (native['phase'] != raw['phase'] ||
        native['request_sha256'] != original.digest ||
        !{
          'confirmed',
          'publication_pending',
          'in_progress',
          'unknown',
          'not_dispatched',
        }.contains(native['outcome'])) {
      throw _invalid;
    }
    outcome = native['outcome'] as String;
    decoderFinished = _flag(native['decoder_finished']);
    processReleased = _flag(native['process_released']);
    ownershipRetired = _flag(native['ownership_retired']);
    _flag(native['never_dispatched']);
    if (native['native_original'] != null) {
      final operation = _object(native['native_original'], {
        'schema_version',
        'protocol',
        'broker_instance',
        'phase',
        'request_sha256',
        'operation_id',
      });
      _version(operation, 'whistle_stream_owner_v1', '1.0');
      _hex(operation['broker_instance'], 32);
      _hex(operation['operation_id'], 32);
      _hex(operation['request_sha256'], 64);
      if (operation['phase'] != original.phase.name) throw _invalid;
    }
    if (confirmed != (native['result'] != null)) throw _invalid;
    streamId = confirmed
        ? _hex(_object(native['result'])['stream_id'], 64)
        : null;
    workerIncarnation = confirmed
        ? _hex(_object(native['result'])['worker_incarnation'], 64)
        : null;
    if (confirmed) {
      if (capture?.registered != true ||
          raw['segment_index'] == null ||
          segmentSampleOffset == null) {
        throw _invalid;
      }
      final result = _object(native['result']);
      if (original.phase == LiveStreamPhase.begin) {
        _sameFields(result, original.body, [
          'session_id',
          'thread_id',
          'epoch',
          'utterance_id',
          'content_scope',
        ]);
        if (result['sequence'] is! int ||
            result['sequence'] != 0 ||
            result['sample_offset'] is! int ||
            result['sample_offset'] != 0) {
          throw _invalid;
        }
      } else {
        _sameFields(result, original.body, ['stream_id', 'worker_incarnation']);
        if (result['pcm_sequence'] is! int ||
            result['pcm_sequence'] != original.body['sequence'] ||
            result['final'] != (original.phase == LiveStreamPhase.finish)) {
          throw _invalid;
        }
        final expectedSamples =
            (original.body['sample_offset'] as int) +
            (original.phase == LiveStreamPhase.pcm
                ? base64Decode(
                        original.body['pcm16_base64'] as String,
                      ).length ~/
                      2
                : 0);
        if (result['received_samples'] is! int ||
            result['received_samples'] != expectedSamples) {
          throw _invalid;
        }
        if (original.phase == LiveStreamPhase.finish &&
            result['capture_ended'] != original.body['capture_ended']) {
          throw _invalid;
        }
        if (raw['publication'] == null) throw _invalid;
      }
    }
    if (raw['publication'] != null) {
      if (original.phase == LiveStreamPhase.begin || capture == null) {
        throw _invalid;
      }
      _publication(
        raw['publication'],
        original,
        confirmed ? _object(native['result'])['entry'] : null,
      );
    }
    body = _freeze(raw) as Map<String, Object?>;
  }
  final LiveStreamOperation original;
  late final Map<String, Object?> body;
  late final String outcome, nativeOwner;
  late final String? streamId, workerIncarnation;
  late final int? segmentSampleOffset;
  late final LiveCaptureObservation? capture;
  late final bool decoderFinished, processReleased, ownershipRetired;
  bool get confirmed => outcome == 'confirmed';
  // A logical microphone retains its STT lifetime across decoder segments.
  // Require the exact final source and actual decoder/HTTP cleanup, while
  // keeping process release separate from rollover permission.
  bool get segmentRetired =>
      original.phase == LiveStreamPhase.finish &&
      confirmed &&
      decoderFinished &&
      ownershipRetired;
}

class LiveStreamCancellation {
  LiveStreamCancellation.fromJson(
    Object? value,
    Map<String, Object?> identity,
  ) {
    final raw = _object(value, {
      ..._identity,
      'fenced',
      'capture',
      'cleanup_confirmed',
    });
    _validateIdentity(raw);
    _sameFields(raw, identity, _identity);
    if (raw['fenced'] != true) throw _invalid;
    capture = raw['capture'] == null
        ? null
        : LiveCaptureObservation.fromJson(raw['capture'], identity);
    if (capture?.captureOpen == true) throw _invalid;
    cleanupConfirmed = _flag(raw['cleanup_confirmed']);
  }
  late final LiveCaptureObservation? capture;
  late final bool cleanupConfirmed;
}
