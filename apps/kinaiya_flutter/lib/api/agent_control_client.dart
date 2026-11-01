/// Session mode transport, sharing actual ownership without sharing authority.
library;

import 'package:http/http.dart' as http;
import 'agent_control.dart';
import 'owned_settings_client.dart';

class AgentControlFailure implements Exception {
  const AgentControlFailure(
    this.message, {
    this.status,
    this.unconfirmed = false,
  });
  final String message;
  final int? status;
  final bool unconfirmed;
  @override
  String toString() => message;
}

class AgentControlAttempt<T> {
  AgentControlAttempt._(this.result, this.settled, this._dispatched);
  final Future<T> result;
  final Future<void> settled;
  final bool Function() _dispatched;
  bool get dispatched => _dispatched();
}

class AgentControlApi {
  AgentControlApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    this.resultTimeout = const Duration(seconds: 10),
    this.cleanupTimeout = const Duration(seconds: 1),
  }) : _transport = OwnedSettingsTransport(
         domain: SettingsDomain.control,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       );

  final OwnedSettingsTransport _transport;
  final Duration resultTimeout, cleanupTimeout;
  String get baseUrl => _transport.baseUrl;
  bool sameOwner(AgentControlApi other) =>
      _transport.sameOwner(other._transport);
  bool get hasCapacity => _transport.hasCapacity;

  AgentControlAttempt<T> _wrap<T>(OwnedSettingsAttempt<T> attempt) =>
      AgentControlAttempt._(
        _result(attempt.result),
        attempt.settled,
        () => attempt.dispatched,
      );

  Future<T> _result<T>(Future<T> result) async {
    try {
      return await result;
    } on OwnedSettingsFailure catch (error) {
      throw AgentControlFailure(
        error.message,
        status: error.status,
        unconfirmed: error.unconfirmed,
      );
    }
  }

  AgentControlAttempt<AgentControlSnapshot> snapshot() => _wrap(
    _transport.start(
      'GET',
      '/v1/agent-control/state',
      null,
      AgentControlSnapshot.fromJson,
    ),
  );

  AgentControlAttempt<AgentModeReceipt> change(AgentModeChange original) =>
      _wrap(
        _transport.start(
          'POST',
          '/v1/agent-control/changes',
          original.body,
          (value) => AgentModeReceipt.fromJson(value, original),
        ),
      );

  AgentControlAttempt<AgentModeLookupResult> lookup(AgentModeChange original) =>
      _wrap(
        _transport.start(
          'POST',
          '/v1/agent-control/changes/lookup',
          original.body,
          (value) => AgentModeLookupResult.fromJson(value, original),
        ),
      );

  Future<void> close() => _transport.close();
}
