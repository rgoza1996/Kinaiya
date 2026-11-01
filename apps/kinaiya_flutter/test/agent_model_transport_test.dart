import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/agent_model_client.dart';
import 'package:kinaiya_flutter/api/agent_model_settings.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'agent_model_settings_test.dart';

class ModelTestClient extends http.BaseClient {
  ModelTestClient(this.handler, {this.closeThrows = false});
  final Future<http.StreamedResponse> Function(http.BaseRequest) handler;
  final bool closeThrows;
  int sends = 0, closes = 0;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    sends++;
    return handler(request);
  }

  @override
  void close() {
    closes++;
    if (closeThrows) throw StateError('SYNTHETIC PRIVATE TRANSPORT DETAIL');
  }
}

http.StreamedResponse modelResponse(Object? body, {int status = 200}) =>
    http.StreamedResponse(
      Stream.value(utf8.encode(jsonEncode(body))),
      status,
      headers: {'content-type': 'application/json'},
    );
AgentModelApi modelApi(
  http.Client Function() factory, {
  String token = 'synthetic-owner',
  String base = 'https://host.invalid/agent',
  int timeoutMs = 1000,
}) => AgentModelApi(
  baseUrl: base,
  ownerToken: token,
  clientFactory: factory,
  resultTimeout: Duration(milliseconds: timeoutMs),
  cleanupTimeout: const Duration(milliseconds: 10),
);
Map<String, Object?> sentBody(http.BaseRequest request) =>
    jsonDecode((request as http.Request).body) as Map<String, Object?>;

void main() {
  test(
    'independent owned transports send exact bound POST/lookup and close once',
    () async {
      final clients = <ModelTestClient>[];
      final requests = <Map<String, Object?>>[];
      final api = modelApi(() {
        final client = ModelTestClient((request) async {
          expect(request.followRedirects, isFalse);
          expect(request.headers['Authorization'], 'Bearer synthetic-owner');
          expect(request.headers['Accept-Encoding'], 'identity');
          expect(request.headers['Content-Type'], 'application/json');
          expect(request.method, 'POST');
          final body = sentBody(request);
          requests.add(body);
          return modelResponse(
            request.url.path.endsWith('/lookup')
                ? lookupJson(body)
                : receiptJson(body),
          );
        });
        clients.add(client);
        return client;
      });
      for (final purpose in <String?>[null, ...agentModelPurposes]) {
        final change = AgentModelChange.fromJson(changeJson(purpose: purpose));
        final post = api.change(change);
        expect((await post.result).json, receiptJson(change.body));
        await post.settled;
        final lookup = api.lookup(change);
        expect((await lookup.result).receipt, isNotNull);
        await lookup.settled;
        expect(requests[requests.length - 1], change.body);
        expect(requests[requests.length - 2], change.body);
      }
      await api.close();
      expect(
        clients.every((client) => client.sends == 1 && client.closes == 1),
        isTrue,
      );
    },
  );

  test(
    'malformed responses, redirects and private error bodies never become proofs or diagnostics',
    () async {
      final change = AgentModelChange.fromJson(changeJson());
      final cases = <http.StreamedResponse Function()>[
        () => http.StreamedResponse(
          Stream.value(utf8.encode('PRIVATE ERROR')),
          409,
        ),
        () => http.StreamedResponse(
          Stream.value(utf8.encode('PRIVATE REDIRECT')),
          302,
          headers: {'location': 'https://foreign.invalid'},
        ),
        () => http.StreamedResponse(
          Stream.value([0xff]),
          200,
          headers: {'content-type': 'application/json'},
        ),
        () => http.StreamedResponse(
          Stream.value(
            utf8.encode(
              jsonEncode(receiptJson(change.body)).replaceFirst(
                '"schema_version":"1.1"',
                '"schema_version":"1.1","schema_version":"1.1"',
              ),
            ),
          ),
          200,
          headers: {'content-type': 'application/json'},
        ),
        () => http.StreamedResponse(
          Stream.value(utf8.encode('{"revision":1e999}')),
          200,
          headers: {'content-type': 'application/json'},
        ),
        () => http.StreamedResponse(
          Stream.value(List.filled(16385, 32)),
          200,
          headers: {'content-type': 'application/json'},
        ),
        () => http.StreamedResponse(
          Stream.value(utf8.encode(jsonEncode(receiptJson(change.body)))),
          200,
          headers: {
            'content-type': 'application/json',
            'content-encoding': 'gzip',
          },
        ),
        () => http.StreamedResponse(
          Stream.value(utf8.encode(jsonEncode(receiptJson(change.body)))),
          200,
          contentLength: 1,
          headers: {'content-type': 'application/json'},
        ),
        () => modelResponse({
          ...receiptJson(change.body),
          'backend_id': backendB,
        }),
      ];
      for (final response in cases) {
        final client = ModelTestClient((_) async => response());
        final api = modelApi(() => client);
        final attempt = api.change(change);
        await expectLater(
          attempt.result,
          throwsA(
            isA<AgentModelFailure>().having(
              (error) => error.toString(),
              'fixed diagnostic',
              isNot(contains('PRIVATE')),
            ),
          ),
        );
        await attempt.settled;
        await api.close();
        expect(client.sends, 1);
        expect(client.closes, 1);
      }
    },
  );

  test(
    'public deadline precedes stalled send; late response remains owned and closes once',
    () async {
      final response = Completer<http.StreamedResponse>();
      final client = ModelTestClient((_) => response.future);
      final api = modelApi(() => client, timeoutMs: 20);
      final change = AgentModelChange.fromJson(changeJson());
      final attempt = api.change(change);
      var settled = false;
      unawaited(attempt.settled.then((_) => settled = true));
      await expectLater(attempt.result, throwsA(isA<AgentModelFailure>()));
      expect(settled, isFalse);
      expect(client.closes, 1);
      await api.close();
      response.complete(modelResponse(receiptJson(change.body)));
      await attempt.settled.timeout(const Duration(seconds: 1));
      expect(client.closes, 1);
    },
  );

  test(
    'actual stream cancellation retains capacity beyond cleanup and result deadlines',
    () async {
      final cancellation = Completer<void>();
      final stream = StreamController<List<int>>(
        onCancel: () => cancellation.future,
      );
      final client = ModelTestClient(
        (_) async => http.StreamedResponse(
          stream.stream,
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      final api = modelApi(() => client, timeoutMs: 20);
      final attempt = api.lookup(AgentModelChange.fromJson(changeJson()));
      var settled = false;
      unawaited(attempt.settled.then((_) => settled = true));
      await expectLater(attempt.result, throwsA(isA<AgentModelFailure>()));
      await api.close();
      expect(settled, isFalse);
      cancellation.complete();
      await attempt.settled.timeout(const Duration(seconds: 1));
      expect(client.closes, 1);
      unawaited(stream.close());
    },
  );

  test(
    'expired synchronous acquisition cannot dispatch or return a late success',
    () async {
      final client = ModelTestClient(
        (request) async => modelResponse(receiptJson(sentBody(request))),
      );
      final api = AgentModelApi(
        baseUrl: 'https://host.invalid',
        ownerToken: 'synthetic-owner',
        resultTimeout: const Duration(milliseconds: 5),
        clientFactory: () {
          final elapsed = Stopwatch()..start();
          while (elapsed.elapsedMilliseconds < 15) {
            /* Synthetic synchronous factory delay. */
          }
          return client;
        },
      );
      final attempt = api.change(AgentModelChange.fromJson(changeJson()));
      await expectLater(attempt.result, throwsA(isA<AgentModelFailure>()));
      await attempt.settled;
      expect(attempt.dispatched, isFalse);
      expect(client.sends, 0);
      expect(client.closes, 1);
      await api.close();
    },
  );

  test(
    'total result deadline includes a stalled awaited cleanup phase',
    () async {
      final cancellation = Completer<void>();
      final stream = StreamController<List<int>>(
        onCancel: () => cancellation.future,
      );
      final client = ModelTestClient(
        (_) async => http.StreamedResponse(stream.stream, 409),
      );
      final api = AgentModelApi(
        baseUrl: 'https://host.invalid',
        ownerToken: 'synthetic-owner',
        clientFactory: () => client,
        resultTimeout: const Duration(milliseconds: 30),
        cleanupTimeout: const Duration(milliseconds: 500),
      );
      final attempt = api.change(AgentModelChange.fromJson(changeJson()));
      final elapsed = Stopwatch()..start();
      await expectLater(attempt.result, throwsA(isA<AgentModelFailure>()));
      expect(elapsed.elapsedMilliseconds, lessThan(300));
      var settled = false;
      unawaited(attempt.settled.then((_) => settled = true));
      await Future<void>.delayed(Duration.zero);
      expect(settled, isFalse);
      cancellation.complete();
      await attempt.settled.timeout(const Duration(seconds: 1));
      await api.close();
      expect(client.closes, 1);
      unawaited(stream.close());
    },
  );

  test(
    'global32 includes lookup and late sends across API replacement; genuine settlement restores capacity',
    () async {
      final held = <Completer<http.StreamedResponse>>[];
      final attempts = <AgentModelAttempt<AgentModelLookup>>[];
      final waits = <Future<void>>[];
      final change = AgentModelChange.fromJson(changeJson());
      for (var i = 0; i < 32; i++) {
        final response = Completer<http.StreamedResponse>();
        held.add(response);
        final api = modelApi(
          () => ModelTestClient((_) => response.future),
          timeoutMs: 20,
        );
        final attempt = api.lookup(change);
        attempts.add(attempt);
        waits.add(
          expectLater(attempt.result, throwsA(isA<AgentModelFailure>())),
        );
      }
      await Future.wait(waits);
      var opened = 0;
      final replacement = modelApi(() {
        opened++;
        return ModelTestClient(
          (_) async => modelResponse(receiptJson(change.body)),
        );
      });
      await expectLater(
        replacement.change(change).result,
        throwsA(isA<AgentModelFailure>()),
      );
      expect(opened, 0);
      for (final response in held) {
        response.complete(modelResponse(lookupJson(change.body)));
      }
      await Future.wait(
        attempts.map((attempt) => attempt.settled),
      ).timeout(const Duration(seconds: 1));
      final recovered = replacement.change(change);
      await recovered.result;
      await recovered.settled;
      expect(opened, 1);
      await replacement.close();
    },
  );

  test(
    'both facade injection constructors refuse missing dedicated factory without shared transport use',
    () async {
      for (final mockConstructor in [false, true]) {
        final shared = ModelTestClient(
          (_) => throw StateError('Shared transport must not be used'),
        );
        final api = mockConstructor
            ? KinaiyaClient.mock(
                sessionBase: 'https://host.invalid',
                agentBase: 'https://host.invalid',
                converseBase: 'https://host.invalid',
                mockTransport: shared,
              )
            : KinaiyaClient(
                sessionBase: 'https://host.invalid',
                agentBase: 'https://host.invalid',
                converseBase: 'https://host.invalid',
                ownerToken: 'synthetic-owner',
                httpClient: shared,
              );
        await expectLater(
          api.saveAgentModels(changeJson()),
          throwsA(isA<AgentModelFailure>()),
        );
        await expectLater(
          api.lookupAgentModels(changeJson()),
          throwsA(isA<AgentModelFailure>()),
        );
        await expectLater(
          api.agentModelReceipt('synthetic-key'),
          throwsA(isA<AgentModelFailure>()),
        );
        expect(shared.sends, 0);
        api.close();
      }
    },
  );

  test(
    'failed actual client close does not claim successful cleanup',
    () async {
      final change = AgentModelChange.fromJson(changeJson());
      final client = ModelTestClient(
        (_) async => modelResponse(receiptJson(change.body)),
        closeThrows: true,
      );
      final api = modelApi(() => client);
      final attempt = api.change(change);
      var settled = false;
      unawaited(attempt.settled.then((_) => settled = true));
      await expectLater(attempt.result, throwsA(isA<AgentModelFailure>()));
      expect(settled, isFalse);
      await api.close();
      expect(client.closes, 1);
    },
  );
}
