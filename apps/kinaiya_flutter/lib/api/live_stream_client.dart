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
       );

  static const _base = '/v2/converse/audio/live-stream';
  final OwnedSettingsTransport _transport;
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

  Future<void> close() => _transport.close();
}
