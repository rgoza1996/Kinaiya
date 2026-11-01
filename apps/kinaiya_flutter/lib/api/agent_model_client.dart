/// Bound model adapter. Shared transport ownership does not merge authorities.
library;

import 'dart:async';
import 'package:http/http.dart' as http;
import 'agent_model_settings.dart';
import 'owned_settings_client.dart';

class AgentModelFailure implements Exception {
  const AgentModelFailure(
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

class AgentModelAttempt<T> {
  AgentModelAttempt._(this.result, this.settled, this._dispatched);
  final Future<T> result;
  final Future<void> settled;
  final bool Function() _dispatched;
  bool get dispatched => _dispatched();
}

class AgentModelApi {
  AgentModelApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    this.resultTimeout = const Duration(seconds: 10),
    this.cleanupTimeout = const Duration(seconds: 1),
  }) : _transport = OwnedSettingsTransport(
         domain: SettingsDomain.model,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       );
  final OwnedSettingsTransport _transport;
  final Duration resultTimeout, cleanupTimeout;
  String get baseUrl => _transport.baseUrl;
  bool sameOwner(AgentModelApi other) => _transport.sameOwner(other._transport);
  bool get hasCapacity => _transport.hasCapacity;

  AgentModelAttempt<T> _wrap<T>(OwnedSettingsAttempt<T> attempt) =>
      AgentModelAttempt._(
        _result(attempt.result),
        attempt.settled,
        () => attempt.dispatched,
      );
  Future<T> _result<T>(Future<T> result) async {
    try {
      return await result;
    } on OwnedSettingsFailure catch (error) {
      throw AgentModelFailure(
        error.message,
        status: error.status,
        unconfirmed: error.unconfirmed,
      );
    }
  }

  AgentModelAttempt<AgentModelReceipt> change(AgentModelChange request) =>
      _wrap(
        _transport.start(
          'POST',
          '/v1/agent/options/models',
          request.body,
          (raw) => AgentModelReceipt.fromJson(raw, request),
        ),
      );
  AgentModelAttempt<AgentModelLookup> lookup(AgentModelChange request) => _wrap(
    _transport.start(
      'POST',
      '/v1/agent/options/model-changes/lookup',
      request.body,
      (raw) => AgentModelLookup.fromJson(raw, request),
    ),
  );
  AgentModelAttempt<Map<String, Object?>> legacyReceipt(String key) {
    if (!RegExp(r'^[A-Za-z0-9:._-]{1,128}$').hasMatch(key)) {
      return AgentModelAttempt._(
        Future.error(const AgentModelFailure('Invalid model receipt key')),
        Future.value(),
        () => false,
      );
    }
    return _wrap(
      _transport.start(
        'GET',
        '/v1/agent/options/model-changes/${Uri.encodeComponent(key)}',
        null,
        (raw) {
          if (raw is! Map<String, Object?>) throw const FormatException();
          return raw;
        },
      ),
    );
  }

  Future<void> close() => _transport.close();
}
