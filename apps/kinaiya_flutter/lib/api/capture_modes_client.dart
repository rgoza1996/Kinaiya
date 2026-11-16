import 'package:http/http.dart' as http;

import 'capture_modes.dart';
import 'owned_settings_client.dart';

class CaptureModeApi {
  CaptureModeApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    Duration resultTimeout = const Duration(seconds: 10),
    Duration cleanupTimeout = const Duration(seconds: 1),
  }) : _transport = OwnedSettingsTransport(
         domain: SettingsDomain.converseCapture,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       );

  final OwnedSettingsTransport _transport;
  bool get hasCapacity => _transport.hasCapacity;
  bool sameOwner(CaptureModeApi other) =>
      _transport.sameOwner(other._transport);
  OwnedSettingsAttempt<CaptureModeSnapshot> inspect() => _transport.start(
    'GET',
    '/v1/converse/options/capture-mode',
    null,
    CaptureModeSnapshot.fromJson,
  );
  OwnedSettingsAttempt<CaptureModeReceipt> change(CaptureModeChange original) =>
      _transport.start(
        'POST',
        '/v1/converse/options/capture-mode',
        original.body,
        (value) => CaptureModeReceipt.fromJson(value, original),
      );
  OwnedSettingsAttempt<CaptureModeLookup> lookup(CaptureModeChange original) =>
      _transport.start(
        'POST',
        '/v1/converse/options/capture-mode-changes/lookup',
        original.body,
        (value) => CaptureModeLookup.fromJson(value, original),
      );
  Future<void> close() => _transport.close();
}
