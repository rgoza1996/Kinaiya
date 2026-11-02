/// Codex-authored owner transport; historical saves never grant execution.
library;

import 'package:http/http.dart' as http;
import 'owned_settings_client.dart';
import 'standing_work.dart';

class StandingOwnerFailure implements Exception {
  const StandingOwnerFailure(
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

class StandingOwnerAttempt<T> {
  StandingOwnerAttempt._(this.result, this.settled, this._dispatched);
  final Future<T> result;
  final Future<void> settled;
  final bool Function() _dispatched;
  bool get dispatched => _dispatched();
}

class StandingOwnerApi {
  StandingOwnerApi({
    required String baseUrl,
    required String ownerToken,
    required http.Client Function() clientFactory,
    this.resultTimeout = const Duration(seconds: 10),
    this.cleanupTimeout = const Duration(seconds: 1),
  }) : _transport = OwnedSettingsTransport(
         domain: SettingsDomain.standing,
         baseUrl: baseUrl,
         ownerToken: ownerToken,
         clientFactory: clientFactory,
         resultTimeout: resultTimeout,
         cleanupTimeout: cleanupTimeout,
       );

  final OwnedSettingsTransport _transport;
  final Duration resultTimeout, cleanupTimeout;
  String get baseUrl => _transport.baseUrl;
  bool sameOwner(StandingOwnerApi other) =>
      _transport.sameOwner(other._transport);
  bool get hasCapacity => _transport.hasCapacity;

  StandingOwnerAttempt<T> _wrap<T>(OwnedSettingsAttempt<T> attempt) =>
      StandingOwnerAttempt._(
        _result(attempt.result),
        attempt.settled,
        () => attempt.dispatched,
      );

  Future<T> _result<T>(Future<T> result) async {
    try {
      return await result;
    } on OwnedSettingsFailure catch (error) {
      throw StandingOwnerFailure(
        error.message,
        status: error.status,
        unconfirmed: error.unconfirmed,
      );
    }
  }

  StandingOwnerAttempt<T> _invalid<T>() => StandingOwnerAttempt._(
    Future<T>.error(
      const StandingOwnerFailure('Invalid standing work request'),
    ),
    Future<void>.value(),
    () => false,
  );

  bool _id(String value) =>
      RegExp(
        r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}',
      ).matchAsPrefix(value)?.end ==
      value.length;

  StandingOwnerAttempt<StandingResponsibilitySnapshot> snapshot(
    String responsibilityId,
  ) {
    if (!_id(responsibilityId)) return _invalid();
    return _wrap(
      _transport.start(
        'GET',
        '/v1/standing/responsibilities/$responsibilityId',
        null,
        (value) {
          final snapshot = StandingResponsibilitySnapshot.fromJson(value);
          if (snapshot.responsibilityId != responsibilityId) {
            throw const FormatException('Invalid standing work proof');
          }
          return snapshot;
        },
      ),
    );
  }

  StandingOwnerAttempt<StandingResponsibilityPage> page({
    String? after,
    int limit = 20,
  }) {
    if ((after != null && !_id(after)) || limit < 1 || limit > 20) {
      return _invalid();
    }
    final path = Uri(
      path: '/v1/standing/responsibilities',
      queryParameters: {if (after != null) 'after': after, 'limit': '$limit'},
    ).toString();
    return _wrap(
      _transport.start('GET', path, null, (value) {
        final page = StandingResponsibilityPage.fromJson(value);
        if (page.items.length > limit ||
            (after != null &&
                page.items.any(
                  (record) =>
                      record.receipt.request.responsibilityId.compareTo(
                        after,
                      ) <=
                      0,
                ))) {
          throw const FormatException('Invalid standing work proof');
        }
        return page;
      }),
    );
  }

  StandingOwnerAttempt<StandingChangeReceipt> change(StandingChange original) =>
      _wrap(
        _transport.start(
          'POST',
          '/v1/standing/changes',
          original.body,
          (value) => StandingChangeReceipt.fromJson(value, original: original),
        ),
      );

  StandingOwnerAttempt<StandingChangeLookup> lookup(StandingChange original) =>
      _wrap(
        _transport.start(
          'POST',
          '/v1/standing/changes/lookup',
          original.body,
          (value) => StandingChangeLookup.fromJson(value, original: original),
        ),
      );

  Future<void> close() => _transport.close();
}
