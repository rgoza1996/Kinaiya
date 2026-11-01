import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/agent_runtime_client.dart';
import 'package:kinaiya_flutter/api/agent_runtime_settings.dart';
import 'package:kinaiya_flutter/ui/agent_runtime_writes.dart';
import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, sentBody;
import 'agent_runtime_settings_test.dart';
import 'agent_runtime_transport_test.dart' show runtimeApi;

AgentRuntimeChange draftRequest(
  AgentRuntimeWriteScope scope, {
  String key = 'synthetic-key',
}) => AgentRuntimeChange.fromJson({
  ...runtimeChangeJson(
    backend: scope.backendId,
    instance: scope.snapshot.instanceId,
    state: scope.snapshot.stateToken,
    revision: scope.snapshot.revision,
    key: key,
    yolo: scope.draft.yolo,
  ),
  'settings': scope.draft.settings,
});

Future<void> awaitRuntimeOwners(
  PendingAgentRuntimeChange operation, {
  required int mutations,
  required int lookups,
}) async {
  final elapsed = Stopwatch()..start();
  while ((operation.mutationOwners != mutations ||
          operation.lookupOwners != lookups) &&
      elapsed.elapsed < const Duration(seconds: 1)) {
    await Future<void>.delayed(const Duration(milliseconds: 1));
  }
  expect(operation.mutationOwners, mutations);
  expect(operation.lookupOwners, lookups);
}

void main() {
  test(
    'private owner, canonical URL and runtime DB isolate dirty drafts and pending across reconnect',
    () async {
      final registry = AgentRuntimeWrites(
        displayDeadline: const Duration(milliseconds: 2),
      );
      final held = Completer<http.StreamedResponse>();
      final original = runtimeApi(
        () => ModelTestClient((_) => held.future),
        timeoutMs: 15,
      );
      final scope = registry.open(original, runtimeSnapshot())!;
      scope.draft.directory = './folder//./name/';
      scope.draft.yolo = true;
      final draft = scope.draft;
      final operation = registry.begin(scope, original, draftRequest(scope))!;
      await registry.retry(operation, original, scope.snapshot.writeAuthority);
      expect(operation.uncertain, isTrue);
      expect(operation.mutationOwners, 1);
      final otherOwner = runtimeApi(
        () => throw StateError('No dispatch'),
        token: 'other-synthetic-owner',
      );
      final other = registry.open(otherOwner, runtimeSnapshot())!;
      expect(other.pending, isNull);
      expect(other.draft.dirty, isFalse);
      expect(other.notice, isNull);
      final replacement = registry.open(
        original,
        runtimeSnapshot(backend: runtimeBackendB),
      )!;
      expect(replacement.pending, isNull);
      expect(replacement.draft.dirty, isFalse);
      final reconnect = runtimeApi(
        () => ModelTestClient(
          (request) async =>
              modelResponse(runtimeReceiptJson(sentBody(request))),
        ),
        base: 'https://runtime-host.invalid/agent/',
      );
      expect(registry.open(reconnect, runtimeSnapshot()), same(scope));
      expect(scope.pending, same(operation));
      expect(scope.draft, same(draft));
      expect(scope.draft.directory, './folder//./name/');
      expect(
        (operation.request.body['settings'] as Map)['working_directory'],
        'folder/name',
      );
      scope.draft.directory = 'local-form-edit-does-not-change-request';
      expect(
        (operation.request.body['settings'] as Map)['working_directory'],
        'folder/name',
      );
      expect(
        await registry.retry(
          operation,
          reconnect,
          scope.snapshot.writeAuthority,
        ),
        AgentRuntimeOutcome.recorded,
      );
      expect(scope.pending, isNull);
      expect(scope.draft.yolo, isFalse);
      await awaitRuntimeOwners(operation, mutations: 1, lookups: 0);
      expect(operation.mutationOwners, 1);
      final next = registry.begin(
        scope,
        reconnect,
        draftRequest(scope, key: 'replacement-key'),
      )!;
      scope.notice = 'replacement scoped notice';
      held.complete(modelResponse(runtimeReceiptJson(operation.request.body)));
      await Future<void>.delayed(const Duration(milliseconds: 5));
      expect(scope.pending, same(next));
      expect(scope.notice, 'replacement scoped notice');
      expect(operation.mutationOwners, 0);
      await registry.retry(next, reconnect, scope.snapshot.writeAuthority);
      await original.close();
      await reconnect.close();
      await otherOwner.close();
      registry.dispose();
    },
  );

  test(
    'same DB restart is lookup only and positive historical YOLO never changes active values',
    () async {
      final registry = AgentRuntimeWrites();
      final original = runtimeApi(() => throw StateError('No mutation needed'));
      final scope = registry.open(original, runtimeSnapshot())!;
      scope.draft.yolo = true;
      final operation = registry.begin(scope, original, draftRequest(scope))!;
      final requests = <http.BaseRequest>[];
      final restarted = runtimeApi(
        () => ModelTestClient((request) async {
          requests.add(request);
          return modelResponse(runtimeLookupJson(sentBody(request)));
        }),
      );
      final actual = runtimeSnapshot(
        instance: runtimeInstanceB,
        state: 'f' * 64,
        revision: 1,
        writable: false,
        yolo: false,
      );
      expect(registry.open(restarted, actual), same(scope));
      expect(
        registry.canRetry(operation, restarted, actual.writeAuthority),
        isFalse,
      );
      expect(
        registry.canLookup(operation, restarted, actual.writeAuthority),
        isTrue,
      );
      expect(
        await registry.lookup(operation, restarted, actual.writeAuthority),
        AgentRuntimeOutcome.recorded,
      );
      expect(requests.single.url.path.endsWith('/lookup'), isTrue);
      expect(sentBody(requests.single), operation.request.body);
      expect(sentBody(requests.single)['yolo'], isTrue);
      expect(scope.snapshot.yolo, isFalse);
      expect(scope.draft.yolo, isFalse);
      expect(scope.draft.base, same(actual));
      expect(scope.pending, isNull);
      expect(scope.notice, contains('Review the refreshed active values'));
      await original.close();
      await restarted.close();
      registry.dispose();
    },
  );

  test(
    'two actual mutations and one lookup remain owned through public refusal; capacity is reusable after actual settlement',
    () async {
      final registry = AgentRuntimeWrites(
        displayDeadline: const Duration(milliseconds: 1),
      );
      final held = <Completer<http.StreamedResponse>>[];
      final bodies = <Map<String, Object?>>[];
      var success = false;
      final api = runtimeApi(
        () => ModelTestClient((request) async {
          final body = sentBody(request);
          bodies.add(body);
          if (success) return modelResponse(runtimeReceiptJson(body));
          final response = Completer<http.StreamedResponse>();
          held.add(response);
          return response.future;
        }),
        timeoutMs: 15,
      );
      final scope = registry.open(api, runtimeSnapshot())!;
      final operation = registry.begin(scope, api, draftRequest(scope))!;
      await registry.retry(operation, api, scope.snapshot.writeAuthority);
      await registry.retry(operation, api, scope.snapshot.writeAuthority);
      expect(operation.mutationOwners, 2);
      expect(
        registry.canRetry(operation, api, scope.snapshot.writeAuthority),
        isFalse,
      );
      await registry.lookup(operation, api, scope.snapshot.writeAuthority);
      expect(operation.lookupOwners, 1);
      expect(
        registry.canLookup(operation, api, scope.snapshot.writeAuthority),
        isFalse,
      );
      await registry.retry(operation, api, scope.snapshot.writeAuthority);
      await registry.lookup(operation, api, scope.snapshot.writeAuthority);
      expect(held.length, 3);
      expect(
        bodies.every(
          (body) => body.toString() == operation.request.body.toString(),
        ),
        isTrue,
      );
      for (var i = 0; i < held.length; i++) {
        held[i].complete(
          modelResponse(
            i == 2
                ? runtimeLookupJson(
                    operation.request.body,
                    recorded: false,
                    fenced: false,
                  )
                : runtimeReceiptJson(operation.request.body),
          ),
        );
      }
      await Future<void>.delayed(const Duration(milliseconds: 5));
      expect(operation.mutationOwners, 0);
      expect(operation.lookupOwners, 0);
      expect(scope.pending, same(operation));
      expect(operation.uncertain, isTrue);
      expect(
        registry.canRetry(operation, api, scope.snapshot.writeAuthority),
        isTrue,
      );
      success = true;
      expect(
        await registry.retry(operation, api, scope.snapshot.writeAuthority),
        AgentRuntimeOutcome.recorded,
      );
      expect(scope.pending, isNull);
      await api.close();
      registry.dispose();
    },
  );

  test(
    'timeout then 400/409/422 or unobserved ABA equality never settles uncertainty',
    () async {
      for (final status in [400, 409, 422]) {
        final registry = AgentRuntimeWrites(
          displayDeadline: const Duration(milliseconds: 1),
        );
        final held = Completer<http.StreamedResponse>();
        var calls = 0, fenced = false;
        final api = runtimeApi(
          () => ModelTestClient((request) async {
            if (request.url.path.endsWith('/lookup')) {
              return modelResponse(
                runtimeLookupJson(
                  sentBody(request),
                  recorded: false,
                  fenced: fenced,
                ),
              );
            }
            calls++;
            if (calls == 1) return held.future;
            return modelResponse({}, status: status);
          }),
          timeoutMs: 15,
        );
        final scope = registry.open(api, runtimeSnapshot())!;
        scope.draft.yolo = true;
        final draft = scope.draft;
        final operation = registry.begin(scope, api, draftRequest(scope))!;
        await registry.retry(operation, api, scope.snapshot.writeAuthority);
        await registry.retry(operation, api, scope.snapshot.writeAuthority);
        expect(operation.uncertain, isTrue);
        expect(scope.pending, same(operation));
        registry.open(
          api,
          runtimeSnapshot(),
        ); // Same active policy/state can ABA.
        expect(
          await registry.lookup(operation, api, scope.snapshot.writeAuthority),
          isNull,
        );
        expect(scope.pending, same(operation));
        expect(scope.draft, same(draft));
        await awaitRuntimeOwners(operation, mutations: 1, lookups: 0);
        fenced = true;
        expect(
          await registry.lookup(operation, api, scope.snapshot.writeAuthority),
          AgentRuntimeOutcome.refused,
        );
        expect(scope.pending, isNull);
        expect(scope.draft, same(draft));
        expect(scope.draft.yolo, isTrue);
        held.complete(
          modelResponse(runtimeReceiptJson(operation.request.body)),
        );
        await Future<void>.delayed(const Duration(milliseconds: 5));
        expect(scope.pending, isNull);
        expect(scope.notice, contains('permanently fenced'));
        await api.close();
        registry.dispose();
      }
    },
  );

  test(
    'dirty draft keeps original state through refresh and explicit reset requires review',
    () async {
      final registry = AgentRuntimeWrites();
      final api = runtimeApi(() => throw StateError('No dispatch'));
      final scope = registry.open(api, runtimeSnapshot())!;
      final draft = scope.draft;
      draft.fileWrites = 'deny';
      registry.open(api, runtimeSnapshot(state: 'f' * 64, revision: 1));
      expect(scope.draft, same(draft));
      expect(scope.draft.base.stateToken, runtimeState);
      expect(scope.snapshot.stateToken, 'f' * 64);
      registry.reset(scope);
      expect(scope.draft.base, same(scope.snapshot));
      expect(scope.draft.dirty, isFalse);
      final operation = registry.begin(scope, api, draftRequest(scope))!;
      final owned = scope.draft;
      registry.reset(scope);
      expect(scope.draft, same(owned));
      expect(scope.pending, same(operation));
      await api.close();
      registry.dispose();
    },
  );

  test(
    'lookup display deadline keeps uncertainty and its actual owner occupied',
    () async {
      final registry = AgentRuntimeWrites(
        displayDeadline: const Duration(milliseconds: 2),
      );
      final response = Completer<http.StreamedResponse>();
      final api = runtimeApi(
        () => ModelTestClient((_) => response.future),
        timeoutMs: 30,
      );
      final scope = registry.open(api, runtimeSnapshot())!;
      final operation = registry.begin(scope, api, draftRequest(scope))!;
      final lookup = registry.lookup(
        operation,
        api,
        scope.snapshot.writeAuthority,
      );
      await Future<void>.delayed(const Duration(milliseconds: 10));
      expect(operation.notice, contains('Still waiting for save status'));
      expect(operation.uncertain, isTrue);
      expect(operation.lookupOwners, 1);
      expect(
        registry.canLookup(operation, api, scope.snapshot.writeAuthority),
        isFalse,
      );
      await lookup;
      expect(scope.pending, same(operation));
      expect(operation.lookupOwners, 1);
      response.complete(
        modelResponse(runtimeLookupJson(operation.request.body)),
      );
      await awaitRuntimeOwners(operation, mutations: 0, lookups: 0);
      expect(scope.pending, same(operation));
      await api.close();
      registry.dispose();
    },
  );

  test(
    'sixteen unresolved scopes cannot be overwritten or evicted by other owners',
    () async {
      final registry = AgentRuntimeWrites();
      final apis = <AgentRuntimeApi>[];
      final retained = <AgentRuntimeWriteScope>[];
      for (var i = 0; i < 16; i++) {
        final api = runtimeApi(
          () => throw StateError('No dispatch'),
          token: 'synthetic-owner-$i',
        );
        apis.add(api);
        final scope = registry.open(api, runtimeSnapshot())!;
        scope.draft.fileWrites = 'deny';
        retained.add(scope);
        expect(
          registry.begin(scope, api, draftRequest(scope, key: 'key-$i')),
          isNotNull,
        );
      }
      final extra = runtimeApi(
        () => throw StateError('No dispatch'),
        token: 'synthetic-extra-owner',
      );
      expect(registry.open(extra, runtimeSnapshot()), isNull);
      for (var i = 0; i < 16; i++) {
        expect(registry.open(apis[i], runtimeSnapshot()), same(retained[i]));
        expect(retained[i].pending!.request.key, 'key-$i');
        expect(retained[i].draft.fileWrites, 'deny');
      }
      for (final api in [...apis, extra]) {
        await api.close();
      }
      registry.dispose();
    },
  );
}
