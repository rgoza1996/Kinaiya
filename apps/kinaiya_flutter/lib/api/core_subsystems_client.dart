import 'package:http/http.dart' as http;

import 'core_subsystems.dart';
import 'owned_settings_client.dart';

class CoreSubsystemApi {
  CoreSubsystemApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    Duration resultTimeout = const Duration(seconds: 10),
    Duration cleanupTimeout = const Duration(seconds: 1),
  }) : _transport = OwnedSettingsTransport(
         domain: SettingsDomain.coreSubsystems,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       );

  final OwnedSettingsTransport _transport;
  bool get hasCapacity => _transport.hasCapacity;
  bool sameOwner(CoreSubsystemApi other) =>
      _transport.sameOwner(other._transport);
  OwnedSettingsAttempt<CoreSubsystemSnapshot> inspect() => _transport.start(
    'GET',
    '/v1/core/options/subsystems',
    null,
    CoreSubsystemSnapshot.fromJson,
  );
  OwnedSettingsAttempt<CoreSubsystemReceipt> change(
    CoreSubsystemChange original,
  ) => _transport.start(
    'POST',
    '/v1/core/options/subsystems',
    original.body,
    (value) => CoreSubsystemReceipt.fromJson(value, original),
  );
  OwnedSettingsAttempt<CoreSubsystemLookup> lookup(
    CoreSubsystemChange original,
  ) => _transport.start(
    'POST',
    '/v1/core/options/subsystem-changes/lookup',
    original.body,
    (value) => CoreSubsystemLookup.fromJson(value, original),
  );
  Future<void> close() => _transport.close();
}
