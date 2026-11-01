import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/agent_model_client.dart';
import 'package:kinaiya_flutter/api/agent_model_settings.dart';
import 'package:kinaiya_flutter/state/agent_model_writes.dart';
import 'agent_model_settings_test.dart';
import 'agent_model_transport_test.dart'
    show ModelTestClient, modelApi, modelResponse, sentBody;

void main() {
  test(
    'owner and backend switches hide original operation; same scope reconnect preserves it',
    () async {
      final held = Completer<http.StreamedResponse>();
      final firstClient = ModelTestClient((_) => held.future);
      final original = modelApi(() => firstClient, timeoutMs: 20);
      final writes = AgentModelWrites(
        displayDeadline: const Duration(milliseconds: 5),
      );
      final request = AgentModelChange.fromJson(changeJson());
      final op = writes.begin(original, authority(), request)!;
      final sending = writes.retry(op, original, authority());
      await sending;
      expect(op.uncertain, isTrue);
      var otherRequests = 0;
      final otherOwner = modelApi(() {
        otherRequests++;
        return ModelTestClient((_) async => modelResponse({}));
      }, token: 'another-synthetic-owner');
      expect(writes.visible(otherOwner, authority()), isNull);
      expect(writes.visible(original, authority(backend: backendB)), isNull);
      expect(await writes.retry(op, otherOwner, authority()), isNull);
      expect(await writes.lookup(op, otherOwner, authority()), isNull);
      expect(
        await writes.lookup(op, original, authority(backend: backendB)),
        isNull,
      );
      expect(otherRequests, 0);
      await original.close();
      final sent = <Map<String, Object?>>[];
      final reconnect = modelApi(
        () => ModelTestClient((request) async {
          sent.add(sentBody(request));
          return modelResponse(lookupJson(sentBody(request)));
        }),
      );
      expect(
        identical(
          writes.visible(reconnect, authority(instance: instanceB)),
          op,
        ),
        isTrue,
      );
      expect(
        writes.canRetry(op, reconnect, authority(instance: instanceB)),
        isFalse,
      );
      expect(
        await writes.lookup(op, reconnect, authority(instance: instanceB)),
        AgentModelOutcome.recorded,
      );
      expect(sent.single, request.body);
      expect(op.mutationOwners, 1); // Logical settlement did not fake cleanup.
      held.complete(modelResponse(receiptJson(request.body)));
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(op.mutationOwners, 0);
      await reconnect.close();
      await otherOwner.close();
      writes.dispose();
    },
  );

  test(
    'late original successful callback cannot retire replacement operation',
    () async {
      final held = Completer<http.StreamedResponse>();
      final writes = AgentModelWrites();
      final api = modelApi(
        () => ModelTestClient(
          (request) => request.url.path.endsWith('/lookup')
              ? Future.value(modelResponse(lookupJson(sentBody(request))))
              : held.future,
        ),
      );
      final original = writes.begin(
        api,
        authority(),
        AgentModelChange.fromJson(changeJson()),
      )!;
      final firstResult = writes.retry(original, api, authority());
      expect(
        await writes.lookup(original, api, authority()),
        AgentModelOutcome.recorded,
      );
      final replacement = writes.begin(
        api,
        authority(),
        AgentModelChange.fromJson(changeJson(key: 'replacement', revision: 1)),
      )!;
      held.complete(modelResponse(receiptJson(original.request.body)));
      expect(await firstResult, isNull);
      expect(identical(writes.visible(api, authority()), replacement), isTrue);
      expect(replacement.complete, isFalse);
      await api.close();
      writes.dispose();
    },
  );

  test(
    'timeout then native refusal stays unknown until serialized same-backend fence',
    () async {
      final first = Completer<http.StreamedResponse>();
      var sends = 0, lookupMode = 0;
      final clients = <ModelTestClient>[];
      final api = modelApi(() {
        final client = ModelTestClient((request) async {
          if (request.url.path.endsWith('/lookup')) {
            final body = sentBody(request);
            if (lookupMode == 0) {
              return modelResponse(
                lookupJson(body, recorded: false, fenced: false),
              );
            }
            if (lookupMode == 1) {
              return modelResponse({
                ...lookupJson(body),
                'backend_id': backendB,
              });
            }
            return modelResponse(lookupJson(body, recorded: false));
          }
          sends++;
          if (sends == 1) return first.future;
          return modelResponse('PRIVATE REFUSAL', status: 409);
        });
        clients.add(client);
        return client;
      }, timeoutMs: 20);
      final writes = AgentModelWrites(
        displayDeadline: const Duration(milliseconds: 5),
      );
      final op = writes.begin(
        api,
        authority(),
        AgentModelChange.fromJson(changeJson()),
      )!;
      await writes.retry(op, api, authority());
      expect(op.uncertain, isTrue);
      await writes.retry(op, api, authority());
      expect(op.uncertain, isTrue);
      expect(writes.visible(api, authority()), same(op));
      expect(await writes.lookup(op, api, authority()), isNull);
      lookupMode = 1;
      expect(await writes.lookup(op, api, authority()), isNull);
      expect(writes.visible(api, authority()), same(op));
      lookupMode = 2;
      expect(
        await writes.lookup(op, api, authority()),
        AgentModelOutcome.refused,
      );
      expect(writes.visible(api, authority()), isNull);
      first.complete(modelResponse(receiptJson(op.request.body)));
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(clients.every((client) => client.closes == 1), isTrue);
      await api.close();
      writes.dispose();
    },
  );

  test(
    'POST2 and lookup1 count actual concurrent owners and recover for later manual attempts',
    () async {
      final heldPosts = <Completer<http.StreamedResponse>>[];
      final heldLookups = <Completer<http.StreamedResponse>>[];
      var hold = true;
      final api = modelApi(
        () => ModelTestClient((request) {
          final body = sentBody(request);
          if (!hold) {
            return Future.value(
              modelResponse(
                request.url.path.endsWith('/lookup')
                    ? lookupJson(body, recorded: false, fenced: false)
                    : 'refused',
                status: request.url.path.endsWith('/lookup') ? 200 : 409,
              ),
            );
          }
          final future = Completer<http.StreamedResponse>();
          (request.url.path.endsWith('/lookup') ? heldLookups : heldPosts).add(
            future,
          );
          return future.future;
        }),
        timeoutMs: 20,
      );
      final writes = AgentModelWrites(
        displayDeadline: const Duration(milliseconds: 5),
      );
      final op = writes.begin(
        api,
        authority(),
        AgentModelChange.fromJson(changeJson()),
      )!;
      await writes.retry(op, api, authority());
      await writes.retry(op, api, authority());
      await writes.retry(op, api, authority());
      expect(heldPosts.length, 2);
      expect(op.mutationOwners, 2);
      await writes.lookup(op, api, authority());
      await writes.lookup(op, api, authority());
      expect(heldLookups.length, 1);
      expect(op.lookupOwners, 1);
      for (final response in heldPosts) {
        response.complete(modelResponse('refused', status: 409));
      }
      heldLookups.single.complete(
        modelResponse(
          lookupJson(op.request.body, recorded: false, fenced: false),
        ),
      );
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(op.mutationOwners, 0);
      expect(op.lookupOwners, 0);
      hold = false;
      for (var i = 0; i < 4; i++) {
        await writes.retry(op, api, authority());
        await writes.lookup(op, api, authority());
      }
      // A public result does not promise actual cleanup. Wait for that separate
      // phase before asserting reusable owner capacity.
      final drain = Stopwatch()..start();
      while ((op.mutationOwners != 0 || op.lookupOwners != 0) &&
          drain.elapsedMilliseconds < 200) {
        await Future<void>.delayed(const Duration(milliseconds: 1));
      }
      expect(op.mutationOwners, 0);
      expect(op.lookupOwners, 0);
      expect(op.uncertain, isTrue);
      await api.close();
      writes.dispose();
    },
  );

  test(
    'logical16 never overwrites or evicts hidden scopes and settled slots are reusable',
    () async {
      final writes = AgentModelWrites();
      final apis = <AgentModelApi>[];
      final ops = <PendingAgentModelChange>[];
      for (var i = 0; i < 16; i++) {
        final api = modelApi(
          () => ModelTestClient(
            (request) async =>
                modelResponse(lookupJson(sentBody(request), recorded: false)),
          ),
          token: 'synthetic-$i',
        );
        apis.add(api);
        ops.add(
          writes.begin(
            api,
            authority(),
            AgentModelChange.fromJson(changeJson(key: 'key-$i')),
          )!,
        );
      }
      final extra = modelApi(
        () => throw StateError('Must not open transport'),
        token: 'extra',
      );
      expect(
        writes.begin(
          extra,
          authority(),
          AgentModelChange.fromJson(changeJson()),
        ),
        isNull,
      );
      expect(writes.visible(extra, authority()), isNull);
      expect(
        await writes.lookup(ops.first, apis.first, authority()),
        AgentModelOutcome.refused,
      );
      expect(
        writes.begin(
          extra,
          authority(),
          AgentModelChange.fromJson(changeJson()),
        ),
        isNotNull,
      );
      for (var i = 1; i < 16; i++) {
        expect(writes.visible(apis[i], authority()), same(ops[i]));
      }
      await Future.wait([...apis, extra].map((api) => api.close()));
      writes.dispose();
    },
  );

  test(
    'hidden late mutation owners still consume global32 after lookup logically settles records',
    () async {
      final held = <Completer<http.StreamedResponse>>[];
      final requests = <Map<String, Object?>>[];
      final results = <Future<AgentModelOutcome?>>[];
      final writes = AgentModelWrites();
      final api = modelApi(
        () => ModelTestClient((request) {
          final body = sentBody(request);
          if (request.url.path.endsWith('/lookup')) {
            return Future.value(modelResponse(lookupJson(body)));
          }
          requests.add(body);
          final response = Completer<http.StreamedResponse>();
          held.add(response);
          return response.future;
        }),
      );
      for (var i = 0; i < 32; i++) {
        final op = writes.begin(
          api,
          authority(),
          AgentModelChange.fromJson(changeJson(key: 'key-$i', revision: i)),
        )!;
        results.add(writes.retry(op, api, authority()));
        if (i < 31) {
          expect(
            await writes.lookup(op, api, authority()),
            AgentModelOutcome.recorded,
          );
        } else {
          expect(writes.canLookup(op, api, authority()), isFalse);
        }
      }
      expect(api.hasCapacity, isFalse);
      final replacement = modelApi(
        () => throw StateError('Global admission must refuse'),
      );
      expect(replacement.hasCapacity, isFalse);
      expect(
        writes.begin(
          replacement,
          authority(backend: backendB),
          AgentModelChange.fromJson(changeJson(backend: backendB)),
        ),
        isNull,
      );
      for (var i = 0; i < held.length; i++) {
        held[i].complete(modelResponse(receiptJson(requests[i])));
      }
      final completed = await Future.wait(results);
      expect(completed.take(31).every((result) => result == null), isTrue);
      expect(completed.last, AgentModelOutcome.recorded);
      expect(api.hasCapacity, isTrue);
      await api.close();
      await replacement.close();
      writes.dispose();
    },
  );
}
