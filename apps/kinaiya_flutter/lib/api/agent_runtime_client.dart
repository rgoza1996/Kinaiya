/// Typed execution settings transport; it shares only actual JSON ownership.
library;

import 'dart:async';
import 'package:http/http.dart' as http;
import 'agent_runtime_settings.dart';
import 'owned_settings_client.dart';

class AgentRuntimeFailure implements Exception {
  const AgentRuntimeFailure(
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

class AgentRuntimeAttempt<T> {
  AgentRuntimeAttempt._(this.result, this.settled, this._dispatched);
  final Future<T> result;
  final Future<void> settled;
  final bool Function() _dispatched;
  bool get dispatched => _dispatched();
}

class AgentRuntimeApi {
  AgentRuntimeApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    this.resultTimeout = const Duration(seconds: 10),
    this.cleanupTimeout = const Duration(seconds: 1),
  }) : _transport = OwnedSettingsTransport(
         domain: SettingsDomain.runtime,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       );

  final OwnedSettingsTransport _transport;
  final Duration resultTimeout, cleanupTimeout;
  String get baseUrl => _transport.baseUrl;
  bool sameOwner(AgentRuntimeApi other) =>
      _transport.sameOwner(other._transport);
  bool get hasCapacity => _transport.hasCapacity;

  AgentRuntimeAttempt<T> _wrap<T>(OwnedSettingsAttempt<T> attempt) =>
      AgentRuntimeAttempt._(
        _result(attempt.result),
        attempt.settled,
        () => attempt.dispatched,
      );

  Future<T> _result<T>(Future<T> result) async {
    try {
      return await result;
    } on OwnedSettingsFailure catch (error) {
      throw AgentRuntimeFailure(
        error.message,
        status: error.status,
        unconfirmed: error.unconfirmed,
      );
    }
  }

  AgentRuntimeAttempt<AgentRuntimeReceipt> change(AgentRuntimeChange change) =>
      _wrap(
        _transport.start(
          'POST',
          '/v1/agent/options/runtime',
          change.body,
          (value) => AgentRuntimeReceipt.fromJson(value, change),
        ),
      );

  AgentRuntimeAttempt<AgentRuntimeLookup> lookup(AgentRuntimeChange change) =>
      _wrap(
        _transport.start(
          'POST',
          '/v1/agent/options/runtime-changes/lookup',
          change.body,
          (value) => AgentRuntimeLookup.fromJson(value, change),
        ),
      );

  /// Inspection only. Key-only absence cannot settle an uncertain change.
  AgentRuntimeAttempt<Map<String, Object?>> legacyReceipt(String key) {
    if (!RegExp(r'^[A-Za-z0-9:._-]{1,128}$').hasMatch(key)) {
      return AgentRuntimeAttempt._(
        Future.error(
          const AgentRuntimeFailure('Invalid execution receipt key'),
        ),
        Future<void>.value(),
        () => false,
      );
    }
    return _wrap(
      _transport.start('GET', '/v1/agent/options/runtime-changes/$key', null, (
        value,
      ) {
        if (value is! Map<String, Object?>) throw const FormatException();
        return value;
      }),
    );
  }

  Future<void> close() => _transport.close();
}
