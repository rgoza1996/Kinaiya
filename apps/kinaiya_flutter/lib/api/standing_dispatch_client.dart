/// Codex-authored owner transport; no Agent renewal or automatic dispatch.
library;

import 'package:http/http.dart' as http;
import 'owned_settings_client.dart';
import 'standing_dispatch.dart';

class StandingDispatchOwnerFailure implements Exception {
  const StandingDispatchOwnerFailure(
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

class StandingDispatchOwnerAttempt<T> {
  StandingDispatchOwnerAttempt._(this.result, this.settled, this._dispatched);
  final Future<T> result;
  final Future<void> settled;
  final bool Function() _dispatched;
  bool get dispatched => _dispatched();
}

class StandingDispatchOwnerApi {
  StandingDispatchOwnerApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    this.resultTimeout = const Duration(seconds: 10),
    this.cleanupTimeout = const Duration(seconds: 1),
  }) : _transport = OwnedSettingsTransport(
         domain: SettingsDomain.standingDispatch,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       );

  final OwnedSettingsTransport _transport;
  final Duration resultTimeout, cleanupTimeout;
  String get baseUrl => _transport.baseUrl;
  bool get hasCapacity => _transport.hasCapacity;
  bool sameOwner(StandingDispatchOwnerApi other) =>
      _transport.sameOwner(other._transport);

  StandingDispatchOwnerAttempt<T> _wrap<T>(OwnedSettingsAttempt<T> attempt) =>
      StandingDispatchOwnerAttempt._(
        _result(attempt.result),
        attempt.settled,
        () => attempt.dispatched,
      );

  Future<T> _result<T>(Future<T> result) async {
    try {
      return await result;
    } on OwnedSettingsFailure catch (error) {
      throw StandingDispatchOwnerFailure(
        error.message,
        status: error.status,
        unconfirmed: error.unconfirmed,
      );
    }
  }

  StandingDispatchOwnerAttempt<StandingDispatchState> snapshot() => _wrap(
    _transport.start(
      'GET',
      '/v1/standing/dispatch',
      null,
      StandingDispatchState.fromJson,
    ),
  );

  StandingDispatchOwnerAttempt<StandingDispatchStop> stopContext() => _wrap(
    _transport.start(
      'GET',
      '/v1/standing/dispatch/stop-context',
      null,
      StandingDispatchStop.fromJson,
    ),
  );

  StandingDispatchOwnerAttempt<StandingDispatchActivationReceipt> activate(
    StandingDispatchActivation original,
  ) => _wrap(
    _transport.start(
      'POST',
      '/v1/standing/dispatch/activate',
      original.body,
      (value) =>
          StandingDispatchActivationReceipt.fromJson(value, original: original),
    ),
  );

  StandingDispatchOwnerAttempt<StandingDispatchActivationLookup> lookup(
    StandingDispatchActivation original,
  ) => _wrap(
    _transport.start(
      'POST',
      '/v1/standing/dispatch/activate/lookup',
      original.body,
      (value) =>
          StandingDispatchActivationLookup.fromJson(value, original: original),
    ),
  );

  StandingDispatchOwnerAttempt<StandingDispatchStopProof> stop(
    StandingDispatchStop original,
  ) => _wrap(
    _transport.start(
      'POST',
      '/v1/standing/dispatch/stop',
      original.body,
      (value) => StandingDispatchStopProof.fromJson(value, original: original),
    ),
  );

  Future<void> close() => _transport.close();
}
