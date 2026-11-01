import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/agent_control.dart';
import 'package:kinaiya_flutter/api/agent_control_client.dart';
import 'package:kinaiya_flutter/api/agent_model_settings.dart';
import 'package:kinaiya_flutter/api/agent_runtime_settings.dart';
import 'package:kinaiya_flutter/api/client.dart';
import 'agent_control_test.dart';
import 'agent_model_settings_test.dart' show changeJson;
import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, modelApi, sentBody;
import 'agent_runtime_settings_test.dart' show runtimeChangeJson;
import 'agent_runtime_transport_test.dart' show runtimeApi;

AgentControlApi controlApi(
  http.Client Function() factory, {
  String token = 'synthetic-control-owner',
  String base = 'https://session-host.invalid/session',
  int timeoutMs = 1000,
}) => AgentControlApi(
  baseUrl: base,
  ownerToken: token,
  clientFactory: factory,
  resultTimeout: Duration(milliseconds: timeoutMs),
  cleanupTimeout: const Duration(milliseconds: 10),
);

void main() {
  test(
    'Session GET/change/full-original lookup own one transport each',
    () async {
      final clients = <ModelTestClient>[];
      final paths = <String>[];
      final original = AgentModeChange.fromJson(modeChangeJson());
      final api = controlApi(() {
        final client = ModelTestClient((request) async {
          paths.add(request.url.path);
          expect(request.url.host, 'session-host.invalid');
          expect(request.followRedirects, isFalse);
          expect(
            request.headers['Authorization'],
            'Bearer synthetic-control-owner',
          );
          expect(request.headers['Accept-Encoding'], 'identity');
          if (request.method == 'GET') {
            expect((request as http.Request).bodyBytes, isEmpty);
            return modelResponse(controlStateJson());
          }
          expect(request.headers['Content-Type'], 'application/json');
          expect(sentBody(request), original.body);
          return modelResponse(
            request.url.path.endsWith('/lookup')
                ? modeLookupJson(original.body)
                : modeReceiptJson(original.body),
          );
        });
        clients.add(client);
        return client;
      });
      final read = api.snapshot();
      expect((await read.result).mode, AgentMode.onDemand);
      await read.settled;
      final change = api.change(original);
      expect((await change.result).request, same(original));
      await change.settled;
      final lookup = api.lookup(original);
      expect((await lookup.result).receipt?.request, same(original));
      await lookup.settled;
      expect(paths, [
        '/session/v1/agent-control/state',
        '/session/v1/agent-control/changes',
        '/session/v1/agent-control/changes/lookup',
      ]);
      await api.close();
      expect(
        clients.every((client) => client.sends == 1 && client.closes == 1),
        isTrue,
      );
    },
  );

  test(
    'malformed/private responses never become proof, retry, or input echo',
    () async {
      final original = AgentModeChange.fromJson(modeChangeJson());
      http.StreamedResponse raw(
        List<int> bytes, {
        Map<String, String>? headers,
        int? length,
      }) => http.StreamedResponse(
        Stream.value(bytes),
        200,
        headers: headers ?? {'content-type': 'application/json'},
        contentLength: length,
      );
      final good = jsonEncode(modeReceiptJson(original.body));
      final cases = <http.StreamedResponse Function()>[
        () => modelResponse({'private': 'PRIVATE ERROR'}, status: 409),
        () => http.StreamedResponse(
          Stream.value(utf8.encode('PRIVATE REDIRECT')),
          302,
          headers: {'location': 'https://foreign.invalid'},
        ),
        () => raw([0xff]),
        () => raw(
          utf8.encode(
            good.replaceFirst(
              '"schema_version":"1.0"',
              '"schema_version":"1.0","schema_version":"1.0"',
            ),
          ),
        ),
        () => raw(utf8.encode('$good {}')),
        () => raw(
          utf8.encode(good.replaceFirst('"revision":1', '"revision":1.0')),
        ),
        () => raw(
          utf8.encode(
            good.replaceFirst('"revision":1', '"revision":9007199254740992'),
          ),
        ),
        () => raw(utf8.encode(good.replaceFirst('"mode_saved"', '"\\ud800"'))),
        () => raw(List.filled(16385, 32)),
        () => raw(
          utf8.encode(good),
          headers: {
            'content-type': 'application/json',
            'content-encoding': 'gzip',
          },
        ),
        () => raw(utf8.encode(good), headers: {'content-type': 'text/plain'}),
        () => raw(utf8.encode(good), length: 1),
        () => modelResponse({
          ...modeReceiptJson(original.body),
          'backend_id': controlBackendB,
        }),
      ];
      for (final response in cases) {
        final client = ModelTestClient((_) async => response());
        final api = controlApi(() => client);
        final attempt = api.change(original);
        await expectLater(
          attempt.result,
          throwsA(
            isA<AgentControlFailure>()
                .having((e) => e.unconfirmed, 'dispatched', isTrue)
                .having(
                  (e) => e.toString(),
                  'private body omitted',
                  isNot(contains('PRIVATE')),
                ),
          ),
        );
        await attempt.settled;
        expect(client.sends, 1);
        expect(client.closes, 1);
        await api.close();
      }
    },
  );

  test(
    'typed conflict status is preserved without reading error contents',
    () async {
      var listened = 0;
      final api = controlApi(
        () => ModelTestClient(
          (_) async => http.StreamedResponse(
            Stream<List<int>>.multi((controller) {
              listened++;
              controller.close();
            }),
            409,
          ),
        ),
      );
      final attempt = api.change(AgentModeChange.fromJson(modeChangeJson()));
      await expectLater(
        attempt.result,
        throwsA(
          isA<AgentControlFailure>().having((e) => e.status, 'status', 409),
        ),
      );
      await attempt.settled;
      expect(
        listened,
        1,
      ); // Single subscription, immediately cancelled on status.
      await api.close();
    },
  );

  test(
    'public timeout and facade close retain late native ownership through actual cancellation',
    () async {
      final response = Completer<http.StreamedResponse>();
      final cancellation = Completer<void>();
      final stream = StreamController<List<int>>(
        onCancel: () => cancellation.future,
      );
      final client = ModelTestClient((_) => response.future);
      final api = controlApi(() => client, timeoutMs: 20);
      final attempt = api.change(AgentModeChange.fromJson(modeChangeJson()));
      var settled = false;
      unawaited(attempt.settled.then((_) => settled = true));
      await expectLater(
        attempt.result,
        throwsA(
          isA<AgentControlFailure>().having(
            (e) => e.unconfirmed,
            'uncertain',
            isTrue,
          ),
        ),
      );
      await api.close();
      expect(settled, isFalse);
      expect(client.closes, 1);
      response.complete(
        http.StreamedResponse(
          stream.stream,
          200,
          headers: {'content-type': 'application/json'},
        ),
      );
      await Future<void>.delayed(Duration.zero);
      expect(settled, isFalse);
      cancellation.complete();
      await attempt.settled;
      expect(settled, isTrue);
      expect(client.closes, 1);
      await stream.close();
    },
  );

  test(
    '32 actual owners are combined across all three domains and hidden late cleanup',
    () async {
      final responses = <Completer<http.StreamedResponse>>[];
      final clients = <ModelTestClient>[];
      http.Client factory() {
        final response = Completer<http.StreamedResponse>();
        responses.add(response);
        final client = ModelTestClient((_) => response.future);
        clients.add(client);
        return client;
      }

      final control = controlApi(factory, timeoutMs: 30);
      final model = modelApi(factory, timeoutMs: 30);
      final runtime = runtimeApi(factory, timeoutMs: 30);
      final outcomes = <Future<void>>[], settled = <Future<void>>[];
      void retain(Future<Object?> result, Future<void> actual) {
        outcomes.add(result.then<void>((_) {}, onError: (Object _) {}));
        settled.add(actual);
      }

      for (var i = 0; i < 32; i++) {
        if (i % 3 == 0) {
          final a = control.snapshot();
          retain(a.result, a.settled);
        } else if (i % 3 == 1) {
          final a = model.change(AgentModelChange.fromJson(changeJson()));
          retain(a.result, a.settled);
        } else {
          final a = runtime.change(
            AgentRuntimeChange.fromJson(runtimeChangeJson()),
          );
          retain(a.result, a.settled);
        }
      }
      expect(responses.length, 32);
      for (final capacity in [
        control.hasCapacity,
        model.hasCapacity,
        runtime.hasCapacity,
      ]) {
        expect(capacity, isFalse);
      }
      final refused = control.snapshot();
      await expectLater(
        refused.result,
        throwsA(
          isA<AgentControlFailure>().having(
            (e) => e.unconfirmed,
            'never sent',
            isFalse,
          ),
        ),
      );
      await refused.settled;
      await Future.wait(outcomes);
      await Future.wait([control.close(), model.close(), runtime.close()]);
      expect(clients.every((client) => client.closes == 1), isTrue);
      expect(responses.length, 32);
      final fresh = controlApi(factory);
      expect(fresh.hasCapacity, isFalse);
      for (final response in responses) {
        response.complete(modelResponse(controlStateJson()));
      }
      await Future.wait(settled);
      expect(fresh.hasCapacity, isTrue);
      await fresh.close();
    },
  );

  test(
    'facade uses Session factory and an injected facade never opens fallback HTTP',
    () async {
      final generic = ModelTestClient(
        (_) async =>
            throw StateError('Generic HTTP must not own mode requests'),
      );
      final client = KinaiyaClient(
        sessionBase: 'https://session.invalid/prefix/',
        agentBase: 'https://agent.invalid',
        converseBase: 'https://converse.invalid',
        ownerToken: 'synthetic-owner',
        httpClient: generic,
      );
      final missing = client.agentControl.snapshot();
      await expectLater(
        missing.result,
        throwsA(
          isA<AgentControlFailure>().having(
            (e) => e.unconfirmed,
            'no dispatch',
            isFalse,
          ),
        ),
      );
      await missing.settled;
      expect(generic.sends, 0);
      client.close();
      final mock = KinaiyaClient.mock(
        sessionBase: 'https://session.invalid/prefix/',
        agentBase: 'https://agent.invalid',
        converseBase: 'https://converse.invalid',
        mockTransport: generic,
        agentControlClientFactory: () => ModelTestClient((request) async {
          expect(
            request.url.toString(),
            'https://session.invalid/prefix/v1/agent-control/state',
          );
          return modelResponse(controlStateJson());
        }),
      );
      final read = mock.agentControl.snapshot();
      expect((await read.result).backendId, controlBackendA);
      await read.settled;
      mock.close();
    },
  );
}
