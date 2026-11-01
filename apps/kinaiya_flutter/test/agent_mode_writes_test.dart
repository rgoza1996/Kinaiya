import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:kinaiya_flutter/api/agent_control.dart';
import 'package:kinaiya_flutter/api/agent_control_client.dart';
import 'package:kinaiya_flutter/state/agent_mode_writes.dart';
import 'agent_control_test.dart';
import 'agent_control_transport_test.dart' show controlApi;
import 'agent_model_transport_test.dart'
    show ModelTestClient, modelResponse, sentBody;

AgentModeChange scopedModeRequest(
  AgentModeWriteScope scope, {
  String key = 'synthetic-key',
}) => AgentModeChange.fromJson(
  modeChangeJson(
    backend: scope.snapshot.backendId,
    instance: scope.snapshot.instanceId,
    revision: scope.snapshot.revision,
    mode: scope.selection.wire,
    key: key,
  ),
);

Future<void> modeOwners(
  PendingAgentModeChange operation,
  int mutations,
  int lookups,
) async {
  final deadline = Stopwatch()..start();
  while ((operation.mutationOwners != mutations ||
          operation.lookupOwners != lookups) &&
      deadline.elapsed < const Duration(seconds: 1)) {
    await Future<void>.delayed(const Duration(milliseconds: 1));
  }
  expect(operation.mutationOwners, mutations);
  expect(operation.lookupOwners, lookups);
}

void main() {
  test(
    'an overlapping retired observation defeats a stale mutation control without replacing the original',
    () async {
      final writes = AgentModeWrites();
      var sends = 0;
      final api = controlApi(
        () => ModelTestClient((_) async {
          sends++;
          throw StateError('A stale mutation must not dispatch');
        }),
      );
      final originalState = controlState();
      final scope = writes.open(api, originalState)!;
      scope.selection = AgentMode.off;
      final operation = writes.begin(scope, api, scopedModeRequest(scope))!;
      final retired = controlState(instance: controlInstanceB, writable: false);
      expect(writes.open(api, retired), same(scope));
      expect(writes.canRetry(operation, api, originalState), isFalse);
      expect(await writes.retry(operation, api, originalState), isNull);
      expect(writes.canLookup(operation, api, retired), isTrue);
      expect(operation.request.instanceId, controlInstanceA);
      expect(scope.pending, same(operation));
      expect(sends, 0);
      await api.close();
      writes.dispose();
    },
  );
  test(
    'private credential, canonical Session URL and control backend scope drafts, notices and recovery',
    () async {
      final writes = AgentModeWrites(
        displayDeadline: const Duration(milliseconds: 1),
      );
      final held = Completer<http.StreamedResponse>();
      final api = controlApi(
        () => ModelTestClient((_) => held.future),
        timeoutMs: 20,
      );
      final scope = writes.open(api, controlState())!;
      scope.selection = AgentMode.off;
      final operation = writes.begin(scope, api, scopedModeRequest(scope))!;
      scope.selection = AgentMode.autonomous;
      await writes.retry(operation, api, scope.snapshot);
      expect(operation.uncertain, isTrue);
      expect(operation.mutationOwners, 1);
      await api.close();
      final otherOwner = controlApi(
        () => throw StateError('No send'),
        token: 'other-synthetic-owner',
      );
      final otherEndpoint = controlApi(
        () => throw StateError('No send'),
        base: 'https://different-session.invalid/session',
      );
      for (final other in [otherOwner, otherEndpoint]) {
        final hidden = writes.open(other, controlState())!;
        expect(hidden.pending, isNull);
        expect(hidden.dirty, isFalse);
        expect(hidden.notice, isNull);
        expect(writes.canLookup(operation, other, hidden.snapshot), isFalse);
      }
      final foreign = writes.open(api, controlState(backend: controlBackendB))!;
      expect(foreign.pending, isNull);
      expect(foreign.selection, AgentMode.onDemand);
      final bodies = <Map<String, Object?>>[];
      final reconnect = controlApi(
        () => ModelTestClient((request) async {
          bodies.add(sentBody(request));
          return modelResponse(modeReceiptJson(sentBody(request)));
        }),
        base: 'https://session-host.invalid/session/',
      );
      expect(writes.open(reconnect, controlState()), same(scope));
      expect(scope.selection, AgentMode.autonomous);
      expect(scope.pending, same(operation));
      expect(operation.request.mode, AgentMode.off);
      expect(
        await writes.retry(operation, reconnect, scope.snapshot),
        AgentModeOutcome.recorded,
      );
      expect(bodies.single, operation.request.body);
      expect(scope.snapshot.mode, AgentMode.onDemand); // Receipt is history.
      scope.selection = AgentMode.off;
      final replacement = writes.begin(
        scope,
        reconnect,
        scopedModeRequest(scope, key: 'replacement'),
      )!;
      scope.notice = 'replacement notice';
      held.complete(modelResponse(modeReceiptJson(operation.request.body)));
      await modeOwners(operation, 0, 0);
      expect(scope.pending, same(replacement));
      expect(scope.notice, 'replacement notice');
      await reconnect.close();
      await otherOwner.close();
      await otherEndpoint.close();
      writes.dispose();
    },
  );

  test(
    'same-backend retired writer is lookup only; fresh metadata alone never settles an original',
    () async {
      final writes = AgentModeWrites();
      final original = controlApi(() => throw StateError('No send'));
      final scope = writes.open(original, controlState())!;
      scope.selection = AgentMode.off;
      final operation = writes.begin(
        scope,
        original,
        scopedModeRequest(scope),
      )!;
      final calls = <http.BaseRequest>[];
      var recorded = true;
      final fresh = controlState(
        instance: controlInstanceB,
        revision: 2,
        mode: 'on_demand',
        writable: false,
      );
      final restarted = controlApi(
        () => ModelTestClient((request) async {
          calls.add(request);
          return modelResponse(
            modeLookupJson(
              sentBody(request),
              recorded: recorded,
              state: fresh.json,
            ),
          );
        }),
      );
      expect(writes.open(restarted, fresh), same(scope));
      expect(scope.pending, same(operation));
      expect(writes.canRetry(operation, restarted, fresh), isFalse);
      expect(writes.canLookup(operation, restarted, fresh), isTrue);
      expect(
        await writes.lookup(operation, restarted, fresh),
        AgentModeOutcome.recorded,
      );
      expect(calls.single.url.path.endsWith('/lookup'), isTrue);
      expect(sentBody(calls.single), operation.request.body);
      expect(scope.snapshot.mode, AgentMode.onDemand);
      expect(scope.snapshot.instanceId, controlInstanceB);
      expect(scope.notice, contains('history'));
      recorded = false;
      // A standalone state change never creates a fence for unrelated task work.
      expect(operation.request.mode, AgentMode.off);
      await original.close();
      await restarted.close();
      writes.dispose();
    },
  );

  test(
    'two mutation and one lookup actual owners are reusable only after genuine settlement',
    () async {
      final writes = AgentModeWrites(
        displayDeadline: const Duration(milliseconds: 1),
      );
      final held = <Completer<http.StreamedResponse>>[];
      final paths = <String>[];
      final bodies = <Map<String, Object?>>[];
      var success = false;
      final api = controlApi(
        () => ModelTestClient((request) async {
          paths.add(request.url.path);
          bodies.add(sentBody(request));
          if (success) return modelResponse(modeReceiptJson(sentBody(request)));
          final response = Completer<http.StreamedResponse>();
          held.add(response);
          return response.future;
        }),
        timeoutMs: 15,
      );
      final scope = writes.open(api, controlState())!;
      scope.selection = AgentMode.off;
      final operation = writes.begin(scope, api, scopedModeRequest(scope))!;
      await writes.retry(operation, api, scope.snapshot);
      await writes.retry(operation, api, scope.snapshot);
      await writes.lookup(operation, api, scope.snapshot);
      expect(operation.mutationOwners, 2);
      expect(operation.lookupOwners, 1);
      expect(writes.canRetry(operation, api, scope.snapshot), isFalse);
      expect(writes.canLookup(operation, api, scope.snapshot), isFalse);
      await writes.retry(operation, api, scope.snapshot);
      await writes.lookup(operation, api, scope.snapshot);
      expect(held.length, 3);
      expect(
        bodies.every(
          (body) => AgentModeChange.fromJson(body).matches(operation.request),
        ),
        isTrue,
      );
      expect(paths.where((path) => path.endsWith('/lookup')).length, 1);
      for (var i = 0; i < held.length; i++) {
        held[i].complete(
          modelResponse(
            i == 2
                ? modeLookupJson(operation.request.body)
                : modeReceiptJson(operation.request.body),
          ),
        );
      }
      await modeOwners(operation, 0, 0);
      expect(scope.pending, same(operation));
      expect(operation.uncertain, isTrue);
      expect(writes.canRetry(operation, api, scope.snapshot), isTrue);
      success = true;
      expect(
        await writes.retry(operation, api, scope.snapshot),
        AgentModeOutcome.recorded,
      );
      await modeOwners(operation, 0, 0);
      expect(scope.pending, isNull);
      await api.close();
      writes.dispose();
    },
  );

  test(
    'later conflicts, unknown generation and unfenced observations keep uncertainty until native fence',
    () async {
      for (final status in [403, 409, 422, 503]) {
        final writes = AgentModeWrites(
          displayDeadline: const Duration(milliseconds: 1),
        );
        final held = Completer<http.StreamedResponse>();
        var mutations = 0, lookupStatus = 409, fenced = false;
        final api = controlApi(
          () => ModelTestClient((request) async {
            final body = sentBody(request);
            if (request.url.path.endsWith('/lookup')) {
              if (lookupStatus != 200) {
                return modelResponse({}, status: lookupStatus);
              }
              return modelResponse(
                modeLookupJson(
                  body,
                  recorded: false,
                  state: controlStateJson(
                    mode: 'off',
                    writable: false,
                    revision: fenced ? 1 : 0,
                  ),
                ),
              );
            }
            mutations++;
            return mutations == 1
                ? held.future
                : modelResponse({}, status: status);
          }),
          timeoutMs: 15,
        );
        final scope = writes.open(api, controlState())!;
        scope.selection = AgentMode.off;
        final operation = writes.begin(scope, api, scopedModeRequest(scope))!;
        await writes.retry(operation, api, scope.snapshot);
        await writes.retry(operation, api, scope.snapshot);
        await writes.lookup(
          operation,
          api,
          scope.snapshot,
        ); // Unknown generation is not a fence.
        expect(scope.pending, same(operation));
        lookupStatus = 200;
        expect(await writes.lookup(operation, api, scope.snapshot), isNull);
        expect(operation.uncertain, isTrue);
        expect(
          scope.pending,
          same(operation),
        ); // Off/readonly without advanced revision.
        fenced = true;
        expect(
          await writes.lookup(operation, api, scope.snapshot),
          AgentModeOutcome.refused,
        );
        expect(scope.pending, isNull);
        held.complete(modelResponse(modeReceiptJson(operation.request.body)));
        await modeOwners(operation, 0, 0);
        expect(scope.notice, contains('permanently fenced'));
        await api.close();
        writes.dispose();
      }
    },
  );

  test(
    'lookup display timeout retains ownership without auto polling or mutation',
    () async {
      final writes = AgentModeWrites(
        displayDeadline: const Duration(milliseconds: 2),
      );
      final response = Completer<http.StreamedResponse>();
      var sends = 0;
      final api = controlApi(
        () => ModelTestClient((_) {
          sends++;
          return response.future;
        }),
        timeoutMs: 25,
      );
      final scope = writes.open(api, controlState())!;
      final operation = writes.begin(scope, api, scopedModeRequest(scope))!;
      final result = writes.lookup(operation, api, scope.snapshot);
      await Future<void>.delayed(const Duration(milliseconds: 8));
      expect(operation.notice, contains('Still waiting for save status'));
      expect(operation.uncertain, isTrue);
      expect(operation.lookupOwners, 1);
      expect(writes.canLookup(operation, api, scope.snapshot), isFalse);
      await result;
      expect(operation.lookupOwners, 1);
      expect(scope.pending, same(operation));
      expect(sends, 1);
      response.complete(modelResponse(modeLookupJson(operation.request.body)));
      await modeOwners(operation, 0, 0);
      expect(scope.pending, same(operation));
      await api.close();
      writes.dispose();
    },
  );

  test(
    'sixteen dirty/pending scopes are bounded and unsupported modes cannot begin',
    () async {
      final writes = AgentModeWrites();
      final apis = <AgentControlApi>[];
      final scopes = <AgentModeWriteScope>[];
      for (var i = 0; i < 16; i++) {
        final api = controlApi(
          () => throw StateError('No send'),
          token: 'synthetic-owner-$i',
        );
        apis.add(api);
        final scope = writes.open(api, controlState())!;
        scope.selection = AgentMode.off;
        scopes.add(scope);
        expect(
          writes.begin(scope, api, scopedModeRequest(scope, key: 'key-$i')),
          isNotNull,
        );
      }
      final extra = controlApi(
        () => throw StateError('No send'),
        token: 'synthetic-extra-owner',
      );
      expect(writes.open(extra, controlState()), isNull);
      for (var i = 0; i < 16; i++) {
        expect(writes.open(apis[i], controlState()), same(scopes[i]));
        expect(scopes[i].pending!.request.key, 'key-$i');
      }
      for (final api in [...apis, extra]) {
        await api.close();
      }
      writes.dispose();
      final clean = AgentModeWrites();
      final api = controlApi(() => throw StateError('No send'));
      final scope = clean.open(api, controlState())!;
      scope.selection = AgentMode.autonomous;
      expect(clean.begin(scope, api, scopedModeRequest(scope)), isNull);
      scope.selection = AgentMode.off;
      clean.reset(scope);
      expect(scope.selection, AgentMode.onDemand);
      expect(
        clean.begin(
          scope,
          api,
          AgentModeChange.fromJson(modeChangeJson(revision: 1)),
        ),
        isNull,
      );
      expect(
        clean.begin(
          scope,
          api,
          AgentModeChange.fromJson(modeChangeJson(instance: controlInstanceB)),
        ),
        isNull,
      );
      clean.open(api, controlState(writable: false));
      expect(clean.begin(scope, api, scopedModeRequest(scope)), isNull);
      await api.close();
      clean.dispose();
    },
  );
}
