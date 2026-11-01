// Codex independent transport oracles, frozen before standing client implementation review.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/agent_control_client.dart';
import 'package:kinaiya_flutter/api/owned_settings_client.dart';
import 'package:kinaiya_flutter/api/standing_work.dart';
import 'package:kinaiya_flutter/api/standing_work_client.dart';

typedef Json = Map<String, Object?>;

const _token = 'synthetic-standing-owner-token';
const _base = 'https://standing-host.invalid/session';
const _testBound = Duration(seconds: 3);
late Json _fixtures;

Json _copy(Object? value) => jsonDecode(jsonEncode(value)) as Json;
Json _fixture(String name) => _copy(_fixtures[name]);
StandingChange _original() => StandingChange.fromJson(_fixture('request'));

Object? _sorted(Object? value) {
  if (value is Map<String, Object?>) {
    final keys = value.keys.toList()..sort();
    return {for (final key in keys) key: _sorted(value[key])};
  }
  if (value is List) return value.map(_sorted).toList();
  return value;
}

List<int> _canonical(Object? value) => utf8.encode(jsonEncode(_sorted(value)));
String _digest(String domain, Object? value) => sha256.convert([
  ...utf8.encode('$domain\u0000'),
  ..._canonical(value),
]).toString();

Json _receiptFor(Json request) => {
  'schema_version': '1.0',
  'status': 'responsibility_saved',
  'backend_id': request['expected_backend'],
  'service_instance': request['expected_instance'],
  'revision': (request['expected_revision'] as int) + 1,
  'request': _copy(request),
  'authorization_digest': _digest('kinaiya.standing.authorization.v1', {
    'responsibility_id': request['responsibility_id'],
    'revision': (request['expected_revision'] as int) + 1,
    'request': request,
  }),
};

Json _lookupFor(Json request) => {
  'schema_version': '1.0',
  'status': 'recorded',
  'request': _copy(request),
  'current': _fixture('current'),
  'request_instance': 'current',
  'receipt': _receiptFor(request),
  'mutation_fenced': false,
};

class _Client extends http.BaseClient {
  _Client(this.handler);
  final FutureOr<http.StreamedResponse> Function(http.BaseRequest) handler;
  final sent = <http.BaseRequest>[];
  int closes = 0;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    sent.add(request);
    return Future<http.StreamedResponse>.sync(() => handler(request));
  }

  @override
  void close() {
    closes++;
  }
}

http.StreamedResponse _raw(
  List<int> bytes, {
  int status = 200,
  Map<String, String>? headers,
  int? length,
  bool split = false,
}) => http.StreamedResponse(
  Stream<List<int>>.fromIterable(
    split
        ? [
            bytes.sublist(0, bytes.length ~/ 2),
            bytes.sublist(bytes.length ~/ 2),
          ]
        : [bytes],
  ),
  status,
  headers: headers ?? {'content-type': 'application/json; charset=utf-8'},
  contentLength: length,
);

http.StreamedResponse _wire(Object? body, {int status = 200}) =>
    _raw(utf8.encode(jsonEncode(body)), status: status);

StandingOwnerApi _api(
  http.Client Function() factory, {
  String base = _base,
  String token = _token,
  Duration timeout = const Duration(seconds: 10),
}) => StandingOwnerApi(
  baseUrl: base,
  ownerToken: token,
  clientFactory: factory,
  resultTimeout: timeout,
  cleanupTimeout: const Duration(milliseconds: 10),
);

Json _controlState() {
  final current = _fixture('current');
  return {
    'schema_version': '1.0',
    'backend_id': current['backend_id'],
    'instance_id': current['instance_id'],
    'revision': 0,
    'mode': 'on_demand',
    'active_work_policy': 'finish_already_accepted',
    'control_writable': true,
    'available_modes': ['off', 'on_demand'],
    'standing_work_available': false,
  };
}

void main() {
  setUpAll(() {
    _fixtures = _copy(
      jsonDecode(
        File('test/fixtures/standing-owner-independent-v1.json')
            .readAsStringSync(),
      ),
    );
    final request = _fixture('request');
    expect(
      sha256.convert(_canonical(request)).toString(),
      _fixtures['expected_request_sha256'],
    );
    expect(_receiptFor(request), _fixture('receipt'));
  });

  test('four owner routes preserve service root, original body, bearer and no redirects', () async {
    final clients = <_Client>[];
    final requests = <http.BaseRequest>[];
    final original = _original();
    const after = '00000000-0000-0000-0000-000000000000';
    final api = _api(() {
      final client = _Client((request) {
        requests.add(request);
        expect(request.url.origin, 'https://standing-host.invalid');
        expect(request.followRedirects, isFalse);
        expect(request.headers['Authorization'], 'Bearer $_token');
        expect(request.headers['Accept'], 'application/json');
        expect(request.headers['Accept-Encoding'], 'identity');
        if (request.method == 'GET') {
          expect((request as http.Request).bodyBytes, isEmpty);
          if (request.url.path.endsWith('/responsibilities')) {
            expect(request.url.queryParameters, {'after': after, 'limit': '7'});
            return _wire(_fixture('page'));
          }
          expect(request.url.query, isEmpty);
          return _wire(_fixture('snapshot'));
        }
        expect(request.method, 'POST');
        expect(request.headers['Content-Type'], 'application/json');
        expect((request as http.Request).bodyBytes, original.canonicalBytes);
        return _wire(
          _fixture(request.url.path.endsWith('/lookup') ? 'lookup' : 'receipt'),
        );
      });
      clients.add(client);
      return client;
    }, base: '$_base/');
    try {
      expect(api.baseUrl, _base);
      final detail = api.snapshot(original.responsibilityId);
      expect((await detail.result).json, _fixture('snapshot'));
      expect(detail.dispatched, isTrue);
      await detail.settled.timeout(_testBound);
      final page = api.page(after: after, limit: 7);
      expect((await page.result).json, _fixture('page'));
      await page.settled.timeout(_testBound);
      final change = api.change(original);
      expect((await change.result).request.matches(original), isTrue);
      await change.settled.timeout(_testBound);
      final lookup = api.lookup(original);
      expect((await lookup.result).receipt!.request.matches(original), isTrue);
      await lookup.settled.timeout(_testBound);
      expect(requests.map((request) => request.url.path).toList(), [
        '/session/v1/standing/responsibilities/${original.responsibilityId}',
        '/session/v1/standing/responsibilities',
        '/session/v1/standing/changes',
        '/session/v1/standing/changes/lookup',
      ]);
      expect(clients.length, 4);
      expect(
        clients.every(
          (client) => client.sent.length == 1 && client.closes == 1,
        ),
        isTrue,
      );
    } finally {
      await api.close();
    }
  });

  test(
    'legitimate 97KB request and two-copy 195KB proof use standing limits',
    () async {
      final raw = _fixture('request');
      (raw['authorization'] as Json)['objective'] = List.filled(
        16000,
        '\u0000',
      ).join();
      final requestBytes = _canonical(raw);
      final lookupBody = _lookupFor(raw);
      expect(requestBytes.length, inInclusiveRange(96000, 100000));
      expect(_canonical(lookupBody).length, inInclusiveRange(190000, 205000));
      final original = StandingChange.fromJson(raw);
      final clients = <_Client>[];
      final api = _api(() {
        final client = _Client((request) {
          final wireRequest = request as http.Request;
          expect(wireRequest.bodyBytes, requestBytes);
          expect(wireRequest.bodyBytes.length, lessThanOrEqualTo(131072));
          return _raw(
            _canonical(
              request.url.path.endsWith('/lookup')
                  ? lookupBody
                  : _receiptFor(raw),
            ),
            split: true,
          );
        });
        clients.add(client);
        return client;
      });
      try {
        final save = api.change(original);
        expect((await save.result).request.matches(original), isTrue);
        await save.settled.timeout(_testBound);
        final proof = api.lookup(original);
        expect((await proof.result).json, lookupBody);
        await proof.settled.timeout(_testBound);
        expect(clients.length, 2);
        expect(
          clients.every(
            (client) => client.sent.length == 1 && client.closes == 1,
          ),
          isTrue,
        );
      } finally {
        await api.close();
      }
    },
  );

  test('malformed, encoded, oversized and refused responses remain private and unconfirmed', () async {
    final good = jsonEncode(_fixture('receipt'));
    final cases = <(String, http.StreamedResponse Function(), int?)>[
      (
        'duplicate key',
        () => _raw(
          utf8.encode(
            good.replaceFirst('"revision":1', '"revision":1,"revision":1'),
          ),
        ),
        null,
      ),
      (
        'wrong media',
        () => _raw(utf8.encode(good), headers: {'content-type': 'text/plain'}),
        null,
      ),
      (
        'compressed',
        () => _raw(
          utf8.encode(good),
          headers: {
            'content-type': 'application/json',
            'content-encoding': 'gzip',
          },
        ),
        null,
      ),
      (
        'oversized actual chunks',
        () => _raw(
          utf8.encode('$good${List.filled(262145, ' ').join()}'),
          split: true,
        ),
        null,
      ),
      (
        'oversized declared length',
        () => _raw(utf8.encode(good), length: 262145),
        null,
      ),
      (
        'conflict body',
        () => _wire({'PRIVATE': 'PRIVATE $_token $_base'}, status: 409),
        409,
      ),
      (
        'unavailable body',
        () => _wire({'PRIVATE': 'PRIVATE $_token $_base'}, status: 503),
        503,
      ),
      (
        'redirect',
        () => _raw(
          utf8.encode('PRIVATE REDIRECT'),
          status: 302,
          headers: {'location': 'https://foreign.invalid'},
        ),
        302,
      ),
    ];
    for (final (name, response, status) in cases) {
      final client = _Client((_) => response());
      final api = _api(() => client);
      try {
        final attempt = api.change(_original());
        await expectLater(
          attempt.result,
          throwsA(
            isA<StandingOwnerFailure>()
                .having(
                  (error) => error.unconfirmed,
                  '$name dispatched uncertainty',
                  isTrue,
                )
                .having((error) => error.status, '$name status', status)
                .having(
                  (error) => error.toString(),
                  'private body',
                  isNot(contains('PRIVATE')),
                )
                .having(
                  (error) => error.toString(),
                  'bearer',
                  isNot(contains(_token)),
                )
                .having(
                  (error) => error.toString(),
                  'endpoint',
                  isNot(contains(_base)),
                ),
          ),
        );
        await attempt.settled.timeout(_testBound);
        expect(attempt.dispatched, isTrue);
        expect(client.sent.length, 1);
        expect(client.closes, 1);
      } finally {
        await api.close();
      }
    }
  });

  test(
    'transport refuses internally valid proof for a different full original',
    () async {
      final original = _original();
      final other = _fixture('request');
      (other['authorization'] as Json)['objective'] =
          'Different literal synthetic owner objective.';
      final otherReceipt = _receiptFor(other);
      final otherLookup = _lookupFor(other);
      // Digest and every internal identity remain valid; only caller-held original differs.
      expect(
        StandingChangeReceipt.fromJson(otherReceipt).request.matches(original),
        isFalse,
      );
      expect(
        StandingChangeLookup.fromJson(otherLookup).request.matches(original),
        isFalse,
      );
      final clients = <_Client>[];
      final api = _api(() {
        final client = _Client(
          (request) => _wire(
            request.url.path.endsWith('/lookup') ? otherLookup : otherReceipt,
          ),
        );
        clients.add(client);
        return client;
      });
      try {
        final save = api.change(original);
        await expectLater(
          save.result,
          throwsA(
            isA<StandingOwnerFailure>().having(
              (error) => error.unconfirmed,
              'save uncertainty',
              isTrue,
            ),
          ),
        );
        await save.settled.timeout(_testBound);
        final lookup = api.lookup(original);
        await expectLater(
          lookup.result,
          throwsA(
            isA<StandingOwnerFailure>().having(
              (error) => error.unconfirmed,
              'lookup uncertainty',
              isTrue,
            ),
          ),
        );
        await lookup.settled.timeout(_testBound);
        expect(clients.length, 2);
        expect(
          clients.every(
            (client) => client.sent.length == 1 && client.closes == 1,
          ),
          isTrue,
        );
      } finally {
        await api.close();
      }
    },
  );

  test('private owner scope survives normalization and a closed facade creates no IO', () async {
    var created = 0;
    http.Client factory() {
      created++;
      return _Client((_) => _wire(_fixture('snapshot')));
    }

    final first = _api(factory, base: '$_base/');
    final same = _api(factory);
    final otherOwner = _api(factory, token: 'other-private-synthetic-owner');
    final otherRoot = _api(factory, base: 'https://other-host.invalid/session');
    try {
      expect(first.sameOwner(same), isTrue);
      expect(first.sameOwner(otherOwner), isFalse);
      expect(first.sameOwner(otherRoot), isFalse);
      await first.close();
      final refused = first.snapshot(_original().responsibilityId);
      await expectLater(
        refused.result,
        throwsA(
          isA<StandingOwnerFailure>().having(
            (error) => error.unconfirmed,
            'not dispatched',
            isFalse,
          ),
        ),
      );
      await refused.settled.timeout(_testBound);
      expect(refused.dispatched, isFalse);
      expect(first.hasCapacity, isFalse);
      expect(same.hasCapacity, isTrue);
      expect(created, 0);
    } finally {
      await Future.wait([
        first.close(),
        same.close(),
        otherOwner.close(),
        otherRoot.close(),
      ]);
    }
  });

  test('32 actual owners mix old control and standing, including late response cancellation', () async {
    final pending = <Completer<http.StreamedResponse>>[];
    final clients = <_Client>[];
    final checks = <Future<void>>[];
    final settlements = <Future<void>>[];
    final settled = List.filled(32, false);
    final lateCancellation = Completer<void>();
    final lateCancelStarted = Completer<void>();
    final lateStream = StreamController<List<int>>(
      onCancel: () {
        if (!lateCancelStarted.isCompleted) lateCancelStarted.complete();
        return lateCancellation.future;
      },
    );
    http.Client heldFactory() {
      final response = Completer<http.StreamedResponse>();
      pending.add(response);
      final client = _Client((_) => response.future);
      clients.add(client);
      return client;
    }

    final control = AgentControlApi(
      baseUrl: _base,
      ownerToken: _token,
      clientFactory: heldFactory,
      resultTimeout: const Duration(milliseconds: 60),
      cleanupTimeout: const Duration(milliseconds: 10),
    );
    final standing = _api(
      heldFactory,
      timeout: const Duration(milliseconds: 60),
    );
    final contenderClients = <_Client>[];
    final contender = _api(() {
      final client = _Client((_) => _wire(_fixture('snapshot')));
      contenderClients.add(client);
      return client;
    });
    var oldResponseReleased = false;
    try {
      final old = control.snapshot();
      settlements.add(old.settled);
      unawaited(old.settled.then((_) => settled[0] = true));
      checks.add(
        expectLater(
          old.result,
          throwsA(
            isA<AgentControlFailure>().having(
              (error) => error.unconfirmed,
              'old native owner',
              isTrue,
            ),
          ),
        ),
      );
      for (var index = 1; index < 32; index++) {
        final attempt = standing.snapshot(_original().responsibilityId);
        settlements.add(attempt.settled);
        final slot = index;
        unawaited(attempt.settled.then((_) => settled[slot] = true));
        checks.add(
          expectLater(
            attempt.result,
            throwsA(
              isA<StandingOwnerFailure>().having(
                (error) => error.unconfirmed,
                'standing native owner',
                isTrue,
              ),
            ),
          ),
        );
      }
      expect(pending.length, 32);
      expect(clients.every((client) => client.sent.length == 1), isTrue);
      await Future.wait(checks).timeout(_testBound);
      await Future.wait([control.close(), standing.close()])
          .timeout(_testBound);
      expect(settled.any((value) => value), isFalse);
      expect(contender.hasCapacity, isFalse);
      final refused = contender.snapshot(_original().responsibilityId);
      await expectLater(
        refused.result,
        throwsA(
          isA<StandingOwnerFailure>().having(
            (error) => error.unconfirmed,
            'no dispatch at cap',
            isFalse,
          ),
        ),
      );
      await refused.settled.timeout(_testBound);
      expect(refused.dispatched, isFalse);
      expect(contenderClients, isEmpty);
      pending[0].complete(
        http.StreamedResponse(
          lateStream.stream,
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      oldResponseReleased = true;
      await lateCancelStarted.future.timeout(_testBound);
      expect(settled[0], isFalse);
      expect(contender.hasCapacity, isFalse);
      lateCancellation.complete();
      await old.settled.timeout(_testBound);
      expect(settled[0], isTrue);
      expect(contender.hasCapacity, isTrue);
      final accepted = contender.snapshot(_original().responsibilityId);
      expect((await accepted.result).json, _fixture('snapshot'));
      await accepted.settled.timeout(_testBound);
      expect(accepted.dispatched, isTrue);
      expect(contenderClients.single.sent.length, 1);
      expect(contenderClients.single.closes, 1);
    } finally {
      if (pending.isNotEmpty && !oldResponseReleased) {
        pending[0].complete(
          http.StreamedResponse(
            lateStream.stream,
            200,
            headers: {'content-type': 'application/json'},
          ),
        );
      }
      if (!lateCancellation.isCompleted) lateCancellation.complete();
      for (var index = 1; index < pending.length; index++) {
        if (!pending[index].isCompleted)
          pending[index].complete(_wire(_fixture('snapshot')));
      }
      try {
        await Future.wait<void>([...checks, ...settlements])
            .timeout(_testBound);
      } finally {
        await Future.wait([
          control.close(),
          standing.close(),
          contender.close(),
        ]).timeout(_testBound);
        await lateStream.close().timeout(_testBound);
      }
    }
    expect(settled.every((value) => value), isTrue);
    expect(
      clients.every((client) => client.sent.length == 1 && client.closes == 1),
      isTrue,
    );
  });

  test('an already-reading stream stays owned after public deadline and facade close', () async {
    final listened = Completer<void>();
    final cancelStarted = Completer<void>();
    final cancellation = Completer<void>();
    final stream = StreamController<List<int>>(
      onListen: () => listened.complete(),
      onCancel: () {
        if (!cancelStarted.isCompleted) cancelStarted.complete();
        return cancellation.future;
      },
    );
    final client = _Client(
      (_) => http.StreamedResponse(
        stream.stream,
        200,
        headers: {'content-type': 'application/json'},
      ),
    );
    final api = _api(() => client, timeout: const Duration(milliseconds: 60));
    final attempt = api.lookup(_original());
    var settled = false;
    unawaited(attempt.settled.then((_) => settled = true));
    final publicResult = expectLater(
      attempt.result,
      throwsA(
        isA<StandingOwnerFailure>().having(
          (error) => error.unconfirmed,
          'read uncertainty',
          isTrue,
        ),
      ),
    );
    try {
      await listened.future.timeout(_testBound);
      await publicResult.timeout(_testBound);
      await api.close().timeout(_testBound);
      await cancelStarted.future.timeout(_testBound);
      expect(attempt.dispatched, isTrue);
      expect(settled, isFalse);
      expect(client.sent.length, 1);
      expect(client.closes, 1);
      cancellation.complete();
      await attempt.settled.timeout(_testBound);
      expect(settled, isTrue);
      expect(client.closes, 1);
    } finally {
      if (!cancellation.isCompleted) cancellation.complete();
      try {
        await Future.wait<void>([publicResult, attempt.settled])
            .timeout(_testBound);
      } finally {
        await api.close();
        await stream.close().timeout(_testBound);
      }
    }
  });

  test('the three old domains still reject requests above 4096 before client acquisition', () async {
    final request = _fixture('request');
    (request['authorization'] as Json)['objective'] = List.filled(
      5000,
      'x',
    ).join();
    expect(_canonical(request).length, greaterThan(4096));
    for (final domain in [
      SettingsDomain.model,
      SettingsDomain.runtime,
      SettingsDomain.control,
    ]) {
      var acquired = 0;
      final transport = OwnedSettingsTransport(
        domain: domain,
        baseUrl: _base,
        ownerToken: _token,
        clientFactory: () {
          acquired++;
          return _Client((_) => _wire({'ok': true}));
        },
      );
      try {
        final attempt = transport.start(
          'POST',
          '/synthetic-owned-limit',
          request,
          (value) => value,
        );
        await expectLater(
          attempt.result,
          throwsA(
            isA<OwnedSettingsFailure>().having(
              (error) => error.unconfirmed,
              'old request not dispatched',
              isFalse,
            ),
          ),
        );
        await attempt.settled.timeout(_testBound);
        expect(attempt.dispatched, isFalse);
        expect(acquired, 0);
      } finally {
        await transport.close();
      }
    }
  });

  test(
    'the three old domains retain the 16384 streamed-response bound',
    () async {
      for (final domain in [
        SettingsDomain.model,
        SettingsDomain.runtime,
        SettingsDomain.control,
      ]) {
        final clients = <_Client>[];
        final large = utf8.encode(
          jsonEncode({'padding': List.filled(17000, 'x').join()}),
        );
        expect(large.length, greaterThan(16384));
        final transport = OwnedSettingsTransport(
          domain: domain,
          baseUrl: _base,
          ownerToken: _token,
          clientFactory: () {
            final sequence = clients.length;
            final client = _Client(
              (_) => sequence == 0
                  ? _wire({'ok': true})
                  : _raw(large, split: true),
            );
            clients.add(client);
            return client;
          },
        );
        try {
          final control = transport.start(
            'GET',
            '/synthetic-owned-limit',
            null,
            (value) => value,
          );
          expect(await control.result, {'ok': true});
          await control.settled.timeout(_testBound);
          final oversized = transport.start(
            'GET',
            '/synthetic-owned-limit',
            null,
            (value) => value,
          );
          await expectLater(
            oversized.result,
            throwsA(
              isA<OwnedSettingsFailure>().having(
                (error) => error.unconfirmed,
                'old response dispatched',
                isTrue,
              ),
            ),
          );
          await oversized.settled.timeout(_testBound);
          expect(oversized.dispatched, isTrue);
          expect(
            clients.every(
              (client) => client.sent.length == 1 && client.closes == 1,
            ),
            isTrue,
          );
        } finally {
          await transport.close();
        }
      }
    },
  );
}
