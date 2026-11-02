// Independent owner transport oracles. Injected clients never open HTTP sockets.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/owned_settings_client.dart';
import 'package:kinaiya_flutter/api/standing_dispatch.dart';
import 'package:kinaiya_flutter/api/standing_dispatch_client.dart';

typedef Json = Map<String, Object?>;

const _base = 'https://dispatch.synthetic.invalid/session';
const _token = 'synthetic-owner-dispatch-token';
const _marker = 'PRIVATE-SYNTHETIC-RESPONSE';
const _bound = Duration(seconds: 3);
late Json _vectors;

Json _copy(Object? value) =>
    (jsonDecode(jsonEncode(value)) as Map).cast<String, Object?>();
Json _fixture(String name) => _copy(_vectors[name]);
StandingDispatchActivation _activation() =>
    StandingDispatchActivation.fromJson(_fixture('activation'));
StandingDispatchStop _stop() => StandingDispatchStop.fromJson(_fixture('stop'));

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

StandingDispatchOwnerApi _api(
  http.Client Function() factory, {
  String base = _base,
  String token = _token,
  Duration timeout = const Duration(seconds: 10),
  Duration cleanup = const Duration(milliseconds: 10),
}) => StandingDispatchOwnerApi(
  baseUrl: base,
  ownerToken: token,
  clientFactory: factory,
  resultTimeout: timeout,
  cleanupTimeout: cleanup,
);

Future<void> _refused<T>(
  StandingDispatchOwnerAttempt<T> attempt, {
  int? status,
  bool unconfirmed = true,
}) async {
  await expectLater(
    attempt.result,
    throwsA(
      isA<StandingDispatchOwnerFailure>()
          .having((e) => e.status, 'status', status)
          .having(
            (e) => e.unconfirmed,
            'actual dispatch uncertainty',
            unconfirmed,
          )
          .having(
            (e) => e.toString(),
            'private response',
            isNot(contains(_marker)),
          )
          .having((e) => e.toString(), 'private token', isNot(contains(_token)))
          .having(
            (e) => e.toString(),
            'private endpoint',
            isNot(contains(_base)),
          ),
    ),
  );
  await attempt.settled.timeout(_bound);
  expect(attempt.dispatched, unconfirmed);
}

void main() {
  setUpAll(() {
    _vectors =
        (jsonDecode(
                  File(
                    'test/fixtures/standing-dispatch-independent-v1.json',
                  ).readAsStringSync(),
                )
                as Map)
            .cast<String, Object?>();
  });

  test(
    'five owner routes retain exact bodies, bearer identity and owned single-use clients',
    () async {
      final clients = <_Client>[];
      final requests = <http.BaseRequest>[];
      final postBytes = <List<int>>[];
      final raw = _fixture('activation');
      final original = StandingDispatchActivation.fromJson(raw);
      final api = _api(() {
        final client = _Client((request) {
          requests.add(request);
          expect(request.headers['Authorization'], 'Bearer $_token');
          expect(request.headers['Accept'], 'application/json');
          expect(request.headers['Accept-Encoding'], 'identity');
          expect(request.followRedirects, isFalse);
          expect(request.url.hasQuery, isFalse);
          expect(request.url.hasFragment, isFalse);
          final name = switch (request.url.path) {
            '/session/v1/standing/dispatch' => 'state',
            '/session/v1/standing/dispatch/stop-context' => 'stop',
            '/session/v1/standing/dispatch/activate' => 'activation_receipt',
            '/session/v1/standing/dispatch/activate/lookup' => 'lookup',
            '/session/v1/standing/dispatch/stop' => 'stop_proof',
            _ => throw StateError('Unexpected owner path'),
          };
          if (request.method == 'POST') {
            expect(request.headers['Content-Type'], 'application/json');
            final body = (request as http.Request).bodyBytes;
            expect(body.length, lessThanOrEqualTo(4096));
            postBytes.add(body.toList());
            expect(
              jsonDecode(utf8.decode(body)),
              name == 'stop_proof' ? _fixture('stop') : _fixture('activation'),
            );
          }
          return _wire(_fixture(name));
        });
        clients.add(client);
        return client;
      }, base: '$_base/');
      try {
        expect(api.baseUrl, _base);
        final read = api.snapshot();
        expect((await read.result).dispatchReady, isTrue);
        await read.settled.timeout(_bound);
        final context = api.stopContext();
        expect((await context.result).matches(_stop()), isTrue);
        await context.settled.timeout(_bound);
        final save = api.activate(original);
        expect((await save.result).request.matches(original), isTrue);
        await save.settled.timeout(_bound);
        (raw['mode_change'] as Json)['idempotency_key'] =
            'later caller mutation';
        final lookup = api.lookup(original);
        expect(
          (await lookup.result).receipt!.request.matches(original),
          isTrue,
        );
        await lookup.settled.timeout(_bound);
        final stop = api.stop(_stop());
        expect((await stop.result).targetFenced, isTrue);
        await stop.settled.timeout(_bound);
        expect(requests.map((r) => (r.method, r.url.path)).toList(), [
          ('GET', '/session/v1/standing/dispatch'),
          ('GET', '/session/v1/standing/dispatch/stop-context'),
          ('POST', '/session/v1/standing/dispatch/activate'),
          ('POST', '/session/v1/standing/dispatch/activate/lookup'),
          ('POST', '/session/v1/standing/dispatch/stop'),
        ]);
        expect(postBytes[0], postBytes[1]);
        expect(clients.length, 5);
        expect(
          clients.every((c) => c.sent.length == 1 && c.closes == 1),
          isTrue,
        );
        expect([
          read.dispatched,
          context.dispatched,
          save.dispatched,
          lookup.dispatched,
          stop.dispatched,
        ], everyElement(isTrue));
      } finally {
        await api.close();
      }
    },
  );

  test(
    'internally valid alternate originals cannot settle activate, lookup or Stop',
    () async {
      expect(
        StandingDispatchActivationReceipt.fromJson(
          _fixture('other_receipt'),
        ).request.matches(_activation()),
        isFalse,
      );
      expect(
        StandingDispatchActivationLookup.fromJson(
          _fixture('other_lookup'),
        ).request.matches(_activation()),
        isFalse,
      );
      expect(
        StandingDispatchStopProof.fromJson(
          _fixture('other_stop_proof'),
        ).request.matches(_stop()),
        isFalse,
      );
      final clients = <_Client>[];
      final api = _api(() {
        final client = _Client(
          (request) => _wire(
            _fixture(
              request.url.path.endsWith('/lookup')
                  ? 'other_lookup'
                  : request.url.path.endsWith('/stop')
                  ? 'other_stop_proof'
                  : 'other_receipt',
            ),
          ),
        );
        clients.add(client);
        return client;
      });
      try {
        await _refused(api.activate(_activation()));
        await _refused(api.lookup(_activation()));
        await _refused(api.stop(_stop()));
        expect(
          clients.every((c) => c.sent.length == 1 && c.closes == 1),
          isTrue,
        );
      } finally {
        await api.close();
      }
    },
  );

  test(
    'strict JSON, private errors, media and oversized chunks never become a proof or retry',
    () async {
      final good = jsonEncode(_fixture('activation_receipt'));
      final nestedExtra = _fixture('activation_receipt');
      ((nestedExtra['mode_receipt'] as Json)['request']
              as Json)['discarded_by_old_model'] =
          _marker;
      final cases = <(String, http.StreamedResponse Function(), int?)>[
        (
          'duplicate',
          () => _raw(
            utf8.encode(good.replaceFirst('"epoch":1', '"epoch":1,"epoch":1')),
          ),
          null,
        ),
        ('trailing', () => _raw(utf8.encode('$good {}')), null),
        (
          'float',
          () =>
              _raw(utf8.encode(good.replaceFirst('"epoch":1', '"epoch":1.0'))),
          null,
        ),
        (
          'unsafe integer',
          () => _raw(
            utf8.encode(
              good.replaceFirst('"epoch":1', '"epoch":9007199254740992'),
            ),
          ),
          null,
        ),
        (
          'surrogate',
          () =>
              _raw(utf8.encode(good.replaceFirst('"mode_saved"', '"\\ud800"'))),
          null,
        ),
        ('invalid UTF8', () => _raw([0xff]), null),
        ('nested sanitization', () => _wire(nestedExtra), null),
        (
          'wrong media',
          () =>
              _raw(utf8.encode(good), headers: {'content-type': 'text/plain'}),
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
          'declared oversize',
          () => _raw(utf8.encode(good), length: 32769),
          null,
        ),
        (
          'actual oversize',
          () => _raw(
            utf8.encode('$good${List.filled(32769, ' ').join()}'),
            split: true,
          ),
          null,
        ),
        ('length mismatch', () => _raw(utf8.encode(good), length: 1), null),
        (
          'conflict',
          () => _wire({'private': '$_marker $_token $_base'}, status: 409),
          409,
        ),
        (
          'unavailable',
          () => _wire({'private': '$_marker $_token $_base'}, status: 503),
          503,
        ),
        (
          'redirect',
          () => _raw(
            utf8.encode(_marker),
            status: 302,
            headers: {'location': 'https://foreign.invalid'},
          ),
          302,
        ),
      ];
      for (final (name, reply, status) in cases) {
        final client = _Client((_) => reply());
        final api = _api(() => client);
        try {
          await _refused(api.activate(_activation()), status: status);
          expect(client.sent.length, 1, reason: name);
          expect(client.closes, 1, reason: name);
        } finally {
          await api.close();
        }
      }
    },
  );

  test(
    'the new owner domain admits exactly32768 raw proof bytes and refuses32769',
    () async {
      final good = utf8.encode(jsonEncode(_fixture('lookup')));
      final inclusive = [...good, ...List.filled(32768 - good.length, 32)];
      for (final extra in [0, 1]) {
        final bytes = [...inclusive, ...List.filled(extra, 32)];
        final client = _Client(
          (_) => _raw(bytes, length: bytes.length, split: true),
        );
        final api = _api(() => client);
        try {
          final attempt = api.lookup(_activation());
          if (extra == 0) {
            expect((await attempt.result).receipt!.epoch, 1);
            await attempt.settled.timeout(_bound);
          } else {
            await _refused(attempt);
          }
          expect(client.sent.length, 1);
          expect(client.closes, 1);
        } finally {
          await api.close();
        }
      }
    },
  );

  test(
    'shared JSON domain caps reject oversized preflight before client construction',
    () async {
      var factories = 0;
      final clients = <_Client>[];
      final transport = OwnedSettingsTransport(
        domain: SettingsDomain.standingDispatch,
        baseUrl: _base,
        ownerToken: _token,
        clientFactory: () {
          factories++;
          final client = _Client((_) => _wire({}));
          clients.add(client);
          return client;
        },
      );
      // This is solely the generic JSON transport bound, not a native DTO ACK.
      final overhead = utf8.encode(jsonEncode({'text': ''})).length;
      try {
        final accepted = transport.start('POST', '/synthetic-cap', {
          'text': List.filled(4096 - overhead, 'x').join(),
        }, (_) => true);
        expect(await accepted.result, isTrue);
        await accepted.settled.timeout(_bound);
        expect(
          (clients.single.sent.single as http.Request).bodyBytes.length,
          4096,
        );
        final refused = transport.start('POST', '/synthetic-cap', {
          'text': List.filled(4097 - overhead, 'x').join(),
        }, (_) => true);
        await expectLater(
          refused.result,
          throwsA(
            isA<OwnedSettingsFailure>().having(
              (e) => e.unconfirmed,
              'no actual dispatch',
              isFalse,
            ),
          ),
        );
        await refused.settled.timeout(_bound);
        expect(refused.dispatched, isFalse);
        expect(factories, 1);
        expect(clients.single.closes, 1);
        final old = OwnedSettingsTransport(
          domain: SettingsDomain.standing,
          baseUrl: _base,
          ownerToken: _token,
          clientFactory: () => _Client((_) => _wire({})),
        );
        try {
          final stillLarge = old.start('POST', '/synthetic-cap', {
            'text': List.filled(5000, 'x').join(),
          }, (_) => true);
          expect(await stillLarge.result, isTrue);
          await stillLarge.settled.timeout(_bound);
          expect(transport.sameOwner(old), isFalse);
        } finally {
          await old.close();
        }
      } finally {
        await transport.close();
      }
    },
  );

  test(
    'owner scope, canonical endpoint and closed facade never create fallback HTTP',
    () async {
      var factories = 0;
      http.Client factory() {
        factories++;
        throw StateError('No real HTTP is allowed');
      }

      final first = _api(factory, base: '$_base/');
      final same = _api(factory);
      final otherOwner = _api(factory, token: 'other-synthetic-owner');
      final otherBase = _api(
        factory,
        base: 'https://other.synthetic.invalid/session',
      );
      try {
        expect(first.sameOwner(same), isTrue);
        expect(first.sameOwner(otherOwner), isFalse);
        expect(first.sameOwner(otherBase), isFalse);
        await first.close();
        await _refused(first.snapshot(), unconfirmed: false);
        expect(first.hasCapacity, isFalse);
        expect(same.hasCapacity, isTrue);
        expect(factories, 0);
        await _refused(same.snapshot(), unconfirmed: false);
        expect(
          factories,
          1,
        ); // Injected failure is final; there is no default IO client.
      } finally {
        await Future.wait([
          first.close(),
          same.close(),
          otherOwner.close(),
          otherBase.close(),
        ]);
      }
    },
  );

  test(
    'invalid credentials or deadline bounds refuse before injected acquisition',
    () async {
      var factories = 0;
      http.Client factory() {
        factories++;
        throw StateError('Unexpected acquisition');
      }

      for (final api in [
        _api(factory, token: ''),
        _api(factory, token: 'bad token'),
        _api(factory, token: List.filled(4097, 'x').join()),
        _api(factory, timeout: Duration.zero),
        _api(factory, timeout: const Duration(seconds: 11)),
        _api(factory, cleanup: Duration.zero),
        _api(factory, cleanup: const Duration(seconds: 2)),
      ]) {
        try {
          await _refused(api.snapshot(), unconfirmed: false);
        } finally {
          await api.close();
        }
      }
      expect(factories, 0);
    },
  );

  test(
    'null-controller snapshot remains explicit read-only history through actual send',
    () async {
      final client = _Client((_) => _wire(_fixture('null_controller_state')));
      final api = _api(() => client);
      try {
        final attempt = api.snapshot();
        final state = await attempt.result;
        expect(state.instanceId, isNull);
        expect(state.controlWritable, isFalse);
        expect(state.producer, isNull);
        expect(state.dispatchReady, isFalse);
        expect(state.epoch, 1);
        await attempt.settled.timeout(_bound);
        expect(client.closes, 1);
      } finally {
        await api.close();
      }
    },
  );

  test(
    'public result deadline includes blocked cleanup while actual settlement stays pending',
    () async {
      final cancelled = Completer<void>();
      final cancelStarted = Completer<void>();
      final stream = StreamController<List<int>>(
        onCancel: () {
          if (!cancelStarted.isCompleted) cancelStarted.complete();
          return cancelled.future;
        },
      );
      final client = _Client(
        (_) => http.StreamedResponse(
          stream.stream,
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      final api = _api(
        () => client,
        timeout: const Duration(milliseconds: 30),
        cleanup: const Duration(milliseconds: 500),
      );
      final watch = Stopwatch()..start();
      final attempt = api.snapshot();
      var settled = false;
      unawaited(attempt.settled.then((_) => settled = true));
      stream.add(
        utf8.encode(jsonEncode(_fixture('state'))),
      ); // Body has not reached actual EOF.
      try {
        await expectLater(
          attempt.result.timeout(_bound),
          throwsA(
            isA<StandingDispatchOwnerFailure>().having(
              (e) => e.unconfirmed,
              'already sent',
              isTrue,
            ),
          ),
        );
        expect(watch.elapsed, lessThan(const Duration(milliseconds: 350)));
        await cancelStarted.future.timeout(_bound);
        expect(settled, isFalse);
        await api.close().timeout(_bound);
        expect(settled, isFalse);
        expect(client.closes, 1);
        cancelled.complete();
        await attempt.settled.timeout(_bound);
        expect(settled, isTrue);
        expect(client.sent.length, 1);
      } finally {
        if (!cancelled.isCompleted) cancelled.complete();
        await api.close();
        await attempt.settled.timeout(_bound);
        await stream.close();
      }
    },
  );

  test(
    'combined32 owners span all five domains and retain late cleanup across facade replacement',
    () async {
      final pending = <Completer<http.StreamedResponse>>[];
      final clients = <_Client>[];
      final checks = <Future<void>>[];
      final settlements = <Future<void>>[];
      final actualSettled = List.filled(32, false);
      final cancellation = Completer<void>(), cancelStarted = Completer<void>();
      final late = StreamController<List<int>>(
        onCancel: () {
          if (!cancelStarted.isCompleted) cancelStarted.complete();
          return cancellation.future;
        },
      );
      http.Client factory() {
        final response = Completer<http.StreamedResponse>();
        pending.add(response);
        final client = _Client((_) => response.future);
        clients.add(client);
        return client;
      }

      final api = _api(factory, timeout: const Duration(milliseconds: 80));
      final old = [
        for (final domain in [
          SettingsDomain.model,
          SettingsDomain.runtime,
          SettingsDomain.control,
          SettingsDomain.standing,
        ])
          OwnedSettingsTransport(
            domain: domain,
            baseUrl: _base,
            ownerToken: _token,
            clientFactory: factory,
            resultTimeout: const Duration(milliseconds: 80),
            cleanupTimeout: const Duration(milliseconds: 10),
          ),
      ];
      final contenderClients = <_Client>[];
      final contender = _api(() {
        final client = _Client((_) => _wire(_fixture('state')));
        contenderClients.add(client);
        return client;
      });
      void retain(Future<void> settlement) {
        final index = settlements.length;
        settlements.add(settlement);
        unawaited(settlement.then((_) => actualSettled[index] = true));
      }

      try {
        for (var n = 0; n < 8; n++) {
          final attempt = api.snapshot();
          retain(attempt.settled);
          checks.add(
            expectLater(
              attempt.result,
              throwsA(
                isA<StandingDispatchOwnerFailure>().having(
                  (e) => e.unconfirmed,
                  'dispatch actual owner',
                  isTrue,
                ),
              ),
            ),
          );
        }
        for (final transport in old) {
          for (var n = 0; n < 6; n++) {
            final attempt = transport.start(
              'GET',
              '/synthetic-shared-ownership',
              null,
              (value) => value,
            );
            retain(attempt.settled);
            checks.add(
              expectLater(
                attempt.result,
                throwsA(
                  isA<OwnedSettingsFailure>().having(
                    (e) => e.unconfirmed,
                    'old actual owner',
                    isTrue,
                  ),
                ),
              ),
            );
          }
        }
        expect(pending.length, 32);
        expect(clients.every((c) => c.sent.length == 1), isTrue);
        await Future.wait(checks).timeout(_bound);
        await Future.wait([
          api.close(),
          ...old.map((t) => t.close()),
        ]).timeout(_bound);
        expect(actualSettled.any((value) => value), isFalse);
        expect(contender.hasCapacity, isFalse);
        expect(old.every((t) => t.hasCapacity), isFalse);
        await _refused(contender.snapshot(), unconfirmed: false);
        expect(contenderClients, isEmpty);
        pending[0].complete(
          http.StreamedResponse(
            late.stream,
            200,
            headers: {'content-type': 'application/json'},
          ),
        );
        await cancelStarted.future.timeout(_bound);
        expect(actualSettled[0], isFalse);
        expect(contender.hasCapacity, isFalse);
        cancellation.complete();
        await settlements[0].timeout(_bound);
        expect(actualSettled[0], isTrue);
        expect(contender.hasCapacity, isTrue);
        final accepted = contender.snapshot();
        expect((await accepted.result).dispatchReady, isTrue);
        await accepted.settled.timeout(_bound);
        expect(contenderClients.single.sent.length, 1);
        expect(contenderClients.single.closes, 1);
      } finally {
        if (!cancellation.isCompleted) cancellation.complete();
        if (pending.isNotEmpty && !pending[0].isCompleted) {
          pending[0].complete(
            http.StreamedResponse(
              late.stream,
              200,
              headers: {'content-type': 'application/json'},
            ),
          );
        }
        for (var n = 1; n < pending.length; n++) {
          if (!pending[n].isCompleted) {
            pending[n].complete(_wire(_fixture('state')));
          }
        }
        await Future.wait(checks).timeout(_bound);
        await Future.wait(settlements).timeout(_bound);
        await Future.wait([
          api.close(),
          contender.close(),
          ...old.map((t) => t.close()),
        ]).timeout(_bound);
        await late.close();
      }
      expect(actualSettled, everyElement(isTrue));
      expect(clients.every((c) => c.closes == 1), isTrue);
    },
  );
}
