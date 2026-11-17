import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/ui/agent_runtime_writes.dart';

import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, sentBody;
import 'agent_runtime_settings_test.dart';
import 'agent_runtime_transport_test.dart' show runtimeApi;
import 'agent_runtime_writes_test.dart' show awaitRuntimeOwners, draftRequest;

void main() {
  test(
    'late acquisition closes its original without send or false uncertainty',
    () async {
      final writes = AgentRuntimeWrites(
        displayDeadline: const Duration(milliseconds: 2),
      );
      final client = ModelTestClient(
        (_) async => throw StateError('Late acquisition must not send'),
      );
      final api = runtimeApi(() {
        final elapsed = Stopwatch()..start();
        while (elapsed.elapsed < const Duration(milliseconds: 30)) {}
        return client;
      }, timeoutMs: 15);
      final scope = writes.open(api, runtimeSnapshot())!;
      final operation = writes.begin(scope, api, draftRequest(scope))!;
      addTearDown(() async {
        await api.close();
        writes.dispose();
      });
      expect(
        await writes.retry(operation, api, scope.snapshot.writeAuthority),
        isNull,
      );
      await awaitRuntimeOwners(operation, mutations: 0, lookups: 0);
      expect(client.sends, 0);
      expect(client.closes, 1);
      expect(scope.pending, same(operation));
      expect(operation.uncertain, isFalse);
      expect(scope.notice, isNull);
      expect(operation.notice, contains('not sent'));
    },
  );

  test(
    'unobserved fence retains the original and cannot settle unknown state',
    () async {
      final writes = AgentRuntimeWrites(
        displayDeadline: const Duration(milliseconds: 1),
      );
      final response = Completer<http.StreamedResponse>();
      final requests = <http.BaseRequest>[];
      final client = ModelTestClient((request) {
        requests.add(request);
        return response.future;
      });
      // Ordinary acquisition budget establishes the sole actual lookup. Its
      // response remains held until after the real result deadline.
      final api = runtimeApi(() => client);
      final scope = writes.open(api, runtimeSnapshot())!;
      final operation = writes.begin(scope, api, draftRequest(scope))!;
      addTearDown(() async {
        if (!response.isCompleted) {
          response.complete(
            modelResponse(
              runtimeLookupJson(
                operation.request.body,
                recorded: false,
                fenced: true,
              ),
            ),
          );
        }
        await awaitRuntimeOwners(operation, mutations: 0, lookups: 0);
        await api.close();
        writes.dispose();
      });
      final result = await writes.lookup(
        operation,
        api,
        scope.snapshot.writeAuthority,
      );
      expect(client.sends, 1);
      expect(requests.single.url.path.endsWith('/lookup'), isTrue);
      expect(sentBody(requests.single), operation.request.body);
      expect(operation.lookupOwners, 1);
      expect(scope.pending, same(operation));
      // The planned native fence has not crossed the original transport yet.
      expect(result, isNull);
      expect(operation.uncertain, isTrue);
      response.complete(
        modelResponse(
          runtimeLookupJson(
            operation.request.body,
            recorded: false,
            fenced: true,
          ),
        ),
      );
      await awaitRuntimeOwners(operation, mutations: 0, lookups: 0);
      expect(scope.pending, same(operation));
      expect(client.sends, 1);
      expect(client.closes, 1);
    },
  );
}
