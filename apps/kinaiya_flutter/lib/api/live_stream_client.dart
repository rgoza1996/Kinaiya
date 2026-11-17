import 'package:http/http.dart' as http;

import 'live_streams.dart';
import 'owned_settings_client.dart';

class LiveStreamApi {
  LiveStreamApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    Duration resultTimeout = const Duration(seconds: 10),
    Duration cleanupTimeout = const Duration(seconds: 1),
  }) : _transport = OwnedSettingsTransport(
         domain: SettingsDomain.converseLiveStream,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       ),
       _recoveryFactory = (() => OwnedSettingsTransport(
         domain: SettingsDomain.converseLiveStream,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       ));

  static const _base = '/v2/converse/audio/live-stream';
  final OwnedSettingsTransport _transport;
  final OwnedSettingsTransport Function() _recoveryFactory;
  bool _closed = false;
  bool get hasCapacity => _transport.hasCapacity;
  bool sameOwner(LiveStreamApi other) => _transport.sameOwner(other._transport);
  OwnedSettingsAttempt<LiveStreamCapability> inspect() => _transport.start(
    'GET',
    '$_base/ownership',
    null,
    LiveStreamCapability.fromJson,
  );
  OwnedSettingsAttempt<LiveUtteranceCapability> inspectUtteranceBoundary() =>
      _transport.start(
        'GET',
        '$_base/next-utterance',
        null,
        LiveUtteranceCapability.fromJson,
      );
  OwnedSettingsAttempt<LiveStreamProof> dispatch(
    LiveStreamOperation original,
  ) => _transport.start(
    'POST',
    '$_base/${original.route}',
    original.body,
    (value) => LiveStreamProof.fromJson(value, original),
  );

  /// These endpoints inspect/reconcile metadata; neither resends captured PCM.
  OwnedSettingsAttempt<LiveStreamProof> lookup(LiveStreamOperation original) =>
      _transport.start(
        'POST',
        '$_base/lookup',
        original.lookup,
        (value) => LiveStreamProof.fromJson(value, original),
      );
  OwnedSettingsAttempt<LiveStreamProof> reconcile(
    LiveStreamOperation original,
  ) => _transport.start(
    'POST',
    '$_base/reconcile',
    original.lookup,
    (value) => LiveStreamProof.fromJson(value, original),
  );
  OwnedSettingsAttempt<LiveStreamCancellation> cancel(
    LiveStreamOperation original,
    String reason,
  ) {
    if (!const {
      'owner_off',
      'cancelled',
      'disconnected',
      'replaced',
      'shutdown',
    }.contains(reason)) {
      throw const FormatException('Invalid Adaptive Live cancellation reason');
    }
    return _transport.start('POST', '$_base/cancel', {
      ...original.identity,
      'reason': reason,
    }, (value) => LiveStreamCancellation.fromJson(value, original.identity));
  }

  /// Enroll before start. This original negative/metadata channel remains
  /// independently owned when the normal client is closed or replaced.
  LiveStreamRecoveryApi recoveryFor(LiveStreamOperation initial) {
    if (_closed) throw StateError('Original live stream client is closed');
    LiveStreamAbortOperation(initial, 'cancelled');
    return LiveStreamRecoveryApi._(initial, _recoveryFactory());
  }

  Future<void> close() {
    _closed = true;
    return _transport.close();
  }
}

/// Negative actions and exact metadata only; this channel cannot send PCM,
/// begin capture, finish a decoder or advance an utterance.
class LiveStreamRecoveryApi {
  LiveStreamRecoveryApi._(this.initial, this._transport);
  final LiveStreamOperation initial;
  final OwnedSettingsTransport _transport;
  bool get hasCapacity => _transport.hasCapacity;
  bool sameOwner(LiveStreamApi other) => _transport.sameOwner(other._transport);

  void _check(LiveStreamOperation original) {
    for (final key in [
      'owner_instance',
      'capture_id',
      'session_id',
      'thread_id',
      'epoch',
      'content_scope',
    ]) {
      if (initial.body[key] != original.body[key]) {
        throw const FormatException(
          'Recovery changed its original live capture',
        );
      }
    }
  }

  void _checkAbort(LiveStreamAbortOperation original) {
    if (original.initial.digest != initial.digest) {
      throw const FormatException(
        'Cancellation changed its original initial begin',
      );
    }
  }

  OwnedSettingsAttempt<LiveStreamAbortCapability> inspectCancellation() =>
      _transport.start(
        'GET',
        '${LiveStreamApi._base}/cancel-original',
        null,
        LiveStreamAbortCapability.fromJson,
      );

  OwnedSettingsAttempt<LiveStreamAbortProof> cancelOriginal(
    LiveStreamAbortOperation original,
  ) {
    _checkAbort(original);
    return _transport.start(
      'POST',
      '${LiveStreamApi._base}/cancel-original',
      original.body,
      (value) => LiveStreamAbortProof.fromJson(value, original),
    );
  }

  OwnedSettingsAttempt<LiveStreamAbortProof> lookupCancellation(
    LiveStreamAbortOperation original,
  ) {
    _checkAbort(original);
    return _transport.start(
      'POST',
      '${LiveStreamApi._base}/cancel-original/lookup',
      original.body,
      (value) => LiveStreamAbortProof.fromJson(value, original),
    );
  }

  OwnedSettingsAttempt<LiveStreamProof> lookup(LiveStreamOperation original) {
    _check(original);
    return _transport.start(
      'POST',
      '${LiveStreamApi._base}/lookup',
      original.lookup,
      (value) => LiveStreamProof.fromJson(value, original),
    );
  }

  OwnedSettingsAttempt<LiveStreamProof> reconcile(
    LiveStreamOperation original,
  ) {
    _check(original);
    return _transport.start(
      'POST',
      '${LiveStreamApi._base}/reconcile',
      original.lookup,
      (value) => LiveStreamProof.fromJson(value, original),
    );
  }

  Future<void> close() => _transport.close();
}
