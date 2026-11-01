import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/agent_model_client.dart';
import 'package:kinaiya_flutter/api/agent_model_settings.dart';
import 'package:kinaiya_flutter/api/agent_runtime_client.dart';
import 'package:kinaiya_flutter/api/agent_runtime_settings.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'agent_model_settings_test.dart' as model;
import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, modelApi, sentBody;
import 'agent_runtime_settings_test.dart';

AgentRuntimeApi runtimeApi(
  http.Client Function() factory, {
  String token = 'synthetic-runtime-owner',
  String base = 'https://runtime-host.invalid/agent',
  int timeoutMs = 1000,
}) => AgentRuntimeApi(
  baseUrl: base,
  ownerToken: token,
  clientFactory: factory,
  resultTimeout: Duration(milliseconds: timeoutMs),
  cleanupTimeout: const Duration(milliseconds: 10),
);

void main() {
  test(
    'typed runtime POST and full lookup preserve canonical bytes, ownership and typed status',
    () async {
      final clients = <ModelTestClient>[];
      final sent = <Map<String, Object?>>[];
      final api = runtimeApi(() {
        final client = ModelTestClient((request) async {
          expect(request.method, 'POST');
          expect(request.followRedirects, isFalse);
          expect(
            request.headers['Authorization'],
            'Bearer synthetic-runtime-owner',
          );
          expect(request.headers['Accept-Encoding'], 'identity');
          expect(request.headers['Content-Type'], 'application/json');
          final body = sentBody(request);
          sent.add(body);
          return modelResponse(
            request.url.path.endsWith('/lookup')
                ? runtimeLookupJson(body)
                : runtimeReceiptJson(body),
          );
        });
        clients.add(client);
        return client;
      });
      final original = AgentRuntimeChange.fromJson(
        runtimeChangeJson(directory: './模型//./folder/'),
      );
      final mutation = api.change(original);
      expect((await mutation.result).json, runtimeReceiptJson(original.body));
      await mutation.settled;
      final lookup = api.lookup(original);
      expect(
        (await lookup.result).receipt!.json,
        runtimeReceiptJson(original.body),
      );
      await lookup.settled;
      expect(sent, [original.body, original.body]);
      expect(clients.every((c) => c.sends == 1 && c.closes == 1), isTrue);
      await api.close();
      final refusal = runtimeApi(
        () => ModelTestClient(
          (_) async =>
              modelResponse({'private': 'DO NOT DISPLAY'}, status: 409),
        ),
      );
      final attempt = refusal.change(original);
      await expectLater(
        attempt.result,
        throwsA(
          isA<AgentRuntimeFailure>()
              .having((x) => x.status, 'actual HTTP status', 409)
              .having((x) => x.unconfirmed, 'dispatched', isTrue)
              .having(
                (x) => x.message,
                'fixed diagnostic',
                'Execution settings request was refused',
              ),
        ),
      );
      await attempt.settled;
      await refusal.close();
    },
  );

  test(
    'runtime facade uses dedicated injection and never falls back to real or shared HTTP',
    () async {
      for (final mock in [false, true]) {
        final shared = ModelTestClient(
          (_) => throw StateError('No shared request'),
        );
        final api = mock
            ? KinaiyaClient.mock(
                sessionBase: 'https://synthetic.invalid',
                agentBase: 'https://synthetic.invalid',
                converseBase: 'https://synthetic.invalid',
                mockTransport: shared,
              )
            : KinaiyaClient(
                sessionBase: 'https://synthetic.invalid',
                agentBase: 'https://synthetic.invalid',
                converseBase: 'https://synthetic.invalid',
                ownerToken: 'synthetic-owner',
                httpClient: shared,
              );
        for (final result in [
          () => api.saveAgentRuntimeSettings(runtimeChangeJson()),
          () => api.lookupAgentRuntimeSettings(runtimeChangeJson()),
          () => api.agentRuntimeSettingsReceipt('synthetic-key'),
        ]) {
          await expectLater(result(), throwsA(isA<AgentRuntimeFailure>()));
        }
        expect(shared.sends, 0);
        api.close();
      }
      final shared = ModelTestClient(
        (_) => throw StateError('No shared request'),
      );
      final dedicated = <ModelTestClient>[];
      final api = KinaiyaClient.mock(
        sessionBase: 'https://synthetic.invalid',
        agentBase: 'https://synthetic.invalid',
        converseBase: 'https://synthetic.invalid',
        mockTransport: shared,
        agentRuntimeClientFactory: () {
          final client = ModelTestClient((request) async {
            if (request.method == 'GET') {
              return modelResponse({'schema_version': '1.0'});
            }
            final body = sentBody(request);
            return modelResponse(
              request.url.path.endsWith('/lookup')
                  ? runtimeLookupJson(body)
                  : runtimeReceiptJson(body),
            );
          });
          dedicated.add(client);
          return client;
        },
      );
      expect(
        await api.saveAgentRuntimeSettings(runtimeChangeJson()),
        runtimeReceiptJson(runtimeChangeJson()),
      );
      expect(
        (await api.lookupAgentRuntimeSettings(runtimeChangeJson()))['status'],
        'recorded',
      );
      expect(await api.agentRuntimeSettingsReceipt('synthetic-key'), {
        'schema_version': '1.0',
      });
      await expectLater(
        api.agentRuntimeSettingsReceipt('a/b'),
        throwsA(isA<AgentRuntimeFailure>()),
      );
      expect(dedicated.length, 3);
      expect(shared.sends, 0);
      expect(dedicated.every((c) => c.closes == 1), isTrue);
      api.close();
    },
  );

  test(
    'runtime malformed, encoded, oversized and noncanonical responses cannot prove a save',
    () async {
      final change = AgentRuntimeChange.fromJson(runtimeChangeJson());
      final cases = <http.StreamedResponse Function()>[
        () => modelResponse({}, status: 302),
        () => http.StreamedResponse(
          Stream.value([0xff]),
          200,
          headers: {'content-type': 'application/json'},
        ),
        () => http.StreamedResponse(
          Stream.value(utf8.encode('{"x":1,"x":2}')),
          200,
          headers: {'content-type': 'application/json'},
        ),
        () => http.StreamedResponse(
          Stream.value(utf8.encode('{"x":1e999}')),
          200,
          headers: {'content-type': 'application/json'},
        ),
        () => http.StreamedResponse(
          Stream.value(List.filled(16385, 32)),
          200,
          headers: {'content-type': 'application/json'},
        ),
        () => http.StreamedResponse(
          Stream.value(
            utf8.encode(jsonEncode(runtimeReceiptJson(change.body))),
          ),
          200,
          headers: {
            'content-type': 'application/json',
            'content-encoding': 'gzip',
          },
        ),
        () => http.StreamedResponse(
          Stream.value(
            utf8.encode(jsonEncode(runtimeReceiptJson(change.body))),
          ),
          200,
          contentLength: 1,
          headers: {'content-type': 'application/json'},
        ),
        () => modelResponse({
          ...runtimeReceiptJson(change.body),
          'backend_id': runtimeBackendB,
        }),
      ];
      for (final response in cases) {
        final client = ModelTestClient((_) async => response());
        final api = runtimeApi(() => client);
        final attempt = api.change(change);
        await expectLater(attempt.result, throwsA(isA<AgentRuntimeFailure>()));
        await attempt.settled;
        expect(client.sends, 1);
        expect(client.closes, 1);
        await api.close();
      }
    },
  );

  test(
    'combined32 covers both domains and retains hidden late owners across facade close',
    () async {
      final held = <Completer<http.StreamedResponse>>[];
      final settlements = <Future<void>>[],
          results = <Future<void>>[],
          closes = <Future<void> Function()>[];
      for (var i = 0; i < 32; i++) {
        final response = Completer<http.StreamedResponse>();
        held.add(response);
        if (i.isEven) {
          final api = runtimeApi(
            () => ModelTestClient((_) => response.future),
            timeoutMs: 20,
          );
          final attempt = api.lookup(
            AgentRuntimeChange.fromJson(runtimeChangeJson()),
          );
          settlements.add(attempt.settled);
          results.add(
            expectLater(attempt.result, throwsA(isA<AgentRuntimeFailure>())),
          );
          closes.add(api.close);
        } else {
          final api = modelApi(
            () => ModelTestClient((_) => response.future),
            timeoutMs: 20,
          );
          final attempt = api.lookup(
            AgentModelChange.fromJson(model.changeJson()),
          );
          settlements.add(attempt.settled);
          results.add(
            expectLater(attempt.result, throwsA(isA<AgentModelFailure>())),
          );
          closes.add(api.close);
        }
      }
      await Future.wait(results);
      await Future.wait(closes.map((close) => close()));
      var opens = 0, settledCount = 0;
      for (final future in settlements) {
        unawaited(future.then((_) => settledCount++));
      }
      final runtime = runtimeApi(() {
        opens++;
        return ModelTestClient(
          (request) async =>
              modelResponse(runtimeReceiptJson(sentBody(request))),
        );
      });
      final models = modelApi(() {
        opens++;
        return ModelTestClient(
          (request) async =>
              modelResponse(model.receiptJson(sentBody(request))),
        );
      });
      await expectLater(
        runtime.change(AgentRuntimeChange.fromJson(runtimeChangeJson())).result,
        throwsA(isA<AgentRuntimeFailure>()),
      );
      await expectLater(
        models.change(AgentModelChange.fromJson(model.changeJson())).result,
        throwsA(isA<AgentModelFailure>()),
      );
      expect(opens, 0);
      expect(settledCount, 0);
      for (var i = 0; i < held.length; i++) {
        held[i].complete(
          modelResponse(
            i.isEven
                ? runtimeLookupJson(runtimeChangeJson())
                : model.lookupJson(model.changeJson()),
          ),
        );
      }
      await Future.wait(settlements).timeout(const Duration(seconds: 1));
      expect(settledCount, 32);
      final post = runtime.change(
        AgentRuntimeChange.fromJson(runtimeChangeJson()),
      );
      await post.result;
      await post.settled;
      final modelPost = models.change(
        AgentModelChange.fromJson(model.changeJson()),
      );
      await modelPost.result;
      await modelPost.settled;
      expect(opens, 2);
      await runtime.close();
      await models.close();
    },
  );

  test(
    'total runtime deadline includes cleanup and actual cancellation retains its slot',
    () async {
      final cancelled = Completer<void>();
      final stream = StreamController<List<int>>(
        onCancel: () => cancelled.future,
      );
      final client = ModelTestClient(
        (_) async => http.StreamedResponse(stream.stream, 409),
      );
      final api = AgentRuntimeApi(
        baseUrl: 'https://synthetic.invalid',
        ownerToken: 'synthetic-owner',
        clientFactory: () => client,
        resultTimeout: const Duration(milliseconds: 30),
        cleanupTimeout: const Duration(milliseconds: 500),
      );
      final attempt = api.change(
        AgentRuntimeChange.fromJson(runtimeChangeJson()),
      );
      final elapsed = Stopwatch()..start();
      await expectLater(attempt.result, throwsA(isA<AgentRuntimeFailure>()));
      expect(elapsed.elapsedMilliseconds, lessThan(300));
      var settled = false;
      unawaited(attempt.settled.then((_) => settled = true));
      await Future<void>.delayed(Duration.zero);
      expect(settled, isFalse);
      cancelled.complete();
      await attempt.settled.timeout(const Duration(seconds: 1));
      expect(client.closes, 1);
      await api.close();
      unawaited(stream.close());
    },
  );
}
