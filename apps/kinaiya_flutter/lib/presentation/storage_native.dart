import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'storage.dart';
import 'storage_stub.dart' as fallback;

PresentationStorage createPresentationStorage() =>
    Platform.isAndroid || Platform.isIOS
    ? NativePresentationStorage()
    : fallback.createPresentationStorage();

const _snapshotLimit = 13 * 1024 * 1024;

// Count without first allocating an oversized UTF-8 buffer. Unlike the ordinary
// UTF-8 encoder, reject unpaired UTF-16 rather than replacing it silently.
bool _validSnapshot(String value) {
  if (value.length > _snapshotLimit) return false;
  var bytes = 0;
  for (var index = 0; index < value.length; index++) {
    final unit = value.codeUnitAt(index);
    if (unit < 0x80) {
      bytes++;
    } else if (unit < 0x800) {
      bytes += 2;
    } else if (unit >= 0xd800 && unit <= 0xdbff) {
      if (++index >= value.length) return false;
      final next = value.codeUnitAt(index);
      if (next < 0xdc00 || next > 0xdfff) return false;
      bytes += 4;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return false;
    } else {
      bytes += 3;
    }
    if (bytes > _snapshotLimit) return false;
  }
  return true;
}

PlatformException _failure(String code) => PlatformException(
  code: code,
  message: switch (code) {
    'presentation_invalid' => 'Invalid presentation snapshot.',
    'presentation_busy' => 'Presentation storage is busy.',
    _ => 'Presentation storage is unavailable.',
  },
);

/// A presentation-only channel; the platform selects the private file path.
class NativePresentationStorage implements PresentationStorage {
  NativePresentationStorage({
    MethodChannel channel = const MethodChannel(
      'dev.kinaiya/presentation_storage_v1',
    ),
    @visibleForTesting Duration responseTimeout = const Duration(seconds: 10),
  }) : _channel = channel,
       _responseTimeout = responseTimeout;

  final MethodChannel _channel;
  final Duration _responseTimeout;

  // Share admission/order across controller instances in this isolate, including
  // validation before channel dispatch. A caller timeout does not release a
  // still-running native operation or allow an older write to overtake a new one.
  static Future<void> _tail = Future<void>.value();
  static int _pending = 0;

  @override
  bool get persistent => true;

  Future<T> _enqueue<T>(Future<T> Function() operation) {
    if (_pending >= 2) {
      return Future<T>.error(_failure('presentation_busy'));
    }
    _pending++;
    final result = Completer<T>();
    _tail = _tail.then((_) async {
      try {
        result.complete(await operation());
      } catch (error) {
        final code =
            error is PlatformException &&
                const {
                  'presentation_invalid',
                  'presentation_busy',
                  'presentation_unavailable',
                }.contains(error.code)
            ? error.code
            : 'presentation_unavailable';
        result.completeError(_failure(code));
      } finally {
        _pending--;
      }
    });
    return result.future.timeout(
      _responseTimeout,
      onTimeout: () => throw _failure('presentation_unavailable'),
    );
  }

  Future<void> _validate(String value) async {
    if (!await compute(_validSnapshot, value)) {
      throw _failure('presentation_invalid');
    }
  }

  @override
  Future<String?> read() => _enqueue(() async {
    final value = await _channel.invokeMethod<Object?>('read');
    if (value == null) return null;
    if (value is! String) throw _failure('presentation_invalid');
    await _validate(value);
    return value;
  });

  @override
  Future<void> write(String value) => _enqueue(() async {
    await _validate(value);
    final result = await _channel.invokeMethod<Object?>('write', {
      'snapshot': value,
    });
    if (result != null) throw _failure('presentation_invalid');
  });
}
