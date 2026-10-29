import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'package:kinaiya_flutter/api/memory_client.dart';
import 'package:kinaiya_flutter/api/memory_models.dart';

import 'memory_models_test.dart' as fixture;

class OwnedClient extends http.BaseClient {
  OwnedClient(this.handler, {this.closeError = false});
  final Future<http.StreamedResponse> Function(http.BaseRequest) handler;
  final bool closeError;
  int sends = 0;
  int closes = 0;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    sends++;
    return handler(request);
  }

  @override
  void close() {
    closes++;
    if (closeError) throw StateError('PRIVATE TRANSPORT ERROR');
  }
}

class CountedMockClient extends MockClient {
  CountedMockClient(super.handler);
  int closes = 0;
  @override
  void close() {
    closes++;
    super.close();
  }
}

http.StreamedResponse rawResponse(
  List<int> bytes, {
  int status = 200,
  Map<String, String>? headers,
  int? length,
}) => http.StreamedResponse(
  Stream.value(bytes),
  status,
  headers: headers ?? {'content-type': 'application/json'},
  contentLength: length,
);
http.StreamedResponse response(Object? body, {int status = 200}) =>
    rawResponse(utf8.encode(jsonEncode(body)), status: status);
MemoryApi api(
  http.Client client, {
  String session = 'http://127.0.0.1:8761',
  String token = 'test-token',
  Duration timeout = const Duration(seconds: 1),
}) => MemoryApi(
  sessionBase: session,
  converseBase: 'https://converse.example/prefix',
  ownerToken: token,
  clientFactory: () => client,
  requestTimeout: timeout,
  cleanupTimeout: const Duration(milliseconds: 30),
);
final binding = MemoryBinding('s', 't', 1);
Matcher failure({bool unconfirmed = false, int? status}) => isA<MemoryFailure>()
    .having((e) => e.unconfirmed, 'unconfirmed', unconfirmed)
    .having((e) => e.status, 'status', status);

void main() {
  test(
    'all six exact native routes/body shapes and detached selection',
    () async {
      final requests = <Map<String, Object?>>[];
      final clients = <OwnedClient>[];
      final memory = MemoryApi(
        sessionBase: 'http://localhost:8761/prefix/',
        converseBase: 'https://converse.example',
        ownerToken: 'owner-token',
        clientFactory: () {
          final c = OwnedClient((request) async {
            expect(request.method, 'POST');
            expect(request.followRedirects, false);
            expect(request.headers['Authorization'], 'Bearer owner-token');
            final bytes = await request.finalize().toBytes();
            expect(bytes.length, lessThanOrEqualTo(16 * 1024));
            final body = jsonDecode(utf8.decode(bytes)) as Map<String, Object?>;
            requests.add({'url': request.url.toString(), ...body});
            if (request.url.path == '/v1/memory/state') {
              return response(fixture.useJson());
            }
            if (request.url.path == '/v1/memory/use') {
              return response({
                ...fixture.useJson(),
                'enabled': body['enabled'],
                'use_generation': body['enabled'] == true ? 1 : 0,
                'implementation_id': body['implementation_id'],
                'selector_ids': body['selector_ids'] ?? [],
              });
            }
            if (request.url.path.endsWith('/configure')) {
              return response({
                ...fixture.stateJson(),
                'enabled': body['enabled'],
                'revision': 4,
              });
            }
            if (request.url.path.endsWith('/catalog')) {
              return response({
                'schema_version': '1.0',
                'state': fixture.stateJson(),
                'entries': [],
              });
            }
            if (request.url.path.endsWith('/inspect')) {
              return response({
                'schema_version': '1.0',
                'state': fixture.stateJson(count: 1),
                'node': fixture.nodeJson(),
              });
            }
            return response(fixture.stateJson());
          });
          clients.add(c);
          return c;
        },
      );
      await memory.state(binding);
      await memory.configure(binding, expectedRevision: 3, enabled: true);
      await memory.catalog(binding);
      await memory.inspect(binding, 'n1');
      await memory.useState(binding);
      final ids = ['n1'];
      final pending = memory.configureUse(
        binding,
        expectedRuntimeId: 'runtime',
        expectedGeneration: 0,
        enabled: true,
        selectorIds: ids,
      );
      ids.clear();
      await pending;
      await memory.configureUse(
        binding,
        expectedRuntimeId: 'runtime',
        expectedGeneration: 0,
        enabled: false,
      );
      expect(requests.length, 7);
      expect(
        requests[0]['url'],
        'http://localhost:8761/prefix/v1/converse-memory/state',
      );
      expect(requests[1]['expected_revision'], 3);
      expect(requests[3]['node_id'], 'n1');
      expect(requests[5]['implementation_id'], 'korina-dag-v1');
      expect(requests[5]['selector_ids'], ['n1']);
      expect(requests[6].containsKey('selector_ids'), false);
      expect(requests[6].containsKey('implementation_id'), false);
      expect(clients.every((c) => c.sends == 1 && c.closes == 1), true);
    },
  );
  test(
    'normal/mock KinaiyaClient isolate memory ownership from shared transport',
    () async {
      for (final mock in [false, true]) {
        final shared = OwnedClient((r) async => response({}));
        final owned = OwnedClient((r) async => response(fixture.stateJson()));
        final c = mock
            ? KinaiyaClient.mock(
                sessionBase: 'http://localhost:8761',
                agentBase: 'unused',
                converseBase: 'http://localhost:8763',
                mockTransport: shared,
                memoryClientFactory: () => owned,
              )
            : KinaiyaClient(
                sessionBase: 'http://localhost:8761',
                agentBase: 'unused',
                converseBase: 'http://localhost:8763',
                ownerToken: 'token',
                httpClient: shared,
                memoryClientFactory: () => owned,
              );
        await c.memory.state(binding);
        expect(owned.closes, 1);
        expect(shared.closes, 0);
        c.close();
        expect(shared.closes, 1);
      }
    },
  );
  test(
    'strict raw JSON attacks and invalid Unicode never become a snapshot',
    () async {
      final normal = jsonEncode(fixture.stateJson());
      final raws = [
        normal.replaceFirst('"epoch":1', '"epoch":1,"epoch":1'),
        normal.replaceFirst('"epoch":1', r'"epoch":1,"\u0065poch":1'),
        normal.replaceFirst('"revision":0', '"revision":9007199254740992'),
        normal.replaceFirst('"revision":0', '"revision":9007199254740993'),
        normal.replaceFirst('"revision":0', '"revision":1.0'),
        normal.replaceFirst('"revision":0', '"revision":1e0'),
        normal.replaceFirst('"revision":0', '"revision":NaN'),
        normal.replaceFirst('"revision":0', '"revision":Infinity'),
        normal.replaceFirst('"revision":0', '"revision":01'),
        normal.replaceFirst('"revision":0', '"revision":-1'),
        normal.replaceFirst('"session_id":"s"', r'"session_id":"\ud800"'),
        normal.replaceFirst('"session_id":"s"', r'"session_id":"\udc00"'),
        normal.replaceFirst('"session_id":"s"', '"session_id":"s\n"'),
        normal.replaceFirst(
          '"status":"ready"',
          '"status":"ready","extra":true',
        ),
        normal.replaceFirst(
          '"status":"ready"',
          '"status":${'[' * 18}null${']' * 18}',
        ),
        '$normal trailing',
        '$normal$normal',
        '',
      ];
      for (final raw in raws) {
        final c = OwnedClient((r) async => rawResponse(utf8.encode(raw)));
        await expectLater(api(c).state(binding), throwsA(failure()));
        expect(c.sends, 1);
        expect(c.closes, 1);
      }
      final malformed = OwnedClient((r) async => rawResponse([0xff]));
      await expectLater(api(malformed).state(binding), throwsA(failure()));
      expect(malformed.closes, 1);
    },
  );
  test('safe integer and valid split Unicode JSON are accepted', () async {
    final bytes = utf8.encode(
      jsonEncode({...fixture.stateJson(), 'revision': memoryMaxSafeInteger}),
    );
    final c = OwnedClient(
      (r) async => http.StreamedResponse(
        Stream.fromIterable(bytes.map((b) => [b])),
        200,
        headers: {'content-type': 'application/json; charset=utf-8'},
      ),
    );
    expect((await api(c).state(binding)).revision, memoryMaxSafeInteger);
    expect(c.closes, 1);
  });
  test(
    'scope/node/runtime mismatches fail, write acknowledgments unconfirmed',
    () async {
      final other = OwnedClient(
        (r) async => response({...fixture.stateJson(), 'epoch': 2}),
      );
      await expectLater(api(other).state(binding), throwsA(failure()));
      final wrongNode = OwnedClient(
        (r) async => response({
          'schema_version': '1.0',
          'state': fixture.stateJson(count: 1),
          'node': fixture.nodeJson(),
        }),
      );
      await expectLater(
        api(wrongNode).inspect(binding, 'n2'),
        throwsA(failure()),
      );
      final wrongRuntime = OwnedClient(
        (r) async => response({...fixture.useJson(), 'runtime_id': 'other'}),
      );
      await expectLater(
        api(wrongRuntime).configureUse(
          binding,
          expectedRuntimeId: 'runtime',
          expectedGeneration: 0,
          enabled: false,
        ),
        throwsA(failure(unconfirmed: true)),
      );
      final invalidAck = OwnedClient((r) async => response({}));
      await expectLater(
        api(invalidAck).configure(binding, expectedRevision: 0, enabled: true),
        throwsA(failure(unconfirmed: true)),
      );
    },
  );
  test('error body never leaked; redirects never followed, no retry', () async {
    for (final status in [202, 204, 301, 307, 401, 403, 404, 409, 422, 503]) {
      final c = OwnedClient((r) async {
        expect(r.followRedirects, false);
        return rawResponse(
          utf8.encode('PRIVATE ERROR token=url'),
          status: status,
        );
      });
      try {
        await api(c).configure(binding, expectedRevision: 0, enabled: true);
        fail('accepted');
      } on MemoryFailure catch (e) {
        expect(e.status, status);
        expect(e.unconfirmed, status >= 500 || status < 400);
        expect(e.toString(), 'Memory request was refused');
      }
      expect(c.sends, 1);
      expect(c.closes, 1);
    }
  });
  test('wire/header/length limits close real owned transport', () async {
    final replies = [
      rawResponse(List.filled(256 * 1024 + 1, 32)),
      rawResponse([], length: 256 * 1024 + 1),
      rawResponse(
        utf8.encode(jsonEncode(fixture.stateJson())),
        headers: {
          'content-type': 'application/json',
          'content-encoding': 'gzip',
        },
      ),
      rawResponse(
        utf8.encode(jsonEncode(fixture.stateJson())),
        headers: {'content-type': 'text/html'},
      ),
      rawResponse(utf8.encode(jsonEncode(fixture.stateJson())), length: 1),
    ];
    for (final reply in replies) {
      final c = OwnedClient((r) async => reply);
      await expectLater(api(c).state(binding), throwsA(failure()));
      expect(c.closes, 1);
    }
  });
  test('URL/token/selection validation precedes transport creation', () async {
    var made = 0;
    for (final url in [
      'http://remote.example',
      'ftp://localhost',
      'https://user:pass@safe.example',
      'https://safe.example?token=x',
      'https://safe.example#f',
    ]) {
      final a = MemoryApi(
        sessionBase: url,
        converseBase: url,
        ownerToken: 'token',
        clientFactory: () {
          made++;
          throw StateError('must not create');
        },
      );
      await expectLater(a.state(binding), throwsA(failure()));
    }
    final c = OwnedClient((r) async => response({}));
    await expectLater(
      api(c, token: 'bad\r\ntoken').state(binding),
      throwsA(failure()),
    );
    for (final selected in [
      <String>[],
      ['a', 'a'],
      ['bad\n'],
      List.generate(9, (i) => 'n$i'),
    ]) {
      expect(
        () => api(c).configureUse(
          binding,
          expectedRuntimeId: 'runtime',
          expectedGeneration: 0,
          enabled: true,
          selectorIds: selected,
        ),
        throwsA(failure()),
      );
    }
    expect(
      () => api(c).configureUse(
        binding,
        expectedRuntimeId: 'runtime',
        expectedGeneration: 0,
        enabled: false,
        selectorIds: ['n'],
      ),
      throwsA(failure()),
    );
    expect(made, 0);
    expect(c.sends, 0);
    expect(c.closes, 0);
  });
  test(
    'send timeout closes client and later response is really cancelled',
    () async {
      final sent = Completer<http.StreamedResponse>();
      var cancelled = 0;
      final stream = StreamController<List<int>>(
        onCancel: () {
          cancelled++;
        },
      );
      final c = OwnedClient((r) => sent.future);
      await expectLater(
        api(
          c,
          timeout: const Duration(milliseconds: 20),
        ).configure(binding, expectedRevision: 0, enabled: true),
        throwsA(failure(unconfirmed: true)),
      );
      expect(c.closes, 1);
      sent.complete(
        http.StreamedResponse(
          stream.stream,
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      await Future<void>.delayed(const Duration(milliseconds: 10));
      expect(cancelled, 1);
      await stream.close();
    },
  );
  test(
    'held body/cancel and close failures are bounded and unconfirmed',
    () async {
      final cancel = Completer<void>();
      var cancelled = 0;
      final stream = StreamController<List<int>>(
        onCancel: () {
          cancelled++;
          return cancel.future;
        },
      );
      final c = OwnedClient(
        (r) async => http.StreamedResponse(
          stream.stream,
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      final pending = api(
        c,
        timeout: const Duration(milliseconds: 20),
      ).configure(binding, expectedRevision: 0, enabled: false);
      await expectLater(pending, throwsA(failure(unconfirmed: true)));
      expect(c.closes, 1);
      expect(cancelled, 1);
      cancel.complete();
      await stream.close();
      final badClose = OwnedClient(
        (r) async => response(fixture.stateJson()),
        closeError: true,
      );
      await expectLater(
        api(badClose).configure(binding, expectedRevision: 0, enabled: false),
        throwsA(failure(unconfirmed: true)),
      );
      expect(badClose.closes, 1);
    },
  );
  test(
    'transport errors are fixed and writes distinguish pre/post dispatch',
    () async {
      final bad = OwnedClient((r) async => throw StateError('PRIVATE token'));
      await expectLater(
        api(bad).configure(binding, expectedRevision: 0, enabled: true),
        throwsA(failure(unconfirmed: true)),
      );
      expect(bad.closes, 1);
      expect(bad.sends, 1);
      final noClient = MemoryApi(
        sessionBase: 'https://safe.example',
        converseBase: 'https://safe.example',
        ownerToken: 'token',
        clientFactory: () => throw StateError('PRIVATE'),
      );
      await expectLater(
        noClient.configure(binding, expectedRevision: 0, enabled: false),
        throwsA(failure()),
      );
    },
  );
  test(
    'contradictory or stale HTTP200 writes are uncertain, pristine off equal is valid',
    () async {
      for (final change in [
        <String, Object?>{'enabled': false, 'revision': 4},
        {'enabled': true, 'revision': 2},
      ]) {
        final c = OwnedClient(
          (r) async => response({...fixture.stateJson(), ...change}),
        );
        await expectLater(
          api(c).configure(binding, expectedRevision: 3, enabled: true),
          throwsA(failure(unconfirmed: true)),
        );
      }
      final good = {
        ...fixture.useJson(),
        'enabled': true,
        'use_generation': 5,
        'implementation_id': 'korina-dag-v1',
        'selector_ids': ['n1'],
      };
      for (final change in [
        <String, Object?>{'enabled': false},
        {'use_generation': 3},
        {'use_generation': 2},
        {'implementation_id': 'other'},
        {
          'selector_ids': ['n2'],
        },
      ]) {
        final c = OwnedClient((r) async => response({...good, ...change}));
        await expectLater(
          api(c).configureUse(
            binding,
            expectedRuntimeId: 'runtime',
            expectedGeneration: 3,
            enabled: true,
            selectorIds: ['n1'],
          ),
          throwsA(failure(unconfirmed: true)),
        );
      }
      final advanced = OwnedClient((r) async => response(good));
      expect(
        (await api(advanced).configureUse(
          binding,
          expectedRuntimeId: 'runtime',
          expectedGeneration: 3,
          enabled: true,
          selectorIds: ['n1'],
        )).useGeneration,
        5,
      );
      final off = OwnedClient((r) async => response(fixture.useJson()));
      expect(
        (await api(off).configureUse(
          binding,
          expectedRuntimeId: 'runtime',
          expectedGeneration: 0,
          enabled: false,
        )).useGeneration,
        0,
      );
    },
  );
  test(
    'owner close aborts all active clients once without closing a foreign owner',
    () async {
      final streams = List.generate(3, (_) => StreamController<List<int>>());
      final clients = <OwnedClient>[];
      var cancelled = 0;
      final memory = MemoryApi(
        sessionBase: 'http://localhost:8761',
        converseBase: 'https://converse.example',
        ownerToken: 'token',
        clientFactory: () {
          final index = clients.length;
          streams[index].onCancel = () {
            cancelled++;
          };
          final c = OwnedClient(
            (r) async => http.StreamedResponse(
              streams[index].stream,
              200,
              headers: {'content-type': 'application/json'},
            ),
          );
          clients.add(c);
          return c;
        },
      );
      final pending = List.generate(
        3,
        (_) => expectLater(
          memory.configure(binding, expectedRevision: 0, enabled: false),
          throwsA(failure(unconfirmed: true)),
        ),
      );
      await Future<void>.delayed(Duration.zero);
      final closing = memory.close();
      expect(clients.map((c) => c.closes), [1, 1, 1]);
      await closing;
      await memory.close();
      await Future.wait(pending);
      expect(cancelled, 3);
      await expectLater(memory.state(binding), throwsA(failure()));
      expect(clients.length, 3);
      expect(clients.map((c) => c.closes), [1, 1, 1]);
      for (final stream in streams) {
        await stream.close();
      }
      final shared = OwnedClient((r) async => response({}));
      final held = Completer<http.StreamedResponse>();
      final own = OwnedClient((r) => held.future);
      final c = KinaiyaClient(
        sessionBase: 'http://localhost:8761',
        agentBase: 'unused',
        converseBase: 'https://converse.example',
        ownerToken: 'token',
        httpClient: shared,
        memoryClientFactory: () => own,
      );
      final operation = expectLater(
        c.memory.state(binding),
        throwsA(failure()),
      );
      c.close();
      expect(own.closes, 1);
      expect(shared.closes, 1);
      held.complete(response(fixture.stateJson()));
      await operation;
      expect(own.closes, 1);
    },
  );
  test(
    'unsettled send owners have a finite cap and are released only after late cancellation',
    () async {
      final sends = List.generate(8, (_) => Completer<http.StreamedResponse>());
      final streams = List.generate(8, (_) => StreamController<List<int>>());
      var made = 0;
      var cancelled = 0;
      final memory = MemoryApi(
        sessionBase: 'http://localhost:8761',
        converseBase: 'https://converse.example',
        ownerToken: 'token',
        requestTimeout: const Duration(milliseconds: 15),
        cleanupTimeout: const Duration(milliseconds: 30),
        clientFactory: () {
          final i = made++;
          return OwnedClient(
            (r) => i < 8
                ? sends[i].future
                : Future.value(response(fixture.stateJson())),
          );
        },
      );
      for (var i = 0; i < 8; i++) {
        await expectLater(memory.state(binding), throwsA(failure()));
      }
      await expectLater(
        memory.configure(binding, expectedRevision: 0, enabled: false),
        throwsA(failure()),
      );
      expect(made, 8);
      for (var i = 0; i < 8; i++) {
        streams[i].onCancel = () {
          cancelled++;
        };
        sends[i].complete(
          http.StreamedResponse(
            streams[i].stream,
            200,
            headers: {'content-type': 'application/json'},
          ),
        );
      }
      await Future<void>.delayed(const Duration(milliseconds: 10));
      expect(cancelled, 8);
      expect((await memory.state(binding)).binding, binding);
      expect(made, 9);
      await memory.close();
      for (final stream in streams) {
        await stream.close();
      }
    },
  );
  test(
    'header refusals cancel body subscription and finite deadline includes body',
    () async {
      var cancelled = 0;
      final stream = StreamController<List<int>>(
        onCancel: () {
          cancelled++;
        },
      );
      final c = OwnedClient(
        (r) async => http.StreamedResponse(
          stream.stream,
          302,
          headers: {'location': 'https://elsewhere.example'},
        ),
      );
      await expectLater(api(c).state(binding), throwsA(failure(status: 302)));
      expect(cancelled, 1);
      expect(c.sends, 1);
      await stream.close();
      final body = StreamController<List<int>>();
      final delayed = OwnedClient((r) async {
        await Future<void>.delayed(const Duration(milliseconds: 25));
        return http.StreamedResponse(
          body.stream,
          200,
          headers: {'content-type': 'application/json'},
        );
      });
      final watch = Stopwatch()..start();
      await expectLater(
        api(delayed, timeout: const Duration(milliseconds: 40)).state(binding),
        throwsA(failure()),
      );
      watch.stop();
      expect(watch.elapsedMilliseconds, lessThan(200));
      expect(delayed.closes, 1);
      await body.close();
    },
  );
  test(
    'completed delayed cancellation restores capacity only after successful client close',
    () async {
      for (final closeFails in [false, true]) {
        final releases = List.generate(8, (_) => Completer<void>());
        final streams = <StreamController<List<int>>>[];
        final clients = <OwnedClient>[];
        var cancelStarted = 0;
        var cancelFinished = 0;
        final memory = MemoryApi(
          sessionBase: 'http://localhost:8761',
          converseBase: 'https://converse.example',
          ownerToken: 'token',
          requestTimeout: const Duration(milliseconds: 10),
          cleanupTimeout: const Duration(milliseconds: 10),
          clientFactory: () {
            final index = clients.length;
            final c = OwnedClient((r) async {
              if (index >= 8) return response(fixture.stateJson());
              final stream = StreamController<List<int>>(
                onCancel: () async {
                  cancelStarted++;
                  await releases[index].future;
                  cancelFinished++;
                },
              );
              streams.add(stream);
              return http.StreamedResponse(
                stream.stream,
                200,
                headers: {'content-type': 'application/json'},
              );
            }, closeError: closeFails);
            clients.add(c);
            return c;
          },
        );
        for (var i = 0; i < 8; i++) {
          await expectLater(memory.state(binding), throwsA(failure()));
        }
        expect(clients.length, 8);
        expect(cancelStarted, 8);
        expect(cancelFinished, 0);
        expect(clients.map((c) => c.closes), List.filled(8, 1));
        await expectLater(memory.state(binding), throwsA(failure()));
        expect(clients.length, 8);
        for (final release in releases) {
          release.complete();
        }
        for (var i = 0; i < 20 && cancelFinished < 8; i++) {
          await Future<void>.delayed(const Duration(milliseconds: 1));
        }
        expect(cancelFinished, 8);
        if (closeFails) {
          await expectLater(memory.state(binding), throwsA(failure()));
          expect(clients.length, 8);
        } else {
          expect((await memory.state(binding)).binding, binding);
          expect(clients.length, 9);
        }
        await memory.close();
        expect(clients.every((c) => c.closes == 1), true);
        for (final stream in streams) {
          await stream.close();
        }
      }
    },
  );
  testWidgets('successful EOF cancellation restores capacity inside fakeAsync', (
    tester,
  ) async {
    final clients = <CountedMockClient>[];
    final memory = MemoryApi(
      sessionBase: 'http://localhost:8761',
      converseBase: 'https://converse.example',
      ownerToken: 'token',
      clientFactory: () {
        final c = CountedMockClient(
          (request) async => http.Response(
            jsonEncode(fixture.stateJson()),
            200,
            headers: {'content-type': 'application/json'},
          ),
        );
        clients.add(c);
        return c;
      },
    );
    // No runAsync or external-zone drain: ordinary widget requests must release
    // each fully read/closed response before the eighth-owner safety cap.
    for (var i = 0; i < 9; i++) {
      expect((await memory.state(binding)).binding, binding);
      expect(clients.length, i + 1);
      expect(clients.every((c) => c.closes == 1), true);
    }
    await memory.close();
    expect(clients.length, 9);
    expect(clients.every((c) => c.closes == 1), true);
  });
}
