// Independent Codex regressions after reviewing v1, frozen before its read-binding fix.
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/standing_work.dart';
import 'package:kinaiya_flutter/api/standing_work_client.dart';

typedef Json = Map<String, Object?>;

late Json fixture;
const _base = 'https://standing-read-binding.invalid/session';
const _owner = 'synthetic-standing-read-owner';
const _settlementBound = Duration(seconds: 3);

Json _object(Object? value) => (value as Map).cast<String, Object?>();
Json _copy(Object? value) => _object(jsonDecode(jsonEncode(value)));

Object? _ordered(Object? value) {
  if (value is Map) {
    final object = _object(value);
    final keys = object.keys.toList()..sort();
    return {for (final key in keys) key: _ordered(object[key])};
  }
  if (value is List) return value.map(_ordered).toList();
  return value;
}

String _digest(String domain, Object? value) => sha256.convert([
  ...utf8.encode(domain),
  0,
  ...utf8.encode(jsonEncode(_ordered(value))),
]).toString();

String _id(int number) =>
    '00000000-0000-0000-0000-${number.toRadixString(16).padLeft(12, '0')}';

Json _record(int number) {
  final record = _copy(fixture['record']);
  final receipt = _object(record['receipt']);
  final request = _object(receipt['request']);
  request['responsibility_id'] = _id(number);
  request['idempotency_key'] = 'independent-read-binding-$number';
  receipt['authorization_digest'] =
      _digest('kinaiya.standing.authorization.v1', {
        'responsibility_id': request['responsibility_id'],
        'revision': receipt['revision'],
        'request': request,
      });
  record['record_digest'] = _digest('kinaiya.standing.record.v1', {
    'receipt': receipt,
    'activation': record['activation'],
    'authorized_from_utc': record['authorized_from_utc'],
  });
  return record;
}

Json _snapshot(int number) => _copy(fixture['snapshot'])
  ..['responsibility_id'] = _id(number)
  ..['record'] = _record(number);

Json _page(List<int> numbers) => _copy(fixture['page'])
  ..['items'] = numbers.map(_record).toList()
  ..['next_cursor'] = null;

class _OwnedMockClient extends MockClient {
  _OwnedMockClient(super.handler);
  int closes = 0;

  @override
  void close() {
    closes++;
    super.close();
  }
}

Future<void> _refusesCallerMismatch<T>({
  required Json response,
  required String expectedPath,
  required Map<String, String> expectedQuery,
  required StandingOwnerAttempt<T> Function(StandingOwnerApi) operation,
}) async {
  var sent = 0;
  final client = _OwnedMockClient((request) async {
    sent++;
    expect(request.method, 'GET');
    expect(request.url.origin, 'https://standing-read-binding.invalid');
    expect(request.url.path, expectedPath);
    expect(request.url.queryParameters, expectedQuery);
    expect(request.followRedirects, isFalse);
    expect(request.headers['Authorization'], 'Bearer $_owner');
    return http.Response(
      jsonEncode(response),
      200,
      headers: {'content-type': 'application/json; charset=utf-8'},
    );
  });
  final api = StandingOwnerApi(
    baseUrl: _base,
    ownerToken: _owner,
    clientFactory: () => client,
  );
  final attempt = operation(api);
  try {
    await expectLater(
      attempt.result,
      throwsA(
        isA<StandingOwnerFailure>().having(
          (error) => error.unconfirmed,
          'actual dispatched read has no caller-bound proof',
          isTrue,
        ),
      ),
    );
  } finally {
    await attempt.settled.timeout(_settlementBound);
    await api.close();
    expect(attempt.dispatched, isTrue);
    expect(sent, 1);
    expect(client.closes, 1);
  }
}

void main() {
  setUpAll(() {
    fixture = _object(
      jsonDecode(
        File(
          'test/fixtures/standing-owner-independent-v1.json',
        ).readAsStringSync(),
      ),
    );
  });

  test(
    'snapshot refuses valid proof for another requested responsibility',
    () async {
      final response = _snapshot(2);
      // The ordinary DTO is internally valid. Only the requested ID differs.
      expect(
        StandingResponsibilitySnapshot.fromJson(response).responsibilityId,
        _id(2),
      );
      await _refusesCallerMismatch(
        response: response,
        expectedPath: '/session/v1/standing/responsibilities/${_id(1)}',
        expectedQuery: {},
        operation: (api) => api.snapshot(_id(1)),
      );
    },
  );

  test(
    'page refuses a valid record equal to the exclusive after cursor',
    () async {
      final response = _page([2]);
      expect(
        StandingResponsibilityPage.fromJson(
          response,
        ).items.single.receipt.request.responsibilityId,
        _id(2),
      );
      await _refusesCallerMismatch(
        response: response,
        expectedPath: '/session/v1/standing/responsibilities',
        expectedQuery: {'after': _id(2), 'limit': '20'},
        operation: (api) => api.page(after: _id(2)),
      );
    },
  );

  test(
    'page refuses a valid record lower than the requested after cursor',
    () async {
      final response = _page([1]);
      expect(
        StandingResponsibilityPage.fromJson(
          response,
        ).items.single.receipt.request.responsibilityId,
        _id(1),
      );
      await _refusesCallerMismatch(
        response: response,
        expectedPath: '/session/v1/standing/responsibilities',
        expectedQuery: {'after': _id(2), 'limit': '20'},
        operation: (api) => api.page(after: _id(2)),
      );
    },
  );

  test(
    'page refuses internally ordered proof above the requested limit',
    () async {
      final response = _page([1, 2]);
      expect(StandingResponsibilityPage.fromJson(response).items.length, 2);
      await _refusesCallerMismatch(
        response: response,
        expectedPath: '/session/v1/standing/responsibilities',
        expectedQuery: {'limit': '1'},
        operation: (api) => api.page(limit: 1),
      );
    },
  );

  test(
    'matching snapshot and bounded exclusive page still return proof',
    () async {
      var sent = 0;
      final clients = <_OwnedMockClient>[];
      final api = StandingOwnerApi(
        baseUrl: _base,
        ownerToken: _owner,
        clientFactory: () {
          final client = _OwnedMockClient((request) async {
            sent++;
            expect(request.method, 'GET');
            expect(request.url.origin, 'https://standing-read-binding.invalid');
            expect(request.followRedirects, isFalse);
            if (request.url.path.endsWith('/${_id(2)}')) {
              expect(request.url.queryParameters, isEmpty);
              return http.Response(
                jsonEncode(_snapshot(2)),
                200,
                headers: {'content-type': 'application/json'},
              );
            }
            expect(request.url.path, '/session/v1/standing/responsibilities');
            expect(request.url.queryParameters, {
              'after': _id(1),
              'limit': '1',
            });
            return http.Response(
              jsonEncode(_page([2])),
              200,
              headers: {'content-type': 'application/json'},
            );
          });
          clients.add(client);
          return client;
        },
      );
      try {
        final detail = api.snapshot(_id(2));
        expect((await detail.result).responsibilityId, _id(2));
        await detail.settled.timeout(_settlementBound);
        expect(detail.dispatched, isTrue);
        final page = api.page(after: _id(1), limit: 1);
        expect(
          (await page.result).items.single.receipt.request.responsibilityId,
          _id(2),
        );
        await page.settled.timeout(_settlementBound);
        expect(page.dispatched, isTrue);
      } finally {
        await api.close();
      }
      expect(sent, 2);
      expect(clients.length, 2);
      expect(clients.every((client) => client.closes == 1), isTrue);
    },
  );
}
